# -*- coding: utf-8 -*-
"""个股资金流向采集（日度）
数据源: 东方财富 push2delay.eastmoney.com clist 批量快照（已实测可用）
字段口径:
    f62 主力净流入(元) / f66 超大单净流入 / f72 大单净流入 / f78 中单净流入 /
    f84 小单净流入 / f184 主力净流入占比% / f6 成交额(元, 顺带回填 stock_daily_quote.amount)
    校验关系: 主力 = 超大单 + 大单（已数值验证）
历史口径说明:
    新浪 MoneyFlow 服务已下线、腾讯 zjlx 控制器已下线、东财 push2his 历史接口
    对本机 IP 并发限流(封禁), 均不可用 -> 本表自上线日起逐日前滚积累,
    与 stock_minute_quote 的积累方式一致。
    trade_date 取库内 stock_daily_quote 最近交易日(先跑 quotes 再跑本任务),
    非交易日运行时快照值与上一交易日相同, upsert 幂等无害。
"""
import math
import time

import requests
from sqlalchemy import text

from app import config
from app.db import engine

EM_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
HEADERS = {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
# 沪深A股范围: 深主板+创业板+沪主板+科创板(与 stock_basic 采集范围一致)
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
FIELDS = "f12,f62,f66,f72,f78,f84,f184,f6,f26"
PAGE_SIZE = 100  # 东财该接口单页上限 100(传更大值会被截断, 已实测)

_session = requests.Session()
_session.trust_env = False  # 直连, 绕过系统代理

UPSERT_SQL = text("""
    INSERT INTO stock_money_flow
        (ts_code, trade_date, main_net, super_net, large_net, mid_net, small_net, main_pct)
    VALUES
        (:ts_code, :trade_date, :main_net, :super_net, :large_net, :mid_net, :small_net, :main_pct)
    ON DUPLICATE KEY UPDATE
        main_net=VALUES(main_net), super_net=VALUES(super_net), large_net=VALUES(large_net),
        mid_net=VALUES(mid_net), small_net=VALUES(small_net), main_pct=VALUES(main_pct),
        etl_update_time=NOW()
""")

# 顺带回填日线成交额(新浪K线无成交额字段, 东财快照 f6 单位元, 零额外请求)
AMOUNT_SQL = text("""
    UPDATE stock_daily_quote SET amount=:amount, etl_update_time=NOW()
    WHERE ts_code=:ts_code AND trade_date=:trade_date
""")

# 顺带补缺上市日期(仅写仍为 NULL 的行, 不覆盖新浪F10已解析值, 幂等)
LIST_DATE_SQL = text("""
    UPDATE stock_basic SET list_date=:list_date, etl_update_time=NOW()
    WHERE ts_code=:ts_code AND list_date IS NULL
""")


def _list_date(v):
    """f26: yyyymmdd 整数 -> 'yyyy-mm-dd'; 非法值返回 None"""
    s = str(v or "").strip().split(".")[0]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _ts_code(symbol: str) -> str:
    return symbol + (".SH" if symbol.startswith(("60", "68")) else ".SZ")


def _fetch_page(page: int) -> dict:
    last_err = None
    for _ in range(config.RETRY_TIMES):
        try:
            r = _session.get(EM_URL, params={
                "pn": page, "pz": PAGE_SIZE, "po": 0, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": FS, "fields": FIELDS,
            }, headers=HEADERS, timeout=15)
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise last_err


def sync_money_flow(limit: int = None) -> dict:
    """采集当日全市场资金流向快照并入库(按最近交易日记账)。
    limit: 调试用, 只写前 N 条。"""
    with engine.connect() as conn:
        trade_date = conn.execute(
            text("SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
        valid = {r[0] for r in conn.execute(
            text("SELECT ts_code FROM stock_basic WHERE is_active=1"))}
    if not trade_date:
        raise RuntimeError("stock_daily_quote 无数据, 请先执行 --quotes")

    rows, page, total = [], 1, None
    while True:
        payload = _fetch_page(page)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            break
        total = data.get("total", 0)
        diff = data.get("diff") or []
        if not diff:
            break
        for d in diff:
            code = str(d.get("f12") or "")
            ts_code = _ts_code(code)
            if ts_code not in valid:
                continue
            rows.append({
                "ts_code": ts_code, "trade_date": trade_date,
                "main_net": _num(d.get("f62")), "super_net": _num(d.get("f66")),
                "large_net": _num(d.get("f72")), "mid_net": _num(d.get("f78")),
                "small_net": _num(d.get("f84")), "main_pct": _num(d.get("f184")),
                "amount": _num(d.get("f6")), "list_date": _list_date(d.get("f26")),
            })
        if page * PAGE_SIZE >= total:
            break
        page += 1
        time.sleep(0.3)

    if limit:
        rows = rows[:limit]
    amount_rows, listdate_rows = 0, 0
    if rows:
        with engine.begin() as conn:
            conn.execute(UPSERT_SQL, rows)
            r = conn.execute(AMOUNT_SQL, [
                {"ts_code": x["ts_code"], "trade_date": x["trade_date"],
                 "amount": x["amount"]} for x in rows if x["amount"] is not None])
            amount_rows = r.rowcount
            r = conn.execute(LIST_DATE_SQL, [
                {"ts_code": x["ts_code"], "list_date": x["list_date"]}
                for x in rows if x["list_date"] is not None])
            listdate_rows = r.rowcount
    return {"trade_date": str(trade_date), "remote_total": total,
            "rows": len(rows), "pages": page, "amount_rows": amount_rows,
            "listdate_rows": listdate_rows}
