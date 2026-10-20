# -*- coding: utf-8 -*-
"""补跑 2026-09-23 每日更新中失败/未执行到位的步骤。
已完成(19:53 run): basic/quotes/valsnap/indicators/index/thsmoney/board/boardsynth/
                   boardmin/darkpool/darkind/ztpool/finreport
本脚本补跑: moneyflow/hotrank/margin/adjdaily/suspend/forecast/restricted/pledge/
            goodwill/holder_num/comment/gapfill/valtopup/valpctl/pick
幂等: 直接重跑即可。"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from daily_update import step  # noqa: E402


def main():
    t0 = time.time()
    summary = []

    from app.collector.moneyflow_collector import sync_money_flow
    ok, r = step("moneyflow 资金流向(重试)", sync_money_flow)
    summary.append(("moneyflow", ok))

    from app.collector.akshare_collector import sync_hot_rank, sync_margin_detail
    ok, r = step("hotrank 人气榜快照(重试)", sync_hot_rank)
    summary.append(("hotrank", ok))

    ok, r = step("margin 两融明细(重试)", lambda: sync_margin_detail(days=3))
    summary.append(("margin", ok))

    from app.collector.adjust_collector import refresh_adjust_events
    ok, r = step("adjdaily 复权因子增量(重试)", refresh_adjust_events)
    summary.append(("adjdaily", ok))

    from app.collector.suspend_collector import sync_suspend
    from app.collector.forecast_collector import sync_fin_forecast
    ok, r = step("suspend 停复牌快照", sync_suspend)
    summary.append(("suspend", ok))
    ok, r = step("forecast 业绩预告", sync_fin_forecast)
    summary.append(("forecast", ok))

    from app.collector.risk_collector import (
        sync_restricted_release, sync_pledge_ratio, sync_goodwill)
    ok, r = step("restricted 限售解禁", sync_restricted_release)
    summary.append(("restricted", ok))
    ok, r = step("pledge 股权质押", sync_pledge_ratio)
    summary.append(("pledge", ok))
    ok, r = step("goodwill 商誉", sync_goodwill)
    summary.append(("goodwill", ok))

    from app.collector.feature_collector import sync_holder_num, sync_comment
    ok, r = step("holder_num 股东户数", sync_holder_num)
    summary.append(("holder_num", ok))
    ok, r = step("comment 千股千评", sync_comment)
    summary.append(("comment", ok))

    def _gapfill():
        import subprocess
        r = subprocess.run([sys.executable, "oneoff/gapfill_amount_valuation.py"],
                           capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace", timeout=1800)
        if r.stdout:
            print(r.stdout[-2000:], flush=True)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "")[-500:])
        return {"ok": True}
    ok, r = step("gapfill 金额/估值缺口回填", _gapfill)
    summary.append(("gapfill", ok))

    from app.collector.basic_collector import topup_valuation_from_quote
    from app.collector.valuation_pctl import refresh_valuation_pctl
    ok, r = step("valtopup 估值日度顶格", topup_valuation_from_quote)
    summary.append(("valtopup", ok))
    ok, r = step("valpctl 估值分位落库", refresh_valuation_pctl)
    summary.append(("valpctl", ok))

    def _pick_all():
        from app.strategy.base import list_strategies
        from app.pick_service import run_pick, _load_universe
        import app.pick_service  # noqa: F401 注册策略
        universe = _load_universe()
        results, failed = [], []
        for s in list_strategies():
            try:
                results.append(run_pick(s["code"], universe=universe))
            except Exception as e:
                failed.append(f"{s['code']}: {e}")
        return {"ok": len(results), "failed": failed,
                "hits": {x["strategy"]: x["hit_count"] for x in results}}
    ok, r = step("pick 全部策略选股", _pick_all)
    summary.append(("pick", ok))

    print("\n" + "=" * 50, flush=True)
    print(f"补跑结束, 总耗时 {time.time() - t0:.0f}s", flush=True)
    for name, ok in summary:
        print(f"  {'✓' if ok else '✗'} {name}", flush=True)


if __name__ == "__main__":
    main()
