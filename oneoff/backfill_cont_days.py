# -*- coding: utf-8 -*-
"""连涨天数(cont_days)历史回填 —— 一次性/可按需重跑，幂等。

口径：连续上涨 = +n（连续 n 天收涨），连续下跌 = -n，平盘 = 0；
      昨天涨今天跌 -> -1，昨天跌今天涨 -> 1。
实现：全表按股票分组在 Python 侧按交易日顺序累计算连涨天数，
      写入临时表后用 UPDATE JOIN 一次性刷回（MySQL 5.5 无窗口函数，逐行 UPDATE 太慢）。

用法：python backfill_cont_days.py
"""
import math
import time

import pandas as pd
from sqlalchemy import text

from app.collector.quote_collector import cont_days_series
from app.db import engine

DDL = """
ALTER TABLE stock_daily_quote
    ADD COLUMN cont_days INT NULL COMMENT '连涨天数(正=连续上涨,负=连续下跌,0=平盘)'
    AFTER vol_ma20
"""


def ensure_column():
    with engine.connect() as c:
        n = c.execute(text("""
            SELECT COUNT(*) FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_daily_quote'
              AND COLUMN_NAME = 'cont_days'
        """)).scalar()
    if n:
        print("[cont_days] 列已存在, 跳过 ALTER", flush=True)
        return
    with engine.begin() as c:
        c.execute(text(DDL))
    print("[cont_days] 已新增列 cont_days INT", flush=True)


def main():
    ensure_column()
    t0 = time.time()
    df = pd.read_sql(text(
        "SELECT ts_code, trade_date, pct_chg FROM stock_daily_quote "
        "ORDER BY ts_code, trade_date"
    ), engine)
    print(f"[cont_days] 读取 {len(df)} 行, {time.time() - t0:.1f}s", flush=True)

    out = []
    for ts_code, g in df.groupby("ts_code", sort=False):
        pct = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
               for v in g["pct_chg"].tolist()]
        dates = g["trade_date"].tolist()
        out.extend((ts_code, d, v) for d, v in zip(dates, cont_days_series(pct)))
    print(f"[cont_days] 计算完成 {len(out)} 行, {time.time() - t0:.1f}s", flush=True)

    with engine.begin() as conn:
        conn.execute(text("DROP TEMPORARY TABLE IF EXISTS tmp_cont_days"))
        conn.execute(text("""
            CREATE TEMPORARY TABLE tmp_cont_days (
                ts_code   VARCHAR(16) NOT NULL,
                trade_date DATE       NOT NULL,
                cont_days  INT        NULL,
                UNIQUE KEY uk_code_date (ts_code, trade_date)
            ) ENGINE=InnoDB
        """))
        batch = 5000
        ins = text("INSERT INTO tmp_cont_days (ts_code, trade_date, cont_days) "
                   "VALUES (:tc, :td, :cd)")
        for i in range(0, len(out), batch):
            conn.execute(ins, [{"tc": a, "td": b, "cd": c} for a, b, c in out[i:i + batch]])
        print(f"[cont_days] 临时表写入完成, {time.time() - t0:.1f}s", flush=True)
        r = conn.execute(text("""
            UPDATE stock_daily_quote q
            JOIN tmp_cont_days t ON t.ts_code = q.ts_code AND t.trade_date = q.trade_date
            SET q.cont_days = t.cont_days
        """))
        print(f"[cont_days] UPDATE JOIN 影响 {r.rowcount} 行, {time.time() - t0:.1f}s", flush=True)
        conn.execute(text("DROP TEMPORARY TABLE tmp_cont_days"))

    with engine.connect() as c:
        nulls = c.execute(text(
            "SELECT COUNT(*) FROM stock_daily_quote WHERE cont_days IS NULL")).scalar()
        pos = c.execute(text(
            "SELECT COUNT(*) FROM stock_daily_quote WHERE cont_days > 0")).scalar()
        neg = c.execute(text(
            "SELECT COUNT(*) FROM stock_daily_quote WHERE cont_days < 0")).scalar()
    print(f"[cont_days] 完成: 空值 {nulls} 行 | 连涨 {pos} 行 | 连跌 {neg} 行 "
          f"| 总耗时 {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
