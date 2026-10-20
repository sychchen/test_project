# -*- coding: utf-8 -*-
"""CLI: 选股入口
用法:
  python run_pick.py --list                 列出可用策略
  python run_pick.py --all                  执行全部已注册策略(任一失败则退出码非零)
  python run_pick.py --strategy MA_BULL     执行指定策略
  python run_pick.py --strategy VOLUME_BREAKOUT --params "{\"vol_multiple\": 2.5}"
"""
import argparse
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.pick_service import run_pick, _load_universe
from app.strategy.base import list_strategies
import app.pick_service  # noqa: F401  确保策略注册


def main():
    ap = argparse.ArgumentParser(description="选股系统-策略选股")
    ap.add_argument("--list", action="store_true", help="列出策略")
    ap.add_argument("--all", action="store_true", help="执行全部已注册策略")
    ap.add_argument("--strategy", type=str, help="策略代码")
    ap.add_argument("--params", type=str, default=None, help="策略参数JSON")
    args = ap.parse_args()

    if args.list:
        for s in list_strategies():
            print(f"  {s['code']:<18} {s['name']:<8} params={s['default_params']}")
        return

    params = json.loads(args.params) if args.params else None

    if args.all:
        # 遍历注册表, 不枚举代码 -> 新增策略自动纳入, 杜绝静默漏跑
        # 数据(126万行日线+基本面)只加载一次, 全策略共享
        strategies = list_strategies()
        ok, failed = 0, []
        universe = _load_universe()
        for s in strategies:
            code = s["code"]
            try:
                r = run_pick(code, params, universe=universe)
                print(f"[pick] {r['strategy']} batch={r['batch_no']} "
                      f"trade_date={r['trade_date']} hits={r['hit_count']}", flush=True)
                ok += 1
            except Exception as e:
                failed.append(code)
                print(f"[pick] {code} FAILED: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
        total = len(strategies)
        print(f"[pick] all done: {ok}/{total} ok"
              + (f", failed={failed}" if failed else ""))
        if failed:
            sys.exit(1)
        return

    if not args.strategy:
        ap.error("请指定 --strategy、--all 或 --list")

    r = run_pick(args.strategy.upper(), params)
    print(f"[pick] {r['strategy']} batch={r['batch_no']} trade_date={r['trade_date']} hits={r['hit_count']}")


if __name__ == "__main__":
    main()
