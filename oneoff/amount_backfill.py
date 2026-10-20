# -*- coding: utf-8 -*-
"""一次性修复: stock_daily_quote 9/10-9/16 amount 采集缺口(26,262 行 NULL)

根因(2026-09-21 排查):
    日线两源(腾讯/新浪)K线接口均无成交额字段, amount 设计上由 moneyflow
    采集(sync_money_flow)顺带回填 —— 但该源是【当日快照】, 只能补
    MAX(trade_date) 一天; 9/10-9/16 moneyflow 步骤失败后, 快照源永远
    无法补历史 -> 6 个交易日全 NULL。

修复(数据源选定过程):
    东财 push2his 历史 k 线首轮单请求成功后即被风控掐断(RemoteDisconnected,
    本机 IP 有封禁前科) -> 改用 Baostock(专为批量历史查询设计, 无激进反爬)。
    交叉验证: 茅台 9/10 baostock=2428698784.05 vs 东财=2428698784.00,
    精确到分一致; amount 单位元。
幂等: oneoff/amount_backfill_progress.txt 记录已处理 ts_code,
    分片重跑从断点直插前沿(同 adjseed 断点模式)。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")   # oneoff 目录直接运行时定位项目根

import baostock as bs
from sqlalchemy import text

from app.db import engine

OUT_DAYS = {"2026-09-10", "2026-09-11", "2026-09-14",
            "2026-09-15", "2026-09-16"}
BEG, END = "2026-09-09", "2026-09-17"
REQ_INTERVAL = 0.05    # baostock 官方服务, 无需激进限速

PROGRESS = Path(__file__).resolve().parent / "amount_backfill_progress.txt"

UPDATE_SQL = text("""
    UPDATE stock_daily_quote SET amount=:amount, etl_update_time=NOW()
    WHERE ts_code=:ts_code AND trade_date=:trade_date AND amount IS NULL
""")


def _bs_code(ts_code: str) -> str:
    sym = ts_code.split(".")[0]
    return ("sh." if ts_code.endswith(".SH") else "sz.") + sym


def _fetch_amounts(ts_code: str) -> dict[str, float]:
    rs = bs.query_history_k_data_plus(
        _bs_code(ts_code), "date,code,amount",
        start_date=BEG, end_date=END, frequency="d", adjustflag="3")
    out = {}
    while rs.error_code == "0" and rs.next():
        d, _, amt = rs.get_row_data()
        if d in OUT_DAYS and amt:
            try:
                out[d] = float(amt)
            except ValueError:
                pass
    return out


def main():
    done = set()
    if PROGRESS.exists():
        done = {ln.strip() for ln in
                PROGRESS.read_text(encoding="utf-8").splitlines() if ln.strip()}

    with engine.connect() as conn:
        stocks = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT ts_code FROM stock_daily_quote "
            "WHERE trade_date IN :days AND amount IS NULL ORDER BY ts_code"),
            {"days": tuple(OUT_DAYS)}).fetchall()]
    todo = [tc for tc in stocks if tc not in done]
    print(f"[amountfix] 缺口股票 {len(stocks)}, 已处理 {len(done)}, 待处理 {len(todo)}",
          flush=True)
    if not todo:
        return

    lg = bs.login()
    if lg.error_code != "0":
        print(f"[amountfix] baostock 登录失败: {lg.error_msg}", flush=True)
        return

    ok = failed = rows_total = 0
    t0 = time.time()
    try:
        for i, tc in enumerate(todo, 1):
            try:
                amounts = _fetch_amounts(tc)
            except Exception as e:
                failed += 1
                print(f"[amountfix] {tc} 失败: {type(e).__name__}: {e}", flush=True)
                time.sleep(2)
                continue   # baostock 偶发抖动, 记录后继续下一只(断点文件不含它, 下轮重试)
            if amounts:
                with engine.begin() as conn:
                    res = conn.execute(UPDATE_SQL, [
                        {"ts_code": tc, "trade_date": d, "amount": a}
                        for d, a in amounts.items()])
                    rows_total += res.rowcount
            ok += 1
            with PROGRESS.open("a", encoding="utf-8") as pf:
                pf.write(tc + "\n")
            if i % 500 == 0:
                print(f"[amountfix] progress {i}/{len(todo)} ok={ok} "
                      f"rows={rows_total} failed={failed} "
                      f"elapsed={int(time.time() - t0)}s", flush=True)
            time.sleep(REQ_INTERVAL)
    finally:
        bs.logout()

    print(f"[amountfix] 本分片完成: ok={ok} rows={rows_total} failed={failed} "
          f"用时={int(time.time() - t0)}s", flush=True)


if __name__ == "__main__":
    main()
