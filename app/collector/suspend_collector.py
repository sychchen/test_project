# -*- coding: utf-8 -*-
"""停复牌信息采集（覆盖度审计 P1, 修复回测幸存者偏差）
数据源: 东财 datacenter-web RPT_CUSTOM_SUSPEND_DATA_INTERFACE（报表域, 与被封的
       push2his 不同域, 2026-09-23 实测可用; akshare stock_tfp_em 同源）

接口语义实测(2026-09-23, 重要):
  filter (MARKET="全部")(DATETIME='D') 返回的是「SUSPEND_END >= D(含未复牌)的全部
  停牌区间记录」, 而非「D 当日停牌快照」—— 未来才开始的停牌也会出现在历史 D 的
  返回里(实证: DATETIME='2025-09-24' 返回 527 行, 含 2026-04-30 起停的 37 只)。
  故不可按 akshare 的「逐日查询当快照」用法落库, 否则把未来停牌记到历史日。

正确落库口径: 以窗口起点 D0 拉全量区间记录(每条含 起始/截止/预计复牌),
  本地把区间 [start, end|最新交易日] ∩ 库内交易日 展开为逐日行:
  stock_trade_suspend(ts_code, trade_date) 一行=该股该日处于停牌, 供回测剔除。
表: uk(ts_code, trade_date), 幂等 upsert, 可全量重建。
锚点校验: 展开后日行数应平滑(数十只/日); 2026-09-23 实测当日 13 只与页面一致。
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


def _ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stock_trade_suspend (
                id BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
                ts_code VARCHAR(16) NOT NULL COMMENT '带市场后缀代码, 如 600519.SH',
                trade_date DATE NOT NULL COMMENT '信息日期(该日处于停牌)',
                suspend_start DATE NULL COMMENT '停牌开始日期',
                suspend_end DATE NULL COMMENT '停牌截止时间',
                suspend_term VARCHAR(64) NULL COMMENT '停牌期限',
                reason VARCHAR(255) NULL COMMENT '停牌原因',
                market VARCHAR(32) NULL COMMENT '所属市场',
                resume_plan DATE NULL COMMENT '预计复牌时间',
                etl_insert_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
                etl_update_time TIMESTAMP NULL DEFAULT NULL COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_code_date (ts_code, trade_date),
                KEY idx_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='停复牌信息(东财数据中心区间展开, 修复回测幸存者偏差)'
        """))


def _fetch_records(anchor: str) -> list[dict]:
    """拉取 SUSPEND_END >= anchor 的全部停牌区间记录(分页)。"""
    rows, page = [], 1
    while True:
        params = {
            "sortColumns": "SUSPEND_START_DATE", "sortTypes": "-1",
            "pageSize": PAGE_SIZE, "pageNumber": page,
            "reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE", "columns": "ALL",
            "source": "WEB", "client": "WEB",
            "filter": "(MARKET=\"全部\")(DATETIME='%s')" % anchor,
        }
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


def _norm_date(v):
    return str(v)[:10] if v else None


def sync_suspend(days: int = 10, start_date: str = None,
                 end_date: str = None) -> dict:
    """停复牌落库(区间展开口径)。

    窗口: 库内日线最近 N 个交易日(days=None/0 时按 start_date~end_date 全量),
    以窗口起点为 anchor 拉区间记录, 本地展开为逐日行后 upsert。幂等可重跑,
    亦可用于全量重建(先 TRUNCATE stock_trade_suspend 再以 --start 跑)。
    """
    _ensure_table()
    lim = days if days else 2000
    with engine.connect() as conn:
        dates = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT trade_date FROM stock_daily_quote "
            "ORDER BY trade_date DESC LIMIT :n"), {"n": lim})]
        pool = {r[0] for r in conn.execute(text(
            "SELECT ts_code FROM stock_basic WHERE is_active=1"))}
    dates = sorted(dates)
    if start_date:
        dates = [d for d in dates if str(d) >= start_date]
    if end_date:
        dates = [d for d in dates if str(d) <= end_date]
    if not dates:
        return {"rows": 0, "note": "窗口内无库内交易日"}
    lo, hi = dates[0], dates[-1]
    date_set = {str(d): d for d in dates}
    raw = _fetch_records(str(lo))
    rows = []
    for x in raw:
        tc = str(x.get("SECUCODE") or "")
        if tc not in pool:
            continue
        s = _norm_date(x.get("SUSPEND_START_DATE"))
        e = _norm_date(x.get("SUSPEND_END_TIME")) or str(hi)  # 未复牌 -> 展开到最新
        if not s or s > str(hi) or e < str(lo):
            continue
        s = max(s, str(lo))
        e = min(e, str(hi))
        common = {
            "suspend_start": _norm_date(x.get("SUSPEND_START_DATE")),
            "suspend_end": _norm_date(x.get("SUSPEND_END_TIME")),
            "suspend_term": (x.get("SUSPEND_EXPIRE") or "")[:64] or None,
            "reason": (x.get("SUSPEND_REASON") or "")[:255] or None,
            "market": (x.get("TRADE_MARKET") or "")[:32] or None,
            "resume_plan": _norm_date(x.get("PREDICT_RESUME_DATE")),
        }
        d = s
        while d <= e:
            if d in date_set:
                rows.append({"ts_code": tc, "trade_date": d, **common})
            d = str((dt.date.fromisoformat(d) + dt.timedelta(days=1)))
    if not rows:
        return {"rows": 0, "records": len(raw), "range": f"{lo}~{hi}"}
    with engine.begin() as conn:
        B = 1000
        for i in range(0, len(rows), B):
            conn.execute(text("""
                INSERT INTO stock_trade_suspend
                    (ts_code, trade_date, suspend_start, suspend_end,
                     suspend_term, reason, market, resume_plan)
                VALUES (:ts_code, :trade_date, :suspend_start, :suspend_end,
                        :suspend_term, :reason, :market, :resume_plan)
                ON DUPLICATE KEY UPDATE
                    suspend_start=VALUES(suspend_start), suspend_end=VALUES(suspend_end),
                    suspend_term=VALUES(suspend_term), reason=VALUES(reason),
                    market=VALUES(market), resume_plan=VALUES(resume_plan),
                    etl_update_time=NOW()
            """), rows[i:i + B])
    return {"rows": len(rows), "records": len(raw), "range": f"{lo}~{hi}"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="停复牌采集(区间展开口径)")
    ap.add_argument("--days", type=int, default=10, help="回看最近 N 个日线交易日")
    ap.add_argument("--start", type=str, default=None, help="窗口起点 YYYY-MM-DD")
    ap.add_argument("--end", type=str, default=None)
    a = ap.parse_args()
    print(sync_suspend(days=None if a.start else a.days,
                       start_date=a.start, end_date=a.end))
