# -*- coding: utf-8 -*-
"""日频衍生指标: 连涨天数 / 连涨涨幅 / 量比 / 换手率

统一管理 stock_daily_quote 上的四个衍生指标列:

  cont_days      连涨天数  正=连续上涨, 负=连续下跌, 0=平盘
  cont_pct       连涨涨幅(%)  当前连续涨/跌段内累计涨跌幅(与 cont_days 同段)
  vol_ratio      量比      当日成交量 / 前5个交易日均量
  turnover_rate  换手率(%) 成交量(手)×100 / 流通股本; 流通股本 = 流通市值/收盘价
                          => vol × close × 10000 / circ_mv

写入路径(两条):

1. 增量(日常盘后): `--quotes` 时由 quote_collector 顺带计算 cont_days/cont_pct/
   vol_ratio(三者自足于K线, 零额外请求); turnover_rate 依赖 circ_mv, 由
   `--valsnap`(snapshot_valuation) 在估值落库后计算。
2. 全量/区间回填: 本模块 refresh() / CLI, 一次重算全表(或最近 N 个交易日)。
   MySQL 5.5 无窗口函数, 故在 Python 侧按 ts_code 分组滚动计算, 写临时表后
   UPDATE JOIN 一次性刷回。

用法:
  python -m app.collector.indicator_collector                 # 全量重算
  python -m app.collector.indicator_collector --days 40       # 仅重算最近40个交易日(带预热窗)
  python -m app.collector.indicator_collector --turnover-only # 只刷换手率
"""
import argparse
import math
import os
import sys
import time

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.collector.quote_collector import cont_days_series, cont_pct_series  # noqa: E402
from app.db import engine  # noqa: E402

# 幂等加列: 列名 -> DDL 片段
_COLUMNS = {
    "cont_pct":      "DECIMAL(10,4) NULL COMMENT '连涨涨幅(%,当前连涨/连跌段累计)'",
    "vol_ratio":     "DECIMAL(10,4) NULL COMMENT '量比(当日量/前5日均量)'",
    "turnover_rate": "DECIMAL(10,4) NULL COMMENT '换手率(%)'",
}

# 换手率 = 成交量(手)*100(股/手) / 流通股本 * 100%; 流通股本 = 流通市值/收盘价
# 注意: 这是**格式串**（下方用 .format(where=...) 拼 WHERE 前缀），
# 不能包 text() —— 否则 TextClause 没有 .format（2026-09-16 修）。
TURNOVER_SQL = """
    UPDATE stock_daily_quote
    SET turnover_rate = ROUND(vol * close * 10000 / circ_mv, 4),
        etl_update_time = NOW()
    WHERE {where} circ_mv IS NOT NULL AND circ_mv > 0
      AND vol IS NOT NULL AND close IS NOT NULL AND close > 0
"""


def ensure_columns() -> list:
    """幂等新增 cont_pct / vol_ratio / turnover_rate 三列，返回新建列名。"""
    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='stock_daily_quote'"
        ))}
        added = []
        for col, ddl in _COLUMNS.items():
            if col not in existing:
                conn.execute(text(
                    f"ALTER TABLE stock_daily_quote ADD COLUMN {col} {ddl}"))
                added.append(col)
    return added


def sync_turnover_rate(trade_date=None) -> dict:
    """按交易日刷新换手率(纯 SQL, 单次 UPDATE)。trade_date 为空则刷全表。

    依赖同行的 circ_mv(由 snapshot_valuation 写入)与 vol/close。
    """
    ensure_columns()
    if trade_date:
        where, args = "trade_date = :td AND ", {"td": trade_date}
    else:
        where, args = "", {}
    with engine.begin() as conn:
        r = conn.execute(text(TURNOVER_SQL.format(where=where)), args)
        rows = r.rowcount
        td = trade_date or conn.execute(
            text("SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
    return {"trade_date": str(td), "rows": rows}


def refresh(days: int = None) -> dict:
    """重算 cont_days / cont_pct / vol_ratio / turnover_rate。

    days=None -> 全表; days=N -> 只刷最近 N 个交易日(向前多读 30 个交易日作预热窗,
    保证连涨计数与量比在窗口边界处仍正确)。
    """
    ensure_columns()
    t0 = time.time()
    with engine.connect() as conn:
        if days:
            dates = [r[0] for r in conn.execute(text(
                "SELECT DISTINCT trade_date FROM stock_daily_quote "
                "ORDER BY trade_date DESC LIMIT :n"), {"n": days + 30})]
            if not dates:
                return {"rows": 0, "mode": "range", "days": days}
            lo, hi = min(dates), max(dates)
            df = pd.read_sql(text(
                "SELECT ts_code, trade_date, pct_chg, vol, close, circ_mv "
                "FROM stock_daily_quote WHERE trade_date >= :lo ORDER BY ts_code, trade_date"
            ), conn, params={"lo": lo})
            target = set(dates[:days])          # 只回写最近 N 日
        else:
            lo = hi = None
            df = pd.read_sql(text(
                "SELECT ts_code, trade_date, pct_chg, vol, close, circ_mv "
                "FROM stock_daily_quote ORDER BY ts_code, trade_date"), conn)
            target = None
    print(f"[indicator] 读取 {len(df)} 行, {time.time() - t0:.1f}s", flush=True)

    out = []
    for ts_code, g in df.groupby("ts_code", sort=False):
        pct = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
               for v in g["pct_chg"].tolist()]
        vol = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
               for v in g["vol"].tolist()]
        close = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
                 for v in g["close"].tolist()]
        cmv = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
               for v in g["circ_mv"].tolist()]
        dates = g["trade_date"].tolist()

        cds = cont_days_series(pct)
        cps = cont_pct_series(pct, cds)
        # 量比: 前5个交易日均量(不含当日); 不足5根 -> None
        vrs = []
        for i in range(len(vol)):
            if i < 5:
                vrs.append(None)
                continue
            win = vol[i - 5:i]
            if any(x is None for x in win) or vol[i] is None:
                vrs.append(None)
                continue
            m = sum(win) / 5.0
            vrs.append(None if m == 0 else vol[i] / m)
        # 换手率: vol(手)*close*10000/circ_mv(元)
        trs = []
        for i in range(len(vol)):
            if vol[i] is None or close[i] is None or cmv[i] is None or cmv[i] <= 0:
                trs.append(None)
                continue
            trs.append(round(vol[i] * close[i] * 10000 / cmv[i], 4))

        for k, d in enumerate(dates):
            if target is not None and d not in target:
                continue
            out.append((ts_code, d, cds[k], cps[k], vrs[k], trs[k]))
    print(f"[indicator] 计算完成 {len(out)} 行, {time.time() - t0:.1f}s", flush=True)

    with engine.begin() as conn:
        conn.execute(text("DROP TEMPORARY TABLE IF EXISTS tmp_ind"))
        conn.execute(text("""
            CREATE TEMPORARY TABLE tmp_ind (
                ts_code       VARCHAR(16) NOT NULL,
                trade_date    DATE        NOT NULL,
                cont_days     INT         NULL,
                cont_pct      DECIMAL(10,4) NULL,
                vol_ratio     DECIMAL(10,4) NULL,
                turnover_rate DECIMAL(10,4) NULL,
                UNIQUE KEY uk_code_date (ts_code, trade_date)
            ) ENGINE=InnoDB
        """))
        ins = text("INSERT INTO tmp_ind (ts_code, trade_date, cont_days, cont_pct, "
                   "vol_ratio, turnover_rate) VALUES (:a,:b,:c,:d,:e,:f)")
        batch = 5000
        for i in range(0, len(out), batch):
            conn.execute(ins, [{"a": a, "b": b, "c": c, "d": d, "e": e, "f": f}
                               for a, b, c, d, e, f in out[i:i + batch]])
        print(f"[indicator] 临时表写入完成, {time.time() - t0:.1f}s", flush=True)
        r = conn.execute(text("""
            UPDATE stock_daily_quote q
            JOIN tmp_ind t ON t.ts_code = q.ts_code AND t.trade_date = q.trade_date
            SET q.cont_days = t.cont_days, q.cont_pct = t.cont_pct,
                q.vol_ratio = t.vol_ratio, q.turnover_rate = t.turnover_rate
        """))
        conn.execute(text("DROP TEMPORARY TABLE tmp_ind"))

    with engine.connect() as conn:
        cov = conn.execute(text(
            "SELECT COUNT(*) AS tot, "
            "SUM(cont_pct IS NOT NULL) AS cont_pct, "
            "SUM(vol_ratio IS NOT NULL) AS vol_ratio, "
            "SUM(turnover_rate IS NOT NULL) AS turnover_rate "
            "FROM stock_daily_quote")).mappings().first()
    return {"rows": r.rowcount, "mode": "range" if days else "full",
            "days": days, "coverage": dict(cov), "elapsed": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser(description="日频衍生指标重算(连涨天数/连涨涨幅/量比/换手率)")
    ap.add_argument("--days", type=int, default=None,
                    help="只刷最近 N 个交易日(默认全表; 内部多读30日预热)")
    ap.add_argument("--turnover-only", action="store_true", help="只刷新换手率(纯SQL, 快速)")
    args = ap.parse_args()

    if args.turnover_only:
        r = sync_turnover_rate()
        print(f"[indicator] 换手率刷新 rows={r['rows']} trade_date={r['trade_date']}", flush=True)
        return
    r = refresh(days=args.days)
    print(f"[indicator] 完成 rows={r['rows']} mode={r['mode']} "
          f"elapsed={r['elapsed']}s coverage={r['coverage']}", flush=True)


if __name__ == "__main__":
    main()
