# -*- coding: utf-8 -*-
"""风险排雷数据采集（覆盖度审计 P5 限售解禁 + P6 股权质押/商誉）
数据源: 东财 datacenter-web（与 push2his 行情域无关, 2026-09-23 实测可用）
  - P5 RPT_LIFT_STAGE     限售解禁事件(含未来解禁日历, 抛压排雷)
  - P6 RPT_CSDC_LIST      个股质押比例(中证登周频快照)
  - P6 RPT_GOODWILL_STOCKDETAILS 个股商誉明细(按报告期)
单位口径(探针核实):
  解禁数量/万股, 解禁市值/万元, FREE_RATIO·TOTAL_RATIO 原值小数(入库 ×100 转百分比),
  质押比例原值百分数, 质押股数/万股, 质押市值/万元, 商誉/元。
增量策略:
  - restricted: 事件表无"缺日"概念, 常规拉滚动窗口(过去90天~未来180天, ~2 请求);
    首轮回填 --start 2024-01-01(全窗口 ~20 请求)
  - pledge: 中证登周频, 探测接口最新 TRADE_DATE 库内已有则空转(1 请求);
    回填传 dates 列表(建议每月最后交易日, ~5 请求/期)
  - goodwill: 缺报告期的期数才拉(1-2 请求/期)
表: stock_restricted_release / stock_pledge_ratio / stock_goodwill
"""
import datetime as dt
import time

import requests
from sqlalchemy import text

from app import config
from app.db import engine

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
PAGE_SIZE = 500
REQ_INTERVAL = 0.5

_session = requests.Session()
_session.trust_env = False


def _get(params: dict) -> dict:
    last = None
    for _ in range(config.RETRY_TIMES):
        try:
            return _session.get(DC_URL, params=params,
                                headers=HEADERS, timeout=20).json()
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _fetch(report_name: str, filt: str, sort_columns: str, sort_types: str,
           columns: str = "ALL") -> list[dict]:
    rows, page = [], 1
    while True:
        payload = _get({
            "sortColumns": sort_columns, "sortTypes": sort_types,
            "pageSize": PAGE_SIZE, "pageNumber": page,
            "reportName": report_name, "columns": columns,
            "source": "WEB", "client": "WEB", "filter": filt,
        })
        result = payload.get("result") or {}
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
    with engine.begin() as conn:
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


# ---------------------------------------------------------------- P5 限售解禁
def sync_restricted_release(start_date: str = None, end_date: str = None) -> dict:
    """限售解禁事件增量: 默认滚动窗口(过去90天~未来180天); --start 全量回填。"""
    today = dt.date.today()
    sd = start_date or (today - dt.timedelta(days=90)).isoformat()
    ed = end_date or (today + dt.timedelta(days=180)).isoformat()
    raw = _fetch("RPT_LIFT_STAGE",
                 "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (sd, ed),
                 "FREE_DATE,CURRENT_FREE_SHARES", "1,1",
                 "SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,FREE_DATE,"
                 "CURRENT_FREE_SHARES,ABLE_FREE_SHARES,LIFT_MARKET_CAP,FREE_RATIO,"
                 "NEW,B20_ADJCHRATE,A20_ADJCHRATE,FREE_SHARES_TYPE,TOTAL_RATIO,"
                 "NON_FREE_SHARES,BATCH_HOLDER_NUM")
    rows = []
    for x in raw:
        tc = str(x.get("SECUCODE") or "")
        if not tc.endswith((".SH", ".SZ")):
            continue
        fd = str(x.get("FREE_DATE") or "")[:10]
        st = (x.get("FREE_SHARES_TYPE") or "")[:64]
        if not fd or not st:
            continue
        rows.append({
            "ts_code": tc, "free_date": fd, "shares_type": st,
            "free_shares": _f(x.get("CURRENT_FREE_SHARES")),
            "actual_free_shares": _f(x.get("ABLE_FREE_SHARES")),
            "lift_market_cap": _f(x.get("LIFT_MARKET_CAP")),
            "free_ratio": None if _f(x.get("FREE_RATIO")) is None else round(_f(x.get("FREE_RATIO")) * 100, 6),
            "total_ratio": None if _f(x.get("TOTAL_RATIO")) is None else round(_f(x.get("TOTAL_RATIO")) * 100, 6),
            "b20_chg": _f(x.get("B20_ADJCHRATE")),
            "a20_chg": _f(x.get("A20_ADJCHRATE")),
            "batch_holder_num": _i(x.get("BATCH_HOLDER_NUM")),
        })
    n = _upsert("stock_restricted_release",
                "free_shares=VALUES(free_shares), actual_free_shares=VALUES(actual_free_shares),"
                " lift_market_cap=VALUES(lift_market_cap), free_ratio=VALUES(free_ratio),"
                " total_ratio=VALUES(total_ratio), b20_chg=VALUES(b20_chg),"
                " a20_chg=VALUES(a20_chg), batch_holder_num=VALUES(batch_holder_num)",
                rows)
    print(f"[restricted] {sd}~{ed} rows={n}", flush=True)
    return {"start": sd, "end": ed, "rows": n}


# ---------------------------------------------------------- P6a 个股质押比例
def sync_pledge_ratio(dates: list = None) -> dict:
    """个股质押比例: 默认探测接口最新快照日, 库内已有则空转; dates 回填指定日。"""
    latest = None
    payload = _get({
        "sortColumns": "TRADE_DATE", "sortTypes": "-1", "pageSize": "1",
        "pageNumber": "1", "reportName": "RPT_CSDC_LIST", "columns": "TRADE_DATE",
        "source": "WEB", "client": "WEB",
    })
    data = (payload.get("result") or {}).get("data") or []
    if data:
        latest = str(data[0].get("TRADE_DATE") or "")[:10]
    if not dates:
        with engine.connect() as c:
            have = c.execute(text(
                "SELECT COUNT(*) FROM stock_pledge_ratio WHERE trade_date=:d"),
                {"d": latest}).scalar() if latest else 0
        if have and have > 0:
            return {"trade_date": latest, "rows": 0, "note": "最新快照已覆盖"}
        dates = [latest]
    total = 0
    for d in dates:
        raw = _fetch("RPT_CSDC_LIST", "(TRADE_DATE='%s')" % d,
                     "PLEDGE_RATIO", "-1")
        rows = []
        for x in raw:
            tc = str(x.get("SECUCODE") or "")
            if not tc.endswith((".SH", ".SZ")):
                continue
            rows.append({
                "ts_code": tc, "trade_date": str(x.get("TRADE_DATE") or "")[:10] or d,
                "pledge_ratio": _f(x.get("PLEDGE_RATIO")),
                "pledge_shares": _f(x.get("REPURCHASE_BALANCE")),
                "pledge_mv": _f(x.get("PLEDGE_MARKET_CAP")),
                "deal_num": _i(x.get("PLEDGE_DEAL_NUM")),
                "industry": (x.get("INDUSTRY") or "")[:64] or None,
            })
        total += _upsert("stock_pledge_ratio",
                         "pledge_ratio=VALUES(pledge_ratio), pledge_shares=VALUES(pledge_shares),"
                         " pledge_mv=VALUES(pledge_mv), deal_num=VALUES(deal_num),"
                         " industry=VALUES(industry)", rows)
        print(f"[pledge] {d} rows={len(rows)}", flush=True)
        time.sleep(REQ_INTERVAL)
    return {"dates": dates, "rows": total}


# -------------------------------------------------------------- P6b 个股商誉
QUARTER_ENDS = ("0331", "0630", "0930", "1231")


def _gw_periods(start_year: int = 2019) -> list[str]:
    today = dt.date.today()
    out = []
    for y in range(start_year, today.year + 1):
        for md in QUARTER_ENDS:
            d = f"{y}{md}"
            if d <= today.strftime("%Y%m%d"):
                out.append(d)
    return out


def sync_goodwill(start_year: int = None, periods: int = 1) -> dict:
    """个股商誉: 默认补最新未覆盖报告期; start_year 全量回填。"""
    covered = set()
    with engine.connect() as c:
        for r in c.execute(text(
                "SELECT DISTINCT DATE_FORMAT(report_date,'%Y%m%d') "
                "FROM stock_goodwill")):
            covered.add(r[0])
    if start_year:
        todo = [p for p in _gw_periods(start_year) if p not in covered]
    else:
        todo = [p for p in _gw_periods()[-periods:] if p not in covered]
    if not todo:
        return {"periods": 0, "rows": 0, "note": "报告期均已覆盖"}
    total = 0
    for p in todo:
        rp = f"{p[:4]}-{p[4:6]}-{p[6:]}"
        raw = _fetch("RPT_GOODWILL_STOCKDETAILS", "(REPORT_DATE='%s')" % rp,
                     "NOTICE_DATE,SECURITY_CODE", "-1,-1")
        rows = []
        for x in raw:
            tc = str(x.get("SECUCODE") or "")
            if not tc.endswith((".SH", ".SZ")):
                continue
            rows.append({
                "ts_code": tc, "report_date": rp,
                "notice_date": (str(x.get("NOTICE_DATE"))[:10]
                                if x.get("NOTICE_DATE") else None),
                "goodwill": _f(x.get("GOODWILL")),
                "goodwill_pre": _f(x.get("GOODWILL_PRE")),
                "impairment": _f(x.get("GOODWILL_CHANGE")),
                "net_asset_ratio": None if _f(x.get("SUMSHEQUITY_RATIO")) is None else round(_f(x.get("SUMSHEQUITY_RATIO")) * 100, 6),
                "net_profit": _f(x.get("PARENTNETPROFIT")),
            })
        total += _upsert("stock_goodwill",
                         "notice_date=VALUES(notice_date), goodwill=VALUES(goodwill),"
                         " goodwill_pre=VALUES(goodwill_pre), impairment=VALUES(impairment),"
                         " net_asset_ratio=VALUES(net_asset_ratio), net_profit=VALUES(net_profit)",
                         rows)
        print(f"[goodwill] {rp} rows={len(rows)}", flush=True)
        time.sleep(REQ_INTERVAL)
    return {"periods": len(todo), "rows": total}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="P5 解禁 + P6 质押/商誉 采集")
    ap.add_argument("--restricted-start", type=str, default=None,
                    help="解禁回填起始日(如 2024-01-01)")
    ap.add_argument("--pledge-dates", type=str, default=None,
                    help="质押回填日期, 逗号分隔 YYYY-MM-DD")
    ap.add_argument("--goodwill-start-year", type=int, default=None)
    a = ap.parse_args()
    if a.restricted_start:
        print(sync_restricted_release(start_date=a.restricted_start))
    print(sync_restricted_release())
    if a.pledge_dates:
        print(sync_pledge_ratio(dates=[d.strip() for d in a.pledge_dates.split(",") if d.strip()]))
    print(sync_pledge_ratio())
    print(sync_goodwill(start_year=a.goodwill_start_year))
