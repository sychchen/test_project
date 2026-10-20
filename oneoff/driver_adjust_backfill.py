# -*- coding: utf-8 -*-
"""复权因子回填驱动器(一次性): 等待腾讯 WAF 解除 -> adjdaily -> adjseed 全量回填。

按 stdout 内容判定成败(而非退出码): adjdaily 看 "failed=0", adjseed 看 "failed=0";
两者均可断点续跑(adjseed 已 seed 股票自动跳过), WAF 期间每轮间隔 10 分钟。
"""
import re
import subprocess
import sys
import time

PY = sys.executable
DRIVER_INTERVAL = 600     # WAF 期间每轮间隔
MAX_ADJDAILY_ROUNDS = 6
MAX_ADJSEED_ROUNDS = 10


def run(args):
    print(f"\n===== driver run_collect.py {' '.join(args)} "
          f"{time.strftime('%H:%M:%S')} =====", flush=True)
    p = subprocess.run([PY, "run_collect.py"] + args,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out[-3000:], flush=True)
    return out


def tail_result(out: str, tag: str) -> dict:
    """从输出中提取最后一个 [tag] ... failed=N 汇总行"""
    m = re.findall(rf"\[{tag}\].*failed=(\d+)", out)
    return {"failed": int(m[-1]) if m else -1}


def main():
    print("等待 WAF 冷却 60 秒...", flush=True)
    time.sleep(60)

    ok_daily = False
    for i in range(MAX_ADJDAILY_ROUNDS):
        out = run(["--adjdaily"])
        r = tail_result(out, "adjdaily")
        print(f"[driver] adjdaily round {i + 1}: {r}", flush=True)
        if r["failed"] == 0:
            ok_daily = True
            break
        time.sleep(DRIVER_INTERVAL)
    if not ok_daily:
        print("[driver] adjdaily 多轮仍未成功, 仍尝试 seed(其自带熔断+续跑)", flush=True)

    for i in range(MAX_ADJSEED_ROUNDS):
        out = run(["--adjseed"])
        r = tail_result(out, "adjseed")
        print(f"[driver] adjseed round {i + 1}: {r}", flush=True)
        if r["failed"] == 0:
            break
        time.sleep(DRIVER_INTERVAL)

    # 终态复核: 事件表行数
    try:
        sys.path.insert(0, ".")
        from sqlalchemy import text
        from app.db import engine
        with engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM stock_adjust_factor")).scalar()
            m = conn.execute(text(
                "SELECT COUNT(DISTINCT ts_code) FROM stock_adjust_factor")).scalar()
        print(f"[driver] 终态: stock_adjust_factor {n} 行 / {m} 只", flush=True)
    except Exception as e:
        print(f"[driver] 终态复核失败: {e}", flush=True)
    print("[driver] 完成", flush=True)


if __name__ == "__main__":
    main()
