# -*- coding: utf-8 -*-
"""一次性补数: 补齐 9/18、9/21 两个缺失交易日(daily_update 9/18 起未跑)

与 daily_update.py 的差异(都是有意为之):
  1) 日线走【区间模式】 start=2026-09-18 end=2026-09-21 —— 只写已收盘交易日,
     不碰今天(9/22 盘中)的日线, 避免盘中半根K线污染日表(今晚 bat 正常跑会覆盖)
  2) 跳过 moneyflow/thsmoney/valsnap —— 三者均为【当日快照】, 历史无法回填;
     9/18-9/21 的 amount 由 gapfill_amount_valuation.py(baostock) 单独补
  3) ztpool/margin/darkpool 传大 days 覆盖缺口; hotrank 快照只能取当下
顺序约束:
  index 必须最先跑 —— ztpool/margin/hotrank 的交易日历都读 stock_index_daily
幂等: 全链路 UPSERT, 可重跑
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def step(name, fn):
    print(f"\n===== [{time.strftime('%H:%M:%S')}] {name} =====", flush=True)
    try:
        r = fn()
        print(f"--> {name}: {r}", flush=True)
        return True, r
    except Exception as e:
        print(f"--> {name} 失败: {type(e).__name__}: {e}", flush=True)
        return False, e


def main():
    t0 = time.time()
    summary = []

    from app.collector.index_collector import sync_index_daily
    from app.collector.quote_collector import sync_daily_quotes
    from app.collector.indicator_collector import refresh as refresh_indicators
    from app.collector.akshare_collector import (
        sync_zt_pool, sync_hot_rank, sync_margin_detail)
    from app.collector.darkpool_collector import (
        sync_block_trade, sync_billboard, refresh_dark_indicator)
    from app.collector.adjust_collector import refresh_adjust_events
    from app.collector.board_synth import synth_board_daily

    # 1) 指数日线(刷新交易日历, 供 ztpool/margin/hotrank 使用)
    ok, r = step("index 指数日线", lambda: sync_index_daily(days=40))
    summary.append(("index", ok))

    # 2) 日线区间模式: 只补 9/18~9/21 两个已收盘交易日
    ok, r = step("quotes 日线 9/18-9/21", lambda: sync_daily_quotes(
        start_date="20260918", end_date="20260921"))
    summary.append(("quotes", ok and r.get("failed_count", 0) <= 50))

    # 3) 衍生指标(覆盖缺口日)
    ok, r = step("indicators 衍生指标", lambda: refresh_indicators(days=5))
    summary.append(("indicators", ok))

    # 4) 五池(9/18 + 9/21)
    ok, r = step("ztpool 五池", lambda: sync_zt_pool(days=4))
    summary.append(("ztpool", ok))

    # 5) 两融明细(交易所 T+1 发布, days=5 覆盖缺口)
    ok, r = step("margin 两融明细", lambda: sync_margin_detail(days=5))
    summary.append(("margin", ok))

    # 6) 暗盘(大宗+龙虎榜, days=7 覆盖缺口)
    ok, r = step("darkpool 大宗交易", lambda: sync_block_trade(days=7))
    summary.append(("darkpool_block", ok))
    ok, r = step("darkpool 龙虎榜", lambda: sync_billboard(days=7))
    summary.append(("darkpool_billboard", ok))
    ok, r = step("darkind 暗盘指标", refresh_dark_indicator)
    summary.append(("darkind", ok))

    # 7) 人气榜快照(只能取当下, 9/18-9/21 历史已永久丢失)
    ok, r = step("hotrank 人气榜快照", sync_hot_rank)
    summary.append(("hotrank", ok))

    # 8) 复权因子日常增量
    ok, r = step("adjdaily 复权因子增量", refresh_adjust_events)
    summary.append(("adjdaily", ok))

    # 9) 板块日线合成(从个股日线重算, 可覆盖缺口日)
    ok, r = step("boardsynth 板块日线合成", synth_board_daily)
    summary.append(("boardsynth", ok))

    print("\n" + "=" * 50, flush=True)
    print(f"补数完成, 总耗时 {time.time() - t0:.0f}s", flush=True)
    for name, ok in summary:
        print(f"  {'✓' if ok else '✗'} {name}", flush=True)


if __name__ == "__main__":
    main()
