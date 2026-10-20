# -*- coding: utf-8 -*-
"""手动跑数入口: 控制台实时输出 + 日志落盘 logs/daily_update_YYYYMMDD_HHMMSS.log

用法(二选一):
  1) 双击项目根的 每日更新.bat
  2) 命令行: python run_manual.py [--skip-pick] [--days 2] (参数透传 daily_update)

设计:
  - 建议每个交易日收盘后(19:00 以后, 两融数据 T+1 早上发布更佳)运行一次
  - 全链路幂等, 中途失败直接重跑即可
  - 完成后窗口停留, 汇总每步 ✓/✗
"""
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.argv = [sys.argv[0]] + sys.argv[1:]   # 保持透传参数给 daily_update 的 argparse


class Tee:
    """stdout 同时写控制台与日志文件"""
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


def main():
    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir,
        f"daily_update_{datetime.now():%Y%m%d_%H%M%S}.log")
    log_f = open(log_path, "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_f)
    sys.stderr = Tee(sys.__stderr__, log_f)

    print(f"[run_manual] 启动每日更新, 日志: {log_path}", flush=True)
    t0 = time.time()
    exit_code = 0
    try:
        import daily_update
        daily_update.main()
    except SystemExit as e:
        exit_code = int(e.code or 0)
    except Exception as e:
        print(f"[run_manual] 异常终止: {type(e).__name__}: {e}", flush=True)
        exit_code = 2

    mins = (time.time() - t0) / 60
    print(f"\n[run_manual] 结束, 用时 {mins:.1f} 分钟, 退出码 {exit_code}", flush=True)
    if exit_code == 0:
        print("[run_manual] ✓ 全部成功", flush=True)
    else:
        print("[run_manual] ✗ 有步骤失败(见上方 ✗ 行), 可直接重跑补齐", flush=True)

    try:
        input("\n按回车关闭窗口...")
    except EOFError:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
