# -*- coding: utf-8 -*-
"""每日数据更新 + 全策略选股 一键流水线
链路: basic -> quotes(腾讯主源 + 新浪兜底, 双源熔断) -> valsnap
     -> indicators(连涨天数/连涨涨幅/量比/换手率) -> index(新浪主源 + 腾讯兜底)
     -> moneyflow -> thsmoney(同花顺交叉验证) -> board(板块基础) -> boardsynth(板块日线合成) -> boardmin(板块分时)
     -> darkpool(大宗+龙虎榜+席位) -> darkind(暗盘指标)
     -> ztpool(涨停/强势/跌停/炸板/昨涨停五池, 东财/akshare) -> finreport(业绩报表批量)
     -> hotrank(人气榜Top100) -> margin(两融明细) -> adjdaily(复权因子事件增量)
     -> valtopup(估值日度顶格) -> valpctl(估值分位落库) -> pick --all(共享数据加载)
     -> suspend(停复牌) -> forecast(业绩预告) -> restricted(解禁) -> pledge(质押) -> goodwill(商誉)
     -> holder_num(股东户数) -> comment(千股千评)
特点:
  - 任一数据源反爬自动降级(quotes / index 均为双源); quotes 大面积失败时重试一次
  - 估值快照在补缺后重跑一次, 保证当日 pe_ttm 覆盖完整
  - 各步骤失败不中断后续步骤, 末尾汇总; 关键步骤失败退出码非零
注: baostock 通道 2026-09-15 实测整体不可用(`query_*` 返回成功但 `rs.next()` 永久阻塞),
    原有的 gap_fill_quotes / gap_fill_index(baostock) 兜底已移除, 改由采集器内部双源承担。
用法: python daily_update.py [--skip-pick] [--days 2]
"""
import argparse
import os
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def step(name, fn):
    """执行单步骤, 返回 (是否成功, 结果dict/异常)"""
    print(f"\n===== [{time.strftime('%H:%M:%S')}] {name} =====", flush=True)
    try:
        r = fn()
        print(f"--> {name} 结果: {r}", flush=True)
        return True, r
    except Exception as e:
        print(f"--> {name} 失败: {type(e).__name__}: {e}", flush=True)
        return False, e


def _detect_quote_gap() -> int:
    """日线缺口探测: 以指数日线为交易日历, 数出 stock_daily_quote 落后多少个交易日。
    指数日线每次固定拉 40 天, 即使个股日线久未更新也能给出可靠的交易日序列;
    两表同样落后(极端: 长期未跑)时探测为 0, 退回默认窗口。上限 60 防失控。"""
    from sqlalchemy import text
    from app.db import engine
    with engine.connect() as c:
        q_max = c.execute(text("SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
        i_max = c.execute(text("SELECT MAX(trade_date) FROM stock_index_daily")).scalar()
        if not q_max or not i_max or i_max <= q_max:
            return 0
        n = c.execute(text(
            "SELECT COUNT(DISTINCT trade_date) FROM stock_index_daily "
            "WHERE trade_date > :q"), {"q": q_max}).scalar()
    return min(int(n or 0), 60)


def main():
    ap = argparse.ArgumentParser(description="每日更新+选股一键流水线")
    ap.add_argument("--skip-pick", action="store_true", help="只更新数据, 不执行选股")
    ap.add_argument("--days", type=int, default=2, help="日线增量天数(默认2)")
    args = ap.parse_args()

    from app.collector.basic_collector import sync_stock_basic, snapshot_valuation
    from app.collector.quote_collector import sync_daily_quotes
    from app.collector.index_collector import sync_index_daily
    from app.collector.moneyflow_collector import sync_money_flow

    t0 = time.time()
    summary = []

    # 1) 基本面(股票列表+估值)
    ok, r = step("basic 股票列表+基本面", sync_stock_basic)
    summary.append(("basic", ok))

    # 2) 日线(腾讯主源, 新浪兜底, 双源熔断)
    #    缺口自适应: 漏跑多天时自动把增量窗口扩到落后交易日数(以指数日线为交易日历)
    gap = _detect_quote_gap()
    eff_days = max(args.days, gap)
    if gap > args.days:
        print(f"  [quotes] 库内日线落后 {gap} 个交易日, 增量窗口自动扩至 {eff_days} 天 "
              f"(如需更久历史可用 --days N 或 start_date 区间模式)", flush=True)
    ok, r = step("quotes 日线行情", lambda: sync_daily_quotes(days=eff_days))
    need_gapfill = ok and r.get("failed_count", 0) > 50
    summary.append(("quotes", ok and not need_gapfill))

    # 2b) 大面积失败 -> 重试一次
    #     (腾讯主源 + 新浪兜底已在 quote_collector 内部逐票尝试; 旧 baostock 补缺通道已弃用)
    if need_gapfill:
        print(f"\n===== quotes 失败 {r['failed_count']} 只, 重试一次 =====", flush=True)
        ok2, r2 = step("quotes 重试", lambda: sync_daily_quotes(days=args.days))
        if ok2:
            print(f"  重试后失败 {r2.get('failed_count')} 只", flush=True)
        summary.append(("quotes 重试", ok2))

    # 3) 估值快照(须在 quotes/gapfill 之后, 当日行已存在)
    ok, r = step("valsnap 估值快照", snapshot_valuation)
    summary.append(("valsnap", ok))

    # 3b) 日频衍生指标(连涨天数/连涨涨幅/量比/换手率; 换手率依赖 circ_mv, 须在 valsnap 之后)
    from app.collector.indicator_collector import refresh as refresh_indicators
    ok, r = step("indicators 衍生指标", lambda: refresh_indicators(days=max(3, eff_days)))
    summary.append(("indicators", ok))

    # 4) 指数日线(新浪主源 + 腾讯兜底, 均在 index_collector 内部按指数逐个尝试)
    ok, r = step("index 指数日线", lambda: sync_index_daily(days=40))
    idx_failed = r.get("failed_count", 0) if ok else 7
    if ok and r.get("tx_fallback"):
        print(f"  [index] 新浪不可用, 腾讯兜底生效: {r['tx_fallback']}", flush=True)
    if idx_failed > 0:
        print(f"\n===== index 失败 {idx_failed}/7 (新浪主源 + 腾讯兜底均已尝试) =====",
              flush=True)
        if ok:
            print(f"  失败明细: {r.get('failed_list')}", flush=True)
        summary.append(("index", False))
    else:
        summary.append(("index", ok))

    # 5) 资金流向(东财延时通道, 顺带回填成交额)
    ok, r = step("moneyflow 资金流向", sync_money_flow)
    flow_ok = ok and (r.get("rows", 0) if ok else 0) > 0

    # 5-fallback) 东财被拦时走新浪 MoneyFlow 增量回填(策略主口径 stock_money_flow 的历史源,
    #             INSERT IGNORE 只补缺口幂等; 2026-10-09 实测 105~167s/2000~3400 只零失败)
    if not flow_ok:
        def _mf_sina_fallback():
            import subprocess
            from app.db import engine as _eng
            from sqlalchemy import text as _text
            with _eng.connect() as _c:
                last = _c.execute(_text(
                    "SELECT MAX(trade_date) FROM stock_money_flow")).scalar()
                nxt = (last + timedelta(days=1)).isoformat() if last else None
            if not nxt:
                return {"rows": 0, "note": "no baseline"}
            root_ = os.path.dirname(os.path.abspath(__file__))
            r_ = subprocess.run(
                [sys.executable, "oneoff/backfill_money_flow.py",
                 "--start", nxt, "--max-requests", "5500"],
                capture_output=True, text=True, cwd=root_,
                encoding="utf-8", errors="replace", timeout=3600)
            if r_.stdout:
                print(r_.stdout[-800:], flush=True)
            if r_.returncode != 0:
                raise RuntimeError((r_.stderr or "sina backfill failed")[-300:])
            return {"rows": 1, "start": nxt}
        try:
            ok2, r2 = step("moneyflow-sina 新浪资金流增量(东财被拦fallback)", _mf_sina_fallback)
            flow_ok = ok2
        except Exception:
            flow_ok = False
    summary.append(("moneyflow", flow_ok))

    # 5-2) 资金流历史回填(探测式, 任务#11): push2his 自 09-23 起 WAF 间歇拦截,
    #      脚本内置 3 次探针重试(被拦时最多多花 ~90s), 窗口打开即断点续跑补历史
    def _mfhist():
        import subprocess
        root_ = os.path.dirname(os.path.abspath(__file__))
        r_ = subprocess.run([sys.executable, "oneoff/moneyflow_history_backfill.py"],
                            capture_output=True, text=True, cwd=root_,
                            encoding="utf-8", errors="replace", timeout=3600)
        if r_.stdout:
            print(r_.stdout[-1500:], flush=True)
        if r_.returncode not in (0,):
            raise RuntimeError((r_.stderr or "blocked/unfinished")[-300:])
        return {"ok": True}
    ok, r = step("mfhist 资金流历史回填(探测式)", _mfhist)
    summary.append(("mfhist", ok))

    # 4b) 同花顺资金流快照(与新浪交叉验证, akshare)
    from app.collector.akshare_collector import sync_money_flow_ths
    ok, r = step("thsmoney 同花顺资金流", sync_money_flow_ths)
    summary.append(("thsmoney", ok))

    # 5b-0) 个股分时采集(2026-10-19 补: 本流水线此前缺这一步, 导致 stock_minute_quote
    #        停在 09-16、下游板块分时只能合成到 09-16; days=3 自愈漏跑)
    from app.collector.minute_collector import sync_minute_quotes
    ok, r = step("minutes 个股分时", lambda: sync_minute_quotes(scale=5, days=3))
    summary.append(("minutes", ok))

    # 5b) 板块行情(新浪行业: 快照+成分映射+人气 -> 日线合成 -> 分时合成)
    from app.collector.board_collector import sync_board_basic
    from app.collector.board_synth import synth_board_daily, synth_board_minutes
    ok, r = step("board 板块基础", sync_board_basic)
    summary.append(("board", ok))
    ok, r = step("boardsynth 板块日线合成", synth_board_daily)
    summary.append(("boardsynth", ok))
    ok, r = step("boardmin 板块分时合成", synth_board_minutes)
    summary.append(("boardmin", ok))

    # 5c) 暗盘(大宗交易+龙虎榜+席位 -> 派生指标)
    from app.collector.darkpool_collector import (
        sync_block_trade, sync_billboard, refresh_dark_indicator)
    ok, r = step("darkpool 大宗交易", lambda: sync_block_trade(days=7))
    summary.append(("darkpool_block", ok))
    ok, r = step("darkpool 龙虎榜", lambda: sync_billboard(days=7))
    summary.append(("darkpool_billboard", ok))
    ok, r = step("darkind 暗盘指标", refresh_dark_indicator)
    summary.append(("darkind", ok))

    # 5d) 涨停池+强势股池+跌停/炸板/昨涨停(东财, akshare vendored 源码)
    from app.collector.akshare_collector import (
        sync_zt_pool, sync_fin_report, sync_hot_rank, sync_margin_detail)
    ok, r = step("ztpool 五池采集", lambda: sync_zt_pool(days=1))
    summary.append(("ztpool", ok))

    # 5d-b) 业绩报表(最新报告期批量页翻, ~6s) + 复权因子日常增量(零请求预警)
    ok, r = step("finreport 业绩报表", lambda: sync_fin_report(quarters=1))
    ok, r = step("hotrank 人气榜快照", sync_hot_rank)
    # days=2: 两融 T+1 发布, 当日盘中跑批查当日必得 0 行, 次日数据须回看一天
    ok, r = step("margin 两融明细", lambda: sync_margin_detail(days=2))
    summary.append(("finreport", ok))

    # 5d-b1) 董监高持股变动(东财 datacenter, 增量; 2026-10-19 Epic#3 补缺接入)
    from app.collector.share_change_collector import sync_share_change
    ok, r = step("sharechange 董监高持股变动", sync_share_change)
    summary.append(("sharechange", ok))
    from app.collector.adjust_collector import refresh_adjust_events
    ok, r = step("adjdaily 复权因子增量", refresh_adjust_events)
    summary.append(("adjdaily", ok))

    # 5d-b2) 停复牌快照(审计P1) + 业绩预告(审计P2): 缺日/缺期才拉, 平时空转
    from app.collector.suspend_collector import sync_suspend
    from app.collector.forecast_collector import sync_fin_forecast
    ok, r = step("suspend 停复牌快照", sync_suspend)
    summary.append(("suspend", ok))
    ok, r = step("forecast 业绩预告", sync_fin_forecast)
    summary.append(("forecast", ok))

    # 5d-b3) 风险排雷三表(审计P5/P6): 解禁事件滚动窗口 + 质押周频探测 + 商誉缺期
    from app.collector.risk_collector import (
        sync_restricted_release, sync_pledge_ratio, sync_goodwill)
    ok, r = step("restricted 限售解禁", sync_restricted_release)
    summary.append(("restricted", ok))
    ok, r = step("pledge 股权质押", sync_pledge_ratio)
    summary.append(("pledge", ok))
    ok, r = step("goodwill 商誉", sync_goodwill)
    summary.append(("goodwill", ok))

    # 5d-b4) 特色数据(审计P4/P8): 股东户数最新期探测 + 千股千评日快照
    from app.collector.feature_collector import sync_holder_num, sync_comment
    ok, r = step("holder_num 股东户数", sync_holder_num)
    summary.append(("holder_num", ok))
    ok, r = step("comment 千股千评", sync_comment)
    summary.append(("comment", ok))

    # 5d-c) 金额/估值缺口回填(加固): 先零成本探测近 30 日是否有
    #       amount NULL / 估值缺日的交易日, 有缺口才走 baostock 补(平时空转 ~1s)。
    #       防 moneyflow/valsnap 快照步骤单日失败造成的永久性历史缺口。
    def _gapfill():
        import subprocess
        root = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, "oneoff/gapfill_amount_valuation.py"],
                           capture_output=True, text=True, cwd=root,
                           encoding="utf-8", errors="replace", timeout=1800)
        if r.stdout:
            print(r.stdout[-2000:], flush=True)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "")[-500:])
        return {"ok": True}
    ok, r = step("gapfill 金额/估值缺口回填", _gapfill)
    summary.append(("gapfill", ok))

    # 5e) 估值日度顶格 + 分位落库(供 VALUE_PCTL 策略读表秒出; valtopup 须在 valsnap 后)
    from app.collector.basic_collector import topup_valuation_from_quote
    from app.collector.valuation_pctl import refresh_valuation_pctl
    ok, r = step("valtopup 估值日度顶格", topup_valuation_from_quote)
    summary.append(("valtopup", ok))
    ok, r = step("valpctl 估值分位落库", refresh_valuation_pctl)
    summary.append(("valpctl", ok))

    # 5f) 本地衍生: 筹码分布(P7 换手率衰减法, 零外部请求) + 技术形态榜单(P9, 零外部请求)
    from app.collector.cyq_collector import sync_cyq
    from app.collector.feature_collector import sync_rank_deriv
    ok, r = step("cyq 筹码分布(本地衍生)", sync_cyq)
    summary.append(("cyq", ok))
    ok, r = step("rank_deriv 形态榜单(本地衍生)", sync_rank_deriv)
    summary.append(("rank_deriv", ok))

    # 6) 全策略选股(共享数据加载)
    if not args.skip_pick:
        def _pick_all():
            from app.strategy.base import list_strategies
            from app.strategy.config_store import is_enabled
            from app.pick_service import run_pick, _load_universe
            import app.pick_service  # noqa: F401 注册策略
            universe = _load_universe()
            results, failed, skipped = [], [], []
            for s in list_strategies():
                if not is_enabled(s["code"]):   # stock_strategy_config.enabled=0 跳过
                    skipped.append(s["code"])
                    continue
                try:
                    results.append(run_pick(s["code"], universe=universe))
                except Exception as e:
                    failed.append(f"{s['code']}: {e}")
            return {"ok": len(results), "failed": failed, "skipped": skipped,
                    "hits": {x["strategy"]: x["hit_count"] for x in results}}
        ok, r = step("pick 全部策略选股", _pick_all)
        summary.append(("pick", ok))

    # 汇总
    elapsed = time.time() - t0
    print("\n" + "=" * 50, flush=True)
    print(f"每日更新完成, 总耗时 {elapsed:.0f}s", flush=True)
    for name, ok in summary:
        print(f"  {'✓' if ok else '✗'} {name}", flush=True)
    if any(not ok for _, ok in summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
