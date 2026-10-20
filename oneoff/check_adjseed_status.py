# -*- coding: utf-8 -*-
"""一次性核查: 复权因子 seed 后台任务的最终落库状态"""
import pymysql

conn = pymysql.connect(
    host="172.16.3.16", port=30306, user="admin",
    password="admin", database="test_project", charset="utf8mb4",
)
cur = conn.cursor()

# 1) 因子事件表总量与覆盖股票数
cur.execute(
    "SELECT COUNT(*), COUNT(DISTINCT ts_code), MIN(ex_date), MAX(ex_date) "
    "FROM stock_adjust_factor"
)
total, stocks, dmin, dmax = cur.fetchone()
print(f"stock_adjust_factor: rows={total} stocks={stocks} ex_date_range={dmin}~{dmax}")

# 2) 事件分布(按来源)
cur.execute("SELECT source, COUNT(*) FROM stock_adjust_factor GROUP BY source")
for et, n in cur.fetchall():
    print(f"  source={et}: {n}")

# 3) 大盘闸门用的日线最新日期(判断任务何时停更)
cur.execute("SELECT MAX(trade_date) FROM stock_daily_quote")
print(f"stock_daily_quote 最新交易日: {cur.fetchone()[0]}")

# 4) 茅台锚点
cur.execute(
    "SELECT ex_date, factor_before, factor_after, adj_pct "
    "FROM stock_adjust_factor WHERE ts_code='600519.SH' "
    "ORDER BY ex_date DESC LIMIT 3"
)
for row in cur.fetchall():
    print(f"  600519.SH {row}")

conn.close()
