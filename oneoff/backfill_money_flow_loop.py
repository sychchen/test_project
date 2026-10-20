# -*- coding: utf-8 -*-
"""资金流历史回填驱动器 —— 自动分段续跑直到无缺口。

背景: 新浪 MoneyFlow.ssl_qsfx_zjlrqs 单窗口约 400 次请求后触发 IP 限流
      (返回 456 挑战页, 约 5 分钟恢复), 无法一次跑完全市场 5218 只。
做法: 反复调用 backfill_money_flow.py(INSERT IGNORE, 天然断点续跑):
      每轮跑 max_requests 次请求 -> 预算耗尽或熔断 -> 探测源恢复 -> 下一轮;
      直到 [剩余缺口] = 0 或达到 --max-rounds。

用法:
  python backfill_money_flow_loop.py                          # 跑到补完
  python backfill_money_flow_loop.py --start 2025-09-09
  python backfill_money_flow_loop.py --round-requests 380 --workers 8 --max-rounds 30
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
REMAIN_RE = re.compile(r"\[剩余缺口\]\s*(\d+)")
MY_REMAIN_RE = re.compile(r"\[本片剩余\]\s*(\d+)")


def probe(host="vip") -> bool:
    r = subprocess.run([PY, os.path.join(BASE, "backfill_money_flow.py"), "--probe",
                        "--host", host],
                       cwd=BASE, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    return '"errors": []' in (r.stdout or "") and '"n": 400' in (r.stdout or "")


def wait_recovery(max_wait=600, step=60, host="vip") -> bool:
    """轮询直到源恢复(最多 max_wait 秒)。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if probe(host):
            print(f"[loop] 源已恢复 (等待 {int(time.time() - t0)}s)", flush=True)
            return True
        print(f"[loop] 源仍限流, {step}s 后重试 (已等 {int(time.time() - t0)}s)", flush=True)
        time.sleep(step)
    return False


def main():
    ap = argparse.ArgumentParser(description="资金流历史回填自动续跑驱动器")
    ap.add_argument("--start", default="2025-09-09")
    ap.add_argument("--end", default=None)
    ap.add_argument("--host", default="vip", choices=["vip", "money"],
                    help="接口域名；两个域名限流独立，可多实例分片并行")
    ap.add_argument("--shard", default=None, metavar="I/N", help="分片: 第 I 片(0 基)/共 N 片")
    ap.add_argument("--round-requests", type=int, default=420, help="每轮请求预算(按真实HTTP请求计数)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--interval", type=float, default=0.05, help="全局请求间隔(秒, 跨进程共享节奏)")
    ap.add_argument("--fail-threshold", type=int, default=3)
    ap.add_argument("--cooldown", type=float, default=200.0)
    ap.add_argument("--max-rounds", type=int, default=60)
    ap.add_argument("--max-wait", type=int, default=900, help="单次等待恢复上限(秒)")
    args = ap.parse_args()

    t_all = time.time()
    for rnd in range(1, args.max_rounds + 1):
        cmd = [PY, os.path.join(BASE, "backfill_money_flow.py"),
               "--start", args.start, "--workers", str(args.workers),
               "--interval", str(args.interval),
               "--max-requests", str(args.round_requests),
               "--fail-threshold", str(args.fail_threshold),
               "--cooldown", str(args.cooldown)]
        if args.end:
            cmd += ["--end", args.end]
        cmd += ["--host", args.host]
        if args.shard:
            cmd += ["--shard", args.shard]
        print(f"\n===== [loop] 第 {rnd}/{args.max_rounds} 轮  "
              f"{time.strftime('%H:%M:%S')}  host={args.host} shard={args.shard or '-'} =====",
              flush=True)
        r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        print(out, flush=True)
        m_my = MY_REMAIN_RE.search(out)
        if args.shard and m_my:
            # 分片模式：以"本片剩余"为准，避免跑到别片的代码上做无用功
            remain = int(m_my.group(1))
            m_all = REMAIN_RE.search(out)
            all_remain = int(m_all.group(1)) if m_all else remain
            print(f"[loop] 本片剩余 {remain} 只 (全局 {all_remain} 只)", flush=True)
        else:
            m = REMAIN_RE.search(out)
            remain = int(m.group(1)) if m else None
        if remain is None:
            print("[loop] 未能解析剩余缺口, 保守退出", flush=True)
            break
        if remain == 0:
            print(f"[loop] ✅ 回填完成, 共 {rnd} 轮, 总耗时 "
                  f"{int(time.time() - t_all)}s", flush=True)
            break
        print(f"[loop] 剩余 {remain} 只 -> 等待源恢复后续跑", flush=True)
        if not wait_recovery(max_wait=args.max_wait, host=args.host):
            print("[loop] ⚠️ 等待超时, 稍后手动重跑可续补", flush=True)
            break
    else:
        print("[loop] 达到 --max-rounds 上限, 可重跑本脚本续补", flush=True)


if __name__ == "__main__":
    main()
