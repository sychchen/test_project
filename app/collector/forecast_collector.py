# -*- coding: utf-8 -*-
"""业绩预告采集（覆盖度审计 P2, 业绩预告驱动事件策略）
数据源: 东财 datacenter RPT_PUBLIC_OP_NEWPREDICT（datacenter.eastmoney.com/securities
       域, 2026-09-23 实测可用; akshare stock_yjyg_em 同源）
接口口径: 单请求=单报告期(filter REPORT_DATE='YYYY-MM-DD', ~10 页/期)。
表: stock_fin_forecast, uk(ts_code, report_date, announce_date, indicator)。
增量策略: 缺报告期的期数才拉(每季一个报告期, 平时日增 ~1 次探测);
       大规模历史回填断点 oneoff/forecast_progress.txt。
字段锚点(2026-09-23 探针 600187.SH 已核): PREDICT_TYPE=预告类型(扭亏等),
       PREDICT_AMT_LOWER/UPPER=预测净利区间(元), ADD_AMP_LOWER/UPPER=变动幅度%,
       PREYEAR_SAME_PERIOD=上年同期值, PREDICT_CONTENT/CHANGE_REASON_EXPLAIN=内容/原因。
"""
import os
import time

import requests
from sqlalchemy import text

from app import config
from app.db import engine

DC_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
PAGE_SIZE = 500
REQ_INTERVAL = 0.5
PROGRESS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "oneoff", "forecast_progress.txt")
QUARTER_ENDS = ("0331", "0630", "0930", "1231")

_session = requests.Session()
_session.trust_env = False


def _ensure_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stock_fin_forecast (
                id BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
                ts_code VARCHAR(16) NOT NULL COMMENT '带市场后缀代码',
                report_date DATE NOT NULL COMMENT '报告期',
                announce_date DATE NULL COMMENT '公告日期',
                indicator VARCHAR(64) NOT NULL COMMENT '预测指标(如归属于上市公司股东的净利润)',
                predict_type VARCHAR(32) NULL COMMENT '预告类型(预增/预减/扭亏/首亏...)',
                forecast_low DECIMAL(24,2) NULL COMMENT '预测数值下限(元)',
                forecast_high DECIMAL(24,2) NULL COMMENT '预测数值上限(元)',
                change_pct_low DECIMAL(12,4) NULL COMMENT '业绩变动幅度下限%',
                change_pct_high DECIMAL(12,4) NULL COMMENT '业绩变动幅度上限%',
                prev_value DECIMAL(24,2) NULL COMMENT '上年同期值(元)',
                content VARCHAR(1000) NULL COMMENT '预告内容',
                reason VARCHAR(1000) NULL COMMENT '变动原因',
                is_latest TINYINT NULL COMMENT '是否最新一次预告 1/0',
                etl_insert_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
                etl_update_time TIMESTAMP NULL DEFAULT NULL COMMENT '更新时间',
                PRIMARY KEY (id),
                UNIQUE KEY uk_code_rp_ann_ind (ts_code, report_date, announce_date, indicator),
                KEY idx_report (report_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='业绩预告(东财数据中心 RPT_PUBLIC_OP_NEWPREDICT)'
        """))


def _periods(start_year: int = 2016) -> list[str]:
    """报告期列表 YYYYMMDD(季度末), 从 start_year 至今, 升序。"""
    import datetime as dt
    today = dt.date.today()
    out = []
    for y in range(start_year, today.year + 1):
        for md in QUARTER_ENDS:
            d = f"{y}{md}"
            if d <= today.strftime("%Y%m%d"):
                out.append(d)
    return out


def _fetch_period(period: str) -> list[dict]:
    rows, page = [], 1
    ymd = f"{period[:4]}-{period[4:6]}-{period[6:]}"
    while True:
        params = {
            "sortColumns": "NOTICE_DATE,SECURITY_CODE", "sortTypes": "-1,-1",
            "pageSize": PAGE_SIZE, "pageNumber": page,
            "reportName": "RPT_PUBLIC_OP_NEWPREDICT", "columns": "ALL",
            "client": "WEB",
            "filter": "(REPORT_DATE='%s')" % ymd,
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


def _upsert(raw: list[dict], period: str) -> int:
    rp = f"{period[:4]}-{period[4:6]}-{period[6:]}"
    rows = []
    for x in raw:
        tc = str(x.get("SECUCODE") or "")
        if not tc.endswith((".SH", ".SZ")):
            continue
        rows.append({
            "ts_code": tc, "report_date": rp,
            "announce_date": (str(x.get("NOTICE_DATE"))[:10]
                              if x.get("NOTICE_DATE") else None),
            "indicator": (x.get("PREDICT_FINANCE") or "")[:64] or "净利润",
            "predict_type": (x.get("PREDICT_TYPE") or "")[:32] or None,
            "forecast_low": x.get("PREDICT_AMT_LOWER"),
            "forecast_high": x.get("PREDICT_AMT_UPPER"),
            "change_pct_low": x.get("ADD_AMP_LOWER"),
            "change_pct_high": x.get("ADD_AMP_UPPER"),
            "prev_value": x.get("PREYEAR_SAME_PERIOD"),
            "content": (x.get("PREDICT_CONTENT") or "")[:1000] or None,
            "reason": (x.get("CHANGE_REASON_EXPLAIN") or "")[:1000] or None,
            "is_latest": 1 if x.get("IS_LATEST") == "T" else 0,
        })
    if not rows:
        return 0
    with engine.begin() as conn:
        B = 1000
        for i in range(0, len(rows), B):
            conn.execute(text("""
                INSERT INTO stock_fin_forecast
                    (ts_code, report_date, announce_date, indicator, predict_type,
                     forecast_low, forecast_high, change_pct_low, change_pct_high,
                     prev_value, content, reason, is_latest)
                VALUES (:ts_code, :report_date, :announce_date, :indicator, :predict_type,
                        :forecast_low, :forecast_high, :change_pct_low, :change_pct_high,
                        :prev_value, :content, :reason, :is_latest)
                ON DUPLICATE KEY UPDATE
                    predict_type=VALUES(predict_type),
                    forecast_low=VALUES(forecast_low), forecast_high=VALUES(forecast_high),
                    change_pct_low=VALUES(change_pct_low), change_pct_high=VALUES(change_pct_high),
                    prev_value=VALUES(prev_value), content=VALUES(content),
                    reason=VALUES(reason), is_latest=VALUES(is_latest),
                    etl_update_time=NOW()
            """), rows[i:i + B])
    return len(rows)


def sync_fin_forecast(periods: int = 1, start_year: int = None) -> dict:
    """业绩预告增量: 默认只补最新报告期; start_year 给定时全量回填(带断点)。

    幂等: uk 四键 upsert; 同一公司同报告期多次预告按公告日期+指标天然分行为。
    """
    _ensure_table()
    done = set()
    if os.path.exists(PROGRESS):
        done = {l.strip() for l in open(PROGRESS, encoding="utf-8") if l.strip()}
    if start_year:
        todo = [p for p in _periods(start_year) if p not in done]
    else:
        todo = _periods()[-periods:]
        todo = [p for p in todo if p not in done]
    if not todo:
        return {"periods": 0, "rows": 0, "note": "报告期均已覆盖"}
    total = 0
    for p in todo:
        n = _upsert(_fetch_period(p), p)
        total += n
        done.add(p)
        with open(PROGRESS, "a", encoding="utf-8") as f:
            f.write(p + "\n")
        print(f"[forecast] {p}: {n} 行", flush=True)
    return {"periods": len(todo), "rows": total,
            "range": f"{todo[0]}~{todo[-1]}"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="业绩预告采集")
    ap.add_argument("--periods", type=int, default=1, help="补最近 N 个报告期")
    ap.add_argument("--start-year", type=int, default=None,
                    help="历史回填起始年(如 2016), 给定时忽略 --periods")
    a = ap.parse_args()
    print(sync_fin_forecast(periods=a.periods, start_year=a.start_year))
