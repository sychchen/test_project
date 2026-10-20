# -*- coding: utf-8 -*-
"""董监高及相关人员持股变动采集（Epic#3 akshare 覆盖补缺, 2026-10-19）

数据源: 东财 datacenter RPT_EXECUTIVE_HOLD_DETAILS（datacenter-web 域,
       与 darkpool/forecast 同源长期稳定; akshare stock_hold_management_detail_em 同源,
       但 akshare 封装是全量翻页且无 filter, 这里直连接口支持增量）
表: stock_share_change, uk(row_hash=全字段MD5) 幂等。
增量策略: 库内最大变动日-3天 起重叠拉取(覆盖写靠 hash 去重, 重叠防补录遗漏);
       首次全量 ~20-40 页(500/页)。
用途: 高管/大股东增持=信心信号, 减持=避雷信号, 供策略层二次过滤/加分。
"""
import datetime as dt
import hashlib
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

_FIELDS = ["SECURITY_CODE", "SECURITY_NAME", "CHANGE_DATE", "PERSON_NAME",
           "CHANGE_SHARES", "AVERAGE_PRICE", "CHANGE_AMOUNT", "CHANGE_REASON",
           "CHANGE_RATIO", "CHANGE_AFTER_HOLDNUM", "HOLD_TYPE",
           "DSE_PERSON_NAME", "POSITION_NAME", "PERSON_DSE_RELATION",
           "BEGIN_HOLD_NUM", "END_HOLD_NUM"]


def _ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stock_share_change (
                id BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
                ts_code VARCHAR(16) NOT NULL COMMENT '带市场后缀代码',
                sec_name VARCHAR(32) NULL COMMENT '证券名称',
                change_date DATE NOT NULL COMMENT '变动日期',
                person_name VARCHAR(64) NULL COMMENT '变动人',
                change_shares DECIMAL(20,2) NULL COMMENT '变动股数(正=增持,负=减持)',
                avg_price DECIMAL(12,3) NULL COMMENT '成交均价(元)',
                change_amount DECIMAL(24,2) NULL COMMENT '变动金额(元)',
                change_reason VARCHAR(128) NULL COMMENT '变动原因',
                change_ratio DECIMAL(12,6) NULL COMMENT '变动比例%',
                after_holdnum DECIMAL(20,2) NULL COMMENT '变动后持股数',
                hold_type VARCHAR(32) NULL COMMENT '持股种类',
                dse_person VARCHAR(64) NULL COMMENT '董监高人员姓名',
                position_name VARCHAR(64) NULL COMMENT '职务',
                person_relation VARCHAR(32) NULL COMMENT '变动人与董监高的关系',
                row_hash CHAR(32) NOT NULL COMMENT '全字段MD5(幂等键)',
                etl_insert_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
                etl_update_time TIMESTAMP NULL DEFAULT NULL COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_hash (row_hash),
                KEY idx_code_date (ts_code, change_date),
                KEY idx_date (change_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='董监高及相关人员持股变动(东财 RPT_EXECUTIVE_HOLD_DETAILS)'
        """))


def _ts_code(sec_code: str) -> str | None:
    if not sec_code:
        return None
    c = str(sec_code)
    if c.startswith("6"):
        return f"{c}.SH"
    if c.startswith(("0", "3")):
        return f"{c}.SZ"
    return f"{c}.BJ"   # 北交所等全量入库, 策略层自行过滤(与 forecast 同哲学)


def _fetch(since: str | None) -> list[dict]:
    """since=None 全量; 否则 filter CHANGE_DATE>=since 增量"""
    rows, page = [], 1
    while True:
        params = {
            "reportName": "RPT_EXECUTIVE_HOLD_DETAILS", "columns": "ALL",
            "quoteColumns": "", "sortColumns": "CHANGE_DATE,SECURITY_CODE,PERSON_NAME",
            "sortTypes": "-1,1,1", "pageSize": PAGE_SIZE, "pageNumber": page,
            "source": "WEB", "client": "WEB",
        }
        if since:
            params["filter"] = f"(CHANGE_DATE>='{since}')"
        last = None
        for _ in range(config.RETRY_TIMES):
            try:
                payload = _session.get(DC_URL, params=params,
                                       headers=HEADERS, timeout=20).json()
                last = None
                break
            except Exception as e:
                last = e
                time.sleep(2)
        if last is not None:
            raise last
        result = payload.get("result") or {}
        data = result.get("data") or []
        rows.extend(data)
        if page >= (result.get("pages") or 1) or not data:
            return rows
        page += 1
        time.sleep(REQ_INTERVAL)


def _upsert(raw: list[dict]) -> int:
    rows = []
    for x in raw:
        tc = _ts_code(x.get("SECURITY_CODE"))
        if not tc:
            continue
        cd = str(x.get("CHANGE_DATE") or "")[:10]
        if not cd or cd == "None":
            continue
        h = hashlib.md5("|".join(str(x.get(k)) for k in _FIELDS)
                        .encode("utf-8")).hexdigest()
        rows.append({
            "ts_code": tc,
            "sec_name": (x.get("SECURITY_NAME") or "")[:32] or None,
            "change_date": cd,
            "person_name": (x.get("PERSON_NAME") or "")[:64] or None,
            "change_shares": x.get("CHANGE_SHARES"),
            "avg_price": x.get("AVERAGE_PRICE"),
            "change_amount": x.get("CHANGE_AMOUNT"),
            "change_reason": (str(x.get("CHANGE_REASON") or ""))[:128] or None,
            "change_ratio": x.get("CHANGE_RATIO"),
            "after_holdnum": x.get("CHANGE_AFTER_HOLDNUM"),
            "hold_type": (x.get("HOLD_TYPE") or "")[:32] or None,
            "dse_person": (x.get("DSE_PERSON_NAME") or "")[:64] or None,
            "position_name": (x.get("POSITION_NAME") or "")[:64] or None,
            "person_relation": (x.get("PERSON_DSE_RELATION") or "")[:32] or None,
            "row_hash": h,
        })
    if not rows:
        return 0
    n = 0
    with engine.begin() as conn:
        B = 1000
        for i in range(0, len(rows), B):
            r = conn.execute(text("""
                INSERT IGNORE INTO stock_share_change
                    (ts_code, sec_name, change_date, person_name, change_shares,
                     avg_price, change_amount, change_reason, change_ratio,
                     after_holdnum, hold_type, dse_person, position_name,
                     person_relation, row_hash)
                VALUES
                    (:ts_code, :sec_name, :change_date, :person_name, :change_shares,
                     :avg_price, :change_amount, :change_reason, :change_ratio,
                     :after_holdnum, :hold_type, :dse_person, :position_name,
                     :person_relation, :row_hash)
            """), rows[i:i + B])
            n += r.rowcount
    return n


def sync_share_change(full: bool = False) -> dict:
    """董监高持股变动同步。full=True 全量翻页; 默认增量(库内最大日-3天重叠)。"""
    _ensure_table()
    since = None
    if not full:
        with engine.connect() as conn:
            mx = conn.execute(text(
                "SELECT MAX(change_date) FROM stock_share_change")).scalar()
        if mx:
            since = (mx - dt.timedelta(days=3)).strftime("%Y-%m-%d")
        # 库空 → since=None 自动转全量
    raw = _fetch(since)
    n = _upsert(raw)
    return {"mode": "full" if since is None else f"since {since}",
            "remote": len(raw), "rows": n}
