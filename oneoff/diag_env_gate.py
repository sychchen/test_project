# -*- coding: utf-8 -*-
"""诊断 WEEKLY_MAINLINE 10/15 日回测窗口为空的原因:
1) 定位 days_ago=5/10/15 对应的 t0 交易日
2) 查 stock_index_daily 在各 t0 的 MA5/MA20, 验证指数环境门(死叉不开仓)是否触发
3) 若门通过, 再逐级统计 select() 候选存活数定位流失环节
"""
from sqlalchemy import text
from app.db import engine

with engine.connect() as conn:
    dates = [r[0] for r in conn.execute(text(
        "SELECT DISTINCT trade_date FROM stock_daily_quote ORDER BY trade_date DESC"
    )).fetchall()]
    print(f"总交易日数: {len(dates)}, 最新: {dates[0]}, 最早: {dates[-1]}")

    for days_ago in (5, 10, 15):
        idx = days_ago  # backtest 中 t0 = trade_dates[-(days_ago+1)], dates 已倒序故取下标 days_ago
        if idx >= len(dates):
            print(f"days_ago={days_ago}: 交易日不足, 跳过")
            continue
        t0 = dates[idx]
        row = conn.execute(text(
            "SELECT trade_date, ma5, ma20 FROM stock_index_daily "
            "WHERE index_code='sh000001' AND trade_date<=:t0 "
            "ORDER BY trade_date DESC LIMIT 1"), {"t0": t0}).fetchone()
        if not row:
            print(f"days_ago={days_ago}: t0={t0} 指数无数据 -> 门返回 None(放行)")
            continue
        d, ma5, ma20 = row
        if ma5 is None or ma20 is None:
            print(f"days_ago={days_ago}: t0={t0} 指数行{d} MA 为空 -> 门返回 None(放行)")
            continue
        ok = float(ma5) >= float(ma20)
        verdict = "放行(MA5>=MA20)" if ok else "*** 拦截(死叉, return []) ***"
        print(f"days_ago={days_ago}: t0={t0} 指数行{d} MA5={float(ma5):.2f} MA20={float(ma20):.2f} -> {verdict}")

    # 附带: 打印 8 月中下旬全部指数 MA 供交叉参考
    print("\n-- sh000001 MA5/MA20 (2026-08-10 起) --")
    rows = conn.execute(text(
        "SELECT trade_date, ma5, ma20 FROM stock_index_daily "
        "WHERE index_code='sh000001' AND trade_date>='2026-08-10' ORDER BY trade_date"
    )).fetchall()
    for d, ma5, ma20 in rows:
        if ma5 is None or ma20 is None:
            print(f"{d}: MA 缺失")
        else:
            flag = "" if float(ma5) >= float(ma20) else "  <死叉>"
            print(f"{d}: MA5={float(ma5):9.2f} MA20={float(ma20):9.2f}{flag}")
