# -*- coding: utf-8 -*-
"""特色数据采集（覆盖度审计 P4 股东户数 + P8 千股千评）
数据源: 东财 datacenter-web（与 push2his 行情域无关, 2026-09-23 实测可用）
  - P4 RPT_HOLDERNUM_DET      股东户数(按报告期, ~11 请求/期)
  - P8 RPT_DMSK_TS_STOCKNEW   千股千评(日频快照 ~11 请求/日, 历史错过无法回填)
单位口径(探针核实):
  HOLDER_NUM_RATIO/INTERVAL_CHRATE 原值百分数; ORG_PARTICIPATE 原值小数(入库 ×100);
  AVG_HOLD_NUM/股, AVG_MARKET_CAP/TOTAL_MARKET_CAP/元, PRIME_INFLOW/元。
增量策略:
  - holder_num: 1 请求探测接口最新 END_DATE, 库内该期>=3000 行则空转, 否则全期拉
    (期内公告持续更新, uk(ts_code,end_date) 覆盖); 回填 --start-year 2019
  - comment: 1 请求探测 TRADE_DATE, 库内已有则空转, 否则全量拉
表: stock_holder_num / stock_comment
"""
import datetime as dt
import time

import requests
from sqlalchemy import text

from app.db import engine

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
PAGE_SIZE = 500
REQ_INTERVAL = 0.5

_session = requests.Session()
_session.trust_env = False


def _get(params: dict) -> dict:
    last = None
    for _ in range(3):
        try:
            return _session.get(DC_URL, params=params,
                                headers=HEADERS, timeout=20).json()
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _fetch(report_name: str, filt: str, sort_columns: str, sort_types: str,
           columns: str = "ALL", extra: dict = None) -> list[dict]:
    rows, page = [], 1
    while True:
        payload = dict(
            sortColumns=sort_columns, sortTypes=sort_types,
            pageSize=PAGE_SIZE, pageNumber=page,
            reportName=report_name, columns=columns,
            source="WEB", client="WEB", filter=filt)
        if extra:
            payload.update(extra)
        result = (_get(payload).get("result")) or {}
        data = result.get("data") or []
        rows.extend(data)
        if page >= (result.get("pages") or 1) or not data:
            return rows
        page += 1
        time.sleep(REQ_INTERVAL)


def _upsert(table: str, set_cols: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    from app.db import engine as e
    with e.begin() as conn:
        B = 1000
        for i in range(0, len(rows), B):
            conn.execute(text(
                "INSERT INTO %s (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s,"
                " etl_update_time=NOW()" % (
                    table, ", ".join(cols), ", ".join(":" + c for c in cols),
                    set_cols)), rows[i:i + B])
    return len(rows)


def _f(x):
    try:
        return None if x in (None, "", "-") else float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    v = _f(x)
    return None if v is None else int(v)


# ---------------------------------------------------------------- P4 股东户数
QUARTER_ENDS = ("-03-31", "-06-30", "-09-30", "-12-31")


def _holder_periods(start_year: int) -> list[str]:
    today = dt.date.today().isoformat()
    out = []
    for y in range(start_year, dt.date.today().year + 1):
        for md in QUARTER_ENDS:
            d = f"{y}{md}"
            if d <= today:
                out.append(d)
    return out


def sync_holder_num(start_year: int = None) -> dict:
    """股东户数: 默认探测接口最新报告期, 库内已覆盖则空转; start_year 全量回填。"""
    # 1 请求探测最新 END_DATE
    payload = _get({
        "sortColumns": "END_DATE", "sortTypes": "-1", "pageSize": "1",
        "pageNumber": "1", "reportName": "RPT_HOLDERNUM_DET",
        "columns": "END_DATE", "source": "WEB", "client": "WEB",
        "filter": "",
    })
    data = (payload.get("result") or {}).get("data") or []
    latest = str(data[0].get("END_DATE"))[:10] if data else None
    if not latest:
        return {"periods": 0, "rows": 0, "note": "接口未返回最新期"}
    # 个别公司非季末自行披露(如 2026-09-22), 增量口径统一回退最近已过季末
    if latest[5:] not in ("03-31", "06-30", "09-30", "12-31"):
        today = dt.date.today().isoformat()
        y, m = int(latest[:4]), int(latest[5:7])
        q = ((m - 1) // 3 + 1) * 3
        qe = {3: "03-31", 6: "06-30", 9: "09-30", 12: "12-31"}[q]
        cand = f"{y}-{qe}"
        if cand > today:  # 未来季末(季中), 回退上一季末
            q -= 3
            if q == 0:
                q, y = 12, y - 1
            cand = f"{y}-{ {3: '03-31', 6: '06-30', 9: '09-30', 12: '12-31'}[q] }"
        latest = cand
    if not start_year:
        with engine.connect() as c:
            have = c.execute(text(
                "SELECT COUNT(*) FROM stock_holder_num WHERE end_date=:d"),
                {"d": latest}).scalar()
        if have and have >= 3000:
            return {"period": latest, "rows": 0, "note": "最新报告期已覆盖"}
        todo = [latest]
    else:
        covered = set()
        with engine.connect() as c:
            for r in c.execute(text(
                    "SELECT DISTINCT DATE_FORMAT(end_date,'%Y-%m-%d') "
                    "FROM stock_holder_num")):
                covered.add(r[0])
        todo = [p for p in _holder_periods(start_year) if p not in covered]
    if not todo:
        return {"periods": 0, "rows": 0, "note": "报告期均已覆盖"}
    total = 0
    for rp in todo:
        raw = _fetch("RPT_HOLDERNUM_DET", "(END_DATE='%s')" % rp,
                     "HOLD_NOTICE_DATE,SECURITY_CODE", "-1,-1",
                     "SECURITY_CODE,SECUCODE,END_DATE,INTERVAL_CHRATE,"
                     "AVG_MARKET_CAP,AVG_HOLD_NUM,TOTAL_MARKET_CAP,"
                     "TOTAL_A_SHARES,HOLD_NOTICE_DATE,HOLDER_NUM,PRE_HOLDER_NUM,"
                     "HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,PRE_END_DATE")
        rows = []
        for x in raw:
            tc = str(x.get("SECUCODE") or "")
            if not tc.endswith((".SH", ".SZ")):
                continue
            rows.append({
                "ts_code": tc, "end_date": rp,
                "holder_num": _i(x.get("HOLDER_NUM")),
                "pre_holder_num": _i(x.get("PRE_HOLDER_NUM")),
                "holder_num_change": _i(x.get("HOLDER_NUM_CHANGE")),
                "holder_num_ratio": _f(x.get("HOLDER_NUM_RATIO")),
                "interval_chrate": _f(x.get("INTERVAL_CHRATE")),
                "avg_hold_num": _f(x.get("AVG_HOLD_NUM")),
                "avg_market_cap": _f(x.get("AVG_MARKET_CAP")),
                "total_market_cap": _f(x.get("TOTAL_MARKET_CAP")),
                "total_a_shares": _f(x.get("TOTAL_A_SHARES")),
                "hold_notice_date": (str(x.get("HOLD_NOTICE_DATE"))[:10]
                                     if x.get("HOLD_NOTICE_DATE") else None),
                "pre_end_date": (str(x.get("PRE_END_DATE"))[:10]
                                 if x.get("PRE_END_DATE") else None),
            })
        total += _upsert("stock_holder_num",
                         "holder_num=VALUES(holder_num),"
                         " pre_holder_num=VALUES(pre_holder_num),"
                         " holder_num_change=VALUES(holder_num_change),"
                         " holder_num_ratio=VALUES(holder_num_ratio),"
                         " interval_chrate=VALUES(interval_chrate),"
                         " avg_hold_num=VALUES(avg_hold_num),"
                         " avg_market_cap=VALUES(avg_market_cap),"
                         " total_market_cap=VALUES(total_market_cap),"
                         " total_a_shares=VALUES(total_a_shares),"
                         " hold_notice_date=VALUES(hold_notice_date),"
                         " pre_end_date=VALUES(pre_end_date)", rows)
        print(f"[holder_num] {rp} rows={len(rows)}", flush=True)
        time.sleep(REQ_INTERVAL)
    return {"periods": len(todo), "rows": total}


# -------------------------------------------------------------- P8 千股千评
def sync_comment() -> dict:
    """千股千评日频快照: 1 请求探测 TRADE_DATE, 库内已有则空转, 否则全量拉。"""
    raw0 = _fetch("RPT_DMSK_TS_STOCKNEW", "", "SECURITY_CODE", "1",
                  "SECUCODE,TRADE_DATE",
                  extra={"token": "894050c76af8597a853f5b408b759f5d",
                         "quoteColumns": ""})
    # 探测期已耗 1+ 请求; 取当日 TRADE_DATE
    if not raw0:
        return {"rows": 0, "note": "接口未返回数据"}
    tdate = str(raw0[0].get("TRADE_DATE") or "")[:10]
    if not tdate:
        return {"rows": 0, "note": "无 TRADE_DATE"}
    with engine.connect() as c:
        have = c.execute(text(
            "SELECT COUNT(*) FROM stock_comment WHERE trade_date=:d"),
            {"d": tdate}).scalar()
    if have and have >= 3000:
        return {"trade_date": tdate, "rows": 0, "note": "当日快照已覆盖"}
    # 全量(重新拉含行情 quoteColumns)
    raw = _fetch("RPT_DMSK_TS_STOCKNEW", "", "SECURITY_CODE", "1",
                 "SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE,"
                 "CHANGE_RATE,TURNOVERRATE,PE_DYNAMIC,PRIME_COST,"
                 "PRIME_COST_20DAYS,PRIME_COST_60DAYS,ORG_PARTICIPATE,"
                 "TOTALSCORE,RANK,RANK_UP,FOCUS,PRIME_INFLOW",
                 extra={"token": "894050c76af8597a853f5b408b759f5d"})
    rows = []
    for x in raw:
        tc = str(x.get("SECUCODE") or "")
        if not tc.endswith((".SH", ".SZ")):
            continue
        op = _f(x.get("ORG_PARTICIPATE"))
        rows.append({
            "ts_code": tc, "trade_date": str(x.get("TRADE_DATE"))[:10] or tdate,
            "close_price": _f(x.get("CLOSE_PRICE")),
            "change_rate": _f(x.get("CHANGE_RATE")),
            "turnover_rate": _f(x.get("TURNOVERRATE")),
            "pe_dynamic": _f(x.get("PE_DYNAMIC")),
            "prime_cost": _f(x.get("PRIME_COST")),
            "prime_cost_20d": _f(x.get("PRIME_COST_20DAYS")),
            "prime_cost_60d": _f(x.get("PRIME_COST_60DAYS")),
            "org_participate": None if op is None else round(op * 100, 6),
            "total_score": _f(x.get("TOTALSCORE")),
            "rank_no": _i(x.get("RANK")),
            "rank_up": _i(x.get("RANK_UP")),
            "focus_index": _f(x.get("FOCUS")),
            "prime_inflow": _f(x.get("PRIME_INFLOW")),
        })
    n = _upsert("stock_comment",
                "close_price=VALUES(close_price), change_rate=VALUES(change_rate),"
                " turnover_rate=VALUES(turnover_rate), pe_dynamic=VALUES(pe_dynamic),"
                " prime_cost=VALUES(prime_cost), prime_cost_20d=VALUES(prime_cost_20d),"
                " prime_cost_60d=VALUES(prime_cost_60d),"
                " org_participate=VALUES(org_participate),"
                " total_score=VALUES(total_score), rank_no=VALUES(rank_no),"
                " rank_up=VALUES(rank_up), focus_index=VALUES(focus_index),"
                " prime_inflow=VALUES(prime_inflow)", rows)
    print(f"[comment] {tdate} rows={n}", flush=True)
    return {"trade_date": tdate, "rows": n}


def sync_rank_deriv(trade_date: str = None, top_n: int = 200, window: int = 60) -> dict:
    """技术形态榜单本地衍生(P9, 覆盖度审计结论: THS 榜单接口 vendored akshare 未内置,
    连涨/创新高由库内日线直接计算, 零外部请求)。四类榜单, 各取 top_n:
      NEW_HIGH_60  创 60 日新高(收盘价 = 窗口最高, 含当日), 按当日涨跌幅降序
      NEW_LOW_60   创 60 日新低(收盘价 = 窗口最低, 含当日), 按当日涨跌幅升序
      CONT_UP      连涨榜(cont_days > 0), 按连涨天数降序
      CONT_DOWN    连跌榜(cont_days < 0), 按连跌天数降序(绝对值)
    幂等: UNIQUE(ts_code, trade_date, rank_type), 重跑覆盖。"""
    t0 = time.time()
    with engine.connect() as conn:
        d = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_index_daily")).scalar()
        if d is None:
            return {"rows": 0}
        qrows = conn.execute(text(
            "SELECT ts_code, trade_date, close, pct_chg, cont_days FROM stock_daily_quote "
            "WHERE trade_date > :d0 AND trade_date <= :d1 ORDER BY ts_code, trade_date"),
            {"d0": d - dt.timedelta(days=int(window * 2.2)), "d1": d}).fetchall()
    # 取每只股票的窗口(恰好 window+1 行, 不足则跳过新高/新低判定)
    dates_all = sorted({r[1] for r in qrows})
    win_dates = set(dates_all[-(window + 1):])
    from collections import defaultdict
    hist = defaultdict(list)
    for ts, td, close, pct, cont in qrows:
        hist[ts].append((td, float(close), None if pct is None else float(pct),
                         None if cont is None else int(cont)))

    boards = {"NEW_HIGH_60": [], "NEW_LOW_60": [], "CONT_UP": [], "CONT_DOWN": []}
    for ts, seq in hist.items():
        last = seq[-1]
        if last[0] != d:
            continue  # 当日无行情(停牌)不进榜
        _, c, pct, cont = last
        win = [x for x in seq if x[0] in win_dates]
        if len(win) >= window and c is not None and pct is not None:
            closes = [x[1] for x in win]
            if c >= max(closes) - 1e-9:
                boards["NEW_HIGH_60"].append((ts, pct))
            if c <= min(closes) + 1e-9:
                boards["NEW_LOW_60"].append((ts, pct))
        if cont is not None and cont > 0:
            boards["CONT_UP"].append((ts, float(cont)))
        elif cont is not None and cont < 0:
            boards["CONT_DOWN"].append((ts, float(cont)))

    rows = []
    for btype, items in boards.items():
        desc = btype != "NEW_LOW_60"
        items.sort(key=lambda x: x[1], reverse=desc)
        for rank, (ts, val) in enumerate(items[:top_n], 1):
            rows.append({"ts_code": ts, "trade_date": d, "rank_type": btype,
                         "rank_num": rank, "metric_value": round(val, 4)})

    with engine.begin() as conn:
        for btype in boards:
            conn.execute(text(
                "DELETE FROM stock_rank_deriv WHERE trade_date = :d AND rank_type = :t"),
                {"d": d, "t": btype})
        if rows:
            conn.execute(text("""
                INSERT INTO stock_rank_deriv
                    (ts_code, trade_date, rank_type, rank_num, metric_value)
                VALUES (:ts_code, :trade_date, :rank_type, :rank_num, :metric_value)"""),
                rows)
    cnt = {k: len(v) for k, v in boards.items()}
    print(f"[rank_deriv] {d} 新高{cnt['NEW_HIGH_60']} 新低{cnt['NEW_LOW_60']} "
          f"连涨{cnt['CONT_UP']} 连跌{cnt['CONT_DOWN']} 落库 top{top_n} ×4, "
          f"耗时 {time.time()-t0:.0f}s", flush=True)
    return {"trade_date": str(d), "counts": cnt, "rows": len(rows)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="P4 股东户数 + P8 千股千评 采集")
    ap.add_argument("--holder-start-year", type=int, default=None,
                    help="股东户数回填起始年(如 2019)")
    ap.add_argument("--skip-holder", action="store_true")
    ap.add_argument("--skip-comment", action="store_true")
    a = ap.parse_args()
    if not a.skip_holder:
        print(sync_holder_num(start_year=a.holder_start_year))
    if not a.skip_comment:
        print(sync_comment())
