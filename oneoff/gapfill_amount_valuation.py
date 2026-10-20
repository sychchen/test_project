# -*- coding: utf-8 -*-
"""缺口回填(baostock 通道): stock_daily_quote.amount + stock_valuation(pe_ttm/pb)

背景: amount/估值快照列设计上都依赖"当日 moneyflow/valsnap 快照步骤成功",
     快照源永远无法补历史 —— 快照步骤失败的交易日, 这些列就永久 NULL。
     本脚本用 baostock 历史K线(peTTM/pbMRQ/amount/tradestatus)补任意近 N 日缺口。
口径:
  amount   : baostock 原值(元), 仅 UPDATE NULL 行, 不碰已有值
  pe_ttm/pb: baostock peTTM/pbMRQ 原值
  total_mv/circ_mv: 库内最新市值 × (close_t/close_latest) 缩放(股本短期不变,
             两日内误差可忽略; baostock 无市值字段)
  pct_chg  : 由库内 close 序列现算
自动探测: 近 lookback_days 日内, quote 表有行但 amount NULL>100 或 valuation 缺日的交易日。
幂等: oneoff/gapfill_progress.txt 记录已处理股票, 分片重跑直插前沿。
用法: python oneoff/gapfill_amount_valuation.py [--lookback 30] [--limit 500]
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import baostock as bs
import pandas as pd
from sqlalchemy import text

from app.db import engine

PROGRESS = Path(__file__).resolve().parent / "gapfill_progress.txt"
REQ_INTERVAL = 0.05
MIN_ROWS_HINT = 100      # 某日 NULL amount 超过该值才视为缺口(停牌/次新噪声)
MIN_VALUATION_COVER = 0.5  # 估值覆盖率低于 50% 视为缺口


def _bs_code(ts_code: str) -> str:
    sym = ts_code.split(".")[0]
    return ("sh." if ts_code.endswith(".SH") else "sz.") + sym


def detect_targets(lookback_days: int):
    """返回 (amount 缺口日集, valuation 缺口日集)"""
    since = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    with engine.connect() as conn:
        q_days = [str(r[0]) for r in conn.execute(text(
            "SELECT DISTINCT trade_date FROM stock_daily_quote "
            "WHERE trade_date >= :s AND trade_date < CURDATE() "
            "ORDER BY trade_date"), {"s": since})]
        amt_days, val_days = set(), set()
        for d in q_days:
            n_null = conn.execute(text(
                "SELECT COUNT(*) FROM stock_daily_quote "
                "WHERE trade_date=:d AND amount IS NULL"), {"d": d}).scalar()
            if n_null > MIN_ROWS_HINT:
                amt_days.add(d)
            cov = conn.execute(text(
                "SELECT COUNT(*) FROM stock_valuation WHERE trade_date=:d"), {"d": d}).scalar()
            nq = conn.execute(text(
                "SELECT COUNT(*) FROM stock_daily_quote WHERE trade_date=:d"), {"d": d}).scalar()
            if nq and cov < nq * MIN_VALUATION_COVER:
                val_days.add(d)
    return amt_days, val_days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--limit", type=int, default=None, help="本次最多处理 N 只(分片)")
    args = ap.parse_args()

    amt_days, val_days = detect_targets(args.lookback)
    if not amt_days and not val_days:
        print("[gapfill] 近期无缺口, 无事可做")
        return
    if val_days and not amt_days:
        # 仅估值缺口时也要拉 amount 字段? 不需要, 但请求同源, 一并带上无害
        pass
    all_days = sorted(amt_days | val_days)
    beg, end = all_days[0], all_days[-1]
    print(f"[gapfill] amount 缺口: {sorted(amt_days)} | valuation 缺口: {sorted(val_days)}",
          flush=True)
    print(f"[gapfill] baostock 区间 {beg} ~ {end}", flush=True)

    done = set()
    if PROGRESS.exists():
        done = {ln.strip() for ln in
                PROGRESS.read_text(encoding="utf-8").splitlines() if ln.strip()}

    with engine.connect() as conn:
        stocks = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT ts_code FROM stock_daily_quote "
            "WHERE trade_date >= :s ORDER BY ts_code"), {"s": beg}).fetchall()]
        # 每股最新市值与收盘(用于市值缩放)
        mv = pd.read_sql(text(
            "SELECT q.ts_code, q.close, q.total_mv, q.circ_mv FROM stock_daily_quote q "
            "JOIN (SELECT ts_code, MAX(trade_date) md FROM stock_daily_quote "
            "      WHERE total_mv IS NOT NULL GROUP BY ts_code) t "
            "ON q.ts_code=t.ts_code AND q.trade_date=t.md"), engine)
    mv_map = {r.ts_code: (float(r.close), float(r.total_mv), float(r.circ_mv))
              for r in mv.itertuples() if pd.notna(r.total_mv)}

    todo = [tc for tc in stocks if tc not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[gapfill] 股票 {len(stocks)}, 已处理 {len(done)}, 待处理 {len(todo)}", flush=True)
    if not todo:
        return

    lg = bs.login()
    if lg.error_code != "0":
        print(f"[gapfill] baostock 登录失败: {lg.error_msg}", flush=True)
        return

    amt_sql = text("""
        UPDATE stock_daily_quote SET amount=:amount, etl_update_time=NOW()
        WHERE ts_code=:ts_code AND trade_date=:trade_date AND amount IS NULL
    """)
    val_sql = text("""
        INSERT INTO stock_valuation
            (ts_code, trade_date, close_price, pct_chg, total_mv, circ_mv,
             pe_ttm, pb)
        VALUES (:ts_code, :trade_date, :close_price, :pct_chg, :total_mv,
                :circ_mv, :pe_ttm, :pb)
        ON DUPLICATE KEY UPDATE
            close_price=VALUES(close_price), pct_chg=VALUES(pct_chg),
            total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv),
            pe_ttm=VALUES(pe_ttm), pb=VALUES(pb)
    """)

    ok = failed = amt_rows = val_rows = 0
    t0 = time.time()
    try:
        for i, tc in enumerate(todo, 1):
            try:
                rs = bs.query_history_k_data_plus(
                    _bs_code(tc),
                    "date,code,amount,peTTM,pbMRQ,tradestatus",
                    start_date=beg, end_date=end,
                    frequency="d", adjustflag="3")
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
            except Exception as e:
                failed += 1
                print(f"[gapfill] {tc} 拉取失败: {type(e).__name__}: {e}", flush=True)
                time.sleep(2)
                continue   # 断点不含它, 下轮重试

            with engine.begin() as conn:
                # 库内已有 pct_chg(腾讯K线自带), 直接取用
                qrows = {str(d): (c, p) for d, c, p in conn.execute(text(
                    "SELECT trade_date, close, pct_chg FROM stock_daily_quote "
                    "WHERE ts_code=:tc AND trade_date IN :ds"),
                    {"tc": tc, "ds": tuple(all_days)}).fetchall()}
                for d_iso in all_days:
                    if d_iso not in qrows:
                        continue
                    c, pct = qrows[d_iso]
                    if not c:
                        continue
                    bs_row = next((r for r in rows if r[0] == d_iso), None)
                    if bs_row is None:
                        continue   # 停牌日 baostock 无记录
                    _, _, amt, pe, pb, status = bs_row
                    if d_iso in amt_days and amt:
                        try:
                            r1 = conn.execute(amt_sql, {
                                "ts_code": tc, "trade_date": d_iso,
                                "amount": float(amt)})
                            amt_rows += r1.rowcount
                        except ValueError:
                            pass
                    if d_iso in val_days and status == "1":
                        mv_base = mv_map.get(tc)
                        ratio = (float(c) / mv_base[0]) if mv_base and mv_base[0] else None
                        conn.execute(val_sql, {
                            "ts_code": tc, "trade_date": d_iso,
                            "close_price": float(c), "pct_chg": pct,
                            "total_mv": (round(mv_base[1] * ratio, 2)
                                         if mv_base and ratio else None),
                            "circ_mv": (round(mv_base[2] * ratio, 2)
                                        if mv_base and ratio else None),
                            "pe_ttm": (float(pe) if pe else None),
                            "pb": (float(pb) if pb else None)})
                        val_rows += 1
            ok += 1
            with PROGRESS.open("a", encoding="utf-8") as pf:
                pf.write(tc + "\n")
            if i % 500 == 0:
                print(f"[gapfill] progress {i}/{len(todo)} ok={ok} amt={amt_rows} "
                      f"val={val_rows} failed={failed} "
                      f"elapsed={int(time.time() - t0)}s", flush=True)
            time.sleep(REQ_INTERVAL)
    finally:
        bs.logout()

    print(f"[gapfill] 本分片: ok={ok} amount+{amt_rows}行 valuation+{val_rows}行 "
          f"failed={failed} 用时={int(time.time() - t0)}s", flush=True)

    # 缺口补完后刷新估值分位落库(供 VALUE_PCTL / 面板使用)
    if val_days and ok and not failed:
        from app.collector.valuation_pctl import refresh_valuation_pctl
        for d in sorted(val_days):
            try:
                r = refresh_valuation_pctl(trade_date=d)
                print(f"[gapfill] valpctl {d}: {r}", flush=True)
            except Exception as e:
                print(f"[gapfill] valpctl {d} 失败: {e}", flush=True)


if __name__ == "__main__":
    main()
