# -*- coding: utf-8 -*-
"""个股资金流向(日度)历史回填 —— 新浪 MoneyFlow 通道

背景
----
`stock_money_flow` 原设计为"自上线日起逐日前滚积累"(见 app/collector/moneyflow_collector.py)，
仅有 2026-09-08 起的 5 个交易日；而 `stock_daily_quote` 日线历史起自 2025-09-09
(246 个交易日)。本脚本按个股拉取逐日资金流，把目标区间补齐到与日线同长。

数据源（**不使用东财 push2his**，该接口对本机 IP 封禁且已被明确排除）
------------------------------------------------------------------
新浪 `MoneyFlow.ssl_qsfx_zjlrqs`（个股资金流向趋势）
    one stock -> 最近 N 个交易日的逐日资金流（num 实测可到 400，一次覆盖整区间）
    返回字段: opendate, trade, changeratio, turnover, netamount, ratioamount,
              r0_net, r0_ratio, r0x_ratio, cnt_r0x_ratio, cate_ra, cate_na
口径映射（主力 = 超大单 + 大单，与 stock_money_flow 一致）
    main_net  = netamount               (主力净流入, 元)
    super_net = r0_net                  (超大单净流入, 元)
    large_net = netamount - r0_net      (大单净流入, 元，由主力-超大单反推)
    main_pct  = ratioamount * 100       (主力净流入占比 %)
    mid_net / small_net = NULL          (新浪该接口不提供中/小单拆分)

⚠ 口径差异提示：新浪与东财对"主力/超大单"的单笔金额分档阈值不同，同一交易日同一只股票
   的主力净额差异可达 1.5~3 倍（实测 000001.SZ 2026-09-14：新浪 +1.26 亿 vs 东财 +0.42 亿，
   但两者成交额一致 = 8.76 亿）。因此本脚本默认 **--skip-existing**：只补日期缺失的行，
   不覆盖库内已有的东财口径数据，避免造成口径断层；如需全区间统一为新窗口径，显式加
   `--overwrite` 并自行承担口径变更。

限流
----
实测该端点吞吐很高（25 并发 200 次请求 2.5s 全成功），但连续约 400 次后触发新浪 IP
反爬（返回空体 → JSONDecodeError），需等待窗口恢复。故内置:
    --max-requests  单次运行的请求预算（默认 350，保守值，防触发反爬）
    --cooldown      连续失败熔断后的冷却秒数
配合"断点续跑"，分批多次运行即可在限额内完成全量。

用法
----
  python backfill_money_flow.py --probe                  # 探测源可用性 + 回溯深度
  python backfill_money_flow.py --probe --num 5
  python backfill_money_flow.py --start 2025-09-09 --workers 12          # 断点续跑(建议)
  python backfill_money_flow.py --start 2025-09-09 --limit 30 --workers 4  # 小样本试跑
  python backfill_money_flow.py --verify-source                          # 与库内快照比对口径
"""
import argparse
import json
import math
import multiprocessing as mp
import os
import random
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根(含 app 包), 而非 oneoff/ 自身

import requests
from sqlalchemy import text

from app.db import engine

SINA_PATH = "/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
# 新浪同一接口挂在两个域名下，实测**限流互相独立**（一个 456 时另一个仍 200）
# —— 按域名分片并行可将整体吞吐翻倍。
HOSTS = {
    "vip": "https://vip.stock.finance.sina.com.cn",
    "money": "https://money.finance.sina.com.cn",
}
HOST = "vip"          # 由 --host 覆盖
SINA_URL = HOSTS[HOST] + SINA_PATH
REFERER = "https://finance.sina.com.cn/"
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]


def sina_url(host=None):
    """按域名拼装接口 URL（host 为空时用模块级 HOST）"""
    return HOSTS[host or HOST] + SINA_PATH


def shard_filter(codes, shard):
    """按 ts_code 的 crc32 稳定分片 —— 与缺口列表组成无关，
    保证多实例并行时各片严格互斥、不会重复抓同一只。"""
    if not shard:
        return codes
    i, n = (int(x) for x in str(shard).split("/"))
    if not (0 <= i < n):
        raise ValueError(f"--shard 需形如 I/N 且 0<=I<N, 收到 {shard!r}")
    return [c for c in codes if (zlib.crc32(c.encode()) % n) == i]


UPSERT_SQL = text("""
    INSERT INTO stock_money_flow
        (ts_code, trade_date, main_net, super_net, large_net, mid_net, small_net, main_pct)
    VALUES
        (:ts_code, :trade_date, :main_net, :super_net, :large_net, :mid_net, :small_net, :main_pct)
    ON DUPLICATE KEY UPDATE
        main_net=VALUES(main_net), super_net=VALUES(super_net), large_net=VALUES(large_net),
        mid_net=VALUES(mid_net), small_net=VALUES(small_net), main_pct=VALUES(main_pct),
        etl_update_time=NOW()
""")
INSERT_IGNORE_SQL = text(str(UPSERT_SQL).replace(
    "INSERT INTO", "INSERT IGNORE INTO").split("ON DUPLICATE KEY UPDATE")[0])

# ---- 跨进程共享的限流/熔断/预算状态 ----
_G = {}


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _daima(ts_code: str) -> str:
    code, _, mkt = ts_code.partition(".")
    return ("sh" if mkt.upper() == "SH" else "sz") + code


# ----------------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------------
def probe(num=400, sample=3, pause=0.5):
    """探测源可用性 + 单次请求能回溯多少根（判断目标区间能否一次拿到）"""
    codes = ["600519.SH", "000001.SZ", "300750.SZ"][:sample]
    out = {"url": sina_url(), "reachable": False, "ok": 0, "fail": 0,
           "bars": [], "errors": []}
    s = requests.Session()
    s.trust_env = False
    for c in codes:
        try:
            r = s.get(sina_url(), params={"page": 1, "num": num, "sort": "opendate",
                                        "asc": 0, "daima": _daima(c)},
                      headers={"Referer": REFERER, "User-Agent": UA_POOL[0]}, timeout=20)
            j = r.json()
            if j:
                out["ok"] += 1
                out["reachable"] = True
                out["bars"].append({"code": c, "n": len(j),
                                    "first": j[-1]["opendate"], "last": j[0]["opendate"]})
            else:
                out["fail"] += 1
                out["errors"].append(f"{c}: empty list")
        except Exception as e:
            out["fail"] += 1
            out["errors"].append(f"{c}: {type(e).__name__}: {str(e)[:70]}")
        time.sleep(pause)
    return out


# ----------------------------------------------------------------------------
# 工作进程
# ----------------------------------------------------------------------------
def _init_worker(shared, interval, fail_threshold, cooldown, host):
    global _G
    _G = {"shared": shared, "thr": fail_threshold, "cd": cooldown,
          "base": interval, "host": host, "sess": requests.Session()}
    _G["sess"].trust_env = False  # 直连, 绕过系统代理


class BlockError(Exception):
    """返回挑战页/非 JSON —— 判定 IP 限流，立即熔断而非重试(重试只会浪费额度)"""


class BudgetExhausted(Exception):
    """请求预算用尽，本轮停止(可稍后重跑续补)"""


def _throttle():
    """全局节奏：多进程共享 next_ts，保证整体 QPS ≈ 1/interval(而非每进程 1/interval)。
    预算(used)按"真实发出的请求"计数，而非任务数——否则重试会突破预算触发反爬。"""
    sh = _G["shared"]
    while True:
        now = time.monotonic()
        cd = sh["cooldown_until"].value
        if now < cd:
            time.sleep(min(cd - now, 30))
            continue
        with sh["lock"]:
            now = time.monotonic()
            if now < sh["cooldown_until"].value:
                continue
            if sh["budget"].value > 0 and sh["used"].value >= sh["budget"].value:
                raise BudgetExhausted()
            nxt = sh["next_ts"].value
            if nxt <= now:
                sh["next_ts"].value = now + sh["interval"].value
                sh["used"].value += 1
                return
        time.sleep(min(max(nxt - now, 0.001), 0.05))


def _enter_cooldown(reason: str):
    sh = _G["shared"]
    with sh["lock"]:
        if time.monotonic() < sh["cooldown_until"].value:
            return
        sh["cooldown_until"].value = time.monotonic() + _G["cd"]
        sh["cooldowns"].value += 1
        sh["fail_streak"].value = 0
        # 冷却后是全新窗口，速率复位到基准(否则上一轮的降速会永久累积)
        sh["interval"].value = _G["base"]
        print(f"[cooldown] {reason} -> 冷却 {int(_G['cd'])}s, 速率复位 "
              f"{_G['base']}s (第 {sh['cooldowns'].value} 次)", flush=True)


def _mark_fail():
    sh = _G["shared"]
    with sh["lock"]:
        sh["fail_streak"].value += 1
        streak = sh["fail_streak"].value
    _enter_cooldown(f"连续失败 {streak} 次")


def _mark_ok():
    sh = _G["shared"]
    with sh["lock"]:
        sh["fail_streak"].value = 0
        # 成功时缓慢恢复速率(0.98 衰减) —— 自适应降速不掉进"越慢越慢"的死循环
        if sh["interval"].value > _G["base"]:
            sh["interval"].value = max(_G["base"], sh["interval"].value * 0.98)


def _fetch_one(ts_code, start, end, num, retries):
    """单只 -> ([(ts_code, date, main, super, large, mid, small, pct)], 总根数)"""
    params = {"page": 1, "num": num, "sort": "opendate", "asc": 0,
              "daima": _daima(ts_code)}
    last_err = None
    for attempt in range(retries):
        _throttle()
        try:
            r = _G["sess"].get(sina_url(_G.get("host")), params=params, timeout=20, headers={
                "Referer": REFERER,
                "User-Agent": UA_POOL[(attempt + os.getpid()) % len(UA_POOL)],
                "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
            })
            txt = (r.text or "").lstrip()
            # 456/挑战页/HTML -> 判定限流，立即熔断(不消耗 retries)
            if r.status_code != 200 or not txt.startswith("["):
                _mark_fail()
                raise BlockError(f"http={r.status_code} head={txt[:40]!r}")
            j = json.loads(txt)
            if not isinstance(j, list):
                _mark_fail()
                raise BlockError(f"unexpected payload type={type(j).__name__}")
            if not j:
                _mark_ok()               # 空列表 = 该股无数据(退市/停牌)，不算失败
                return [], 0
            rows = []
            for rec in j:
                d = str(rec.get("opendate") or "")
                if len(d) != 10 or d < start or d > end:
                    continue
                main = _num(rec.get("netamount"))
                sup = _num(rec.get("r0_net"))
                large = (main - sup) if (main is not None and sup is not None) else None
                pct = _num(rec.get("ratioamount"))
                rows.append((ts_code, d, main, sup, large, None, None,
                             (pct * 100 if pct is not None else None)))
            _mark_ok()
            return rows, len(j)
        except BlockError as e:
            last_err = e
            raise                     # 限流不重试，交给冷却窗口
        except Exception as e:
            last_err = e
            time.sleep(random.uniform(0.5, 1.2) * (attempt + 1))
    raise RuntimeError(f"{ts_code}: {type(last_err).__name__}: {str(last_err)[:70]}")


def _write(rows, skip_existing):
    if not rows:
        return 0
    data = [{"ts_code": r[0], "trade_date": r[1], "main_net": r[2], "super_net": r[3],
             "large_net": r[4], "mid_net": r[5], "small_net": r[6], "main_pct": r[7]}
            for r in rows]
    sql = INSERT_IGNORE_SQL if skip_existing else UPSERT_SQL
    with engine.begin() as conn:
        r = conn.execute(sql, data)
    return r.rowcount


def _worker(task):
    """task = (ts_code, start, end, num, retries, skip_existing)"""
    code, start, end, num, retries, skip_existing = task
    try:
        rows, nbars = _fetch_one(code, start, end, num, retries)
        written = _write(rows, skip_existing)
        return {"code": code, "ok": True, "written": written, "bars": nbars,
                "first": (rows[0][1] if rows else None),
                "last": (rows[-1][1] if rows else None)}
    except BudgetExhausted:
        return {"code": code, "ok": False, "skipped": True,
                "error": "request budget exhausted"}
    except Exception as e:
        return {"code": code, "ok": False, "written": 0, "error": str(e)[:110]}


# ----------------------------------------------------------------------------
# 待补清单: 逐股对比日线行数与资金流行数
# ----------------------------------------------------------------------------
def pending_stocks(start, end):
    with engine.connect() as conn:
        q = conn.execute(text("""
            SELECT ts_code, COUNT(*) AS n FROM stock_daily_quote
            WHERE trade_date BETWEEN :s AND :e GROUP BY ts_code
        """), {"s": start, "e": end}).fetchall()
        f = conn.execute(text("""
            SELECT ts_code, COUNT(*) AS n FROM stock_money_flow
            WHERE trade_date BETWEEN :s AND :e GROUP BY ts_code
        """), {"s": start, "e": end}).fetchall()
        active = {r[0] for r in conn.execute(text(
            "SELECT ts_code FROM stock_basic WHERE is_active=1"))}
    fmap = {r[0]: r[1] for r in f}
    out = []
    for ts_code, n in q:
        if ts_code not in active:
            continue
        if fmap.get(ts_code, 0) < n:          # 少一行即视为待补
            out.append(ts_code)
    return out


def stats(start, end):
    with engine.connect() as conn:
        days = conn.execute(text("""
            SELECT COUNT(DISTINCT trade_date) FROM stock_daily_quote
            WHERE trade_date BETWEEN :s AND :e"""), {"s": start, "e": end}).scalar()
        tot = conn.execute(text("SELECT COUNT(*) FROM stock_basic WHERE is_active=1")).scalar()
        cur = conn.execute(text("""
            SELECT COUNT(*), MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date)
            FROM stock_money_flow""")).fetchone()
    return {"days": days, "active": tot, "flow_rows": cur[0],
            "flow_min": str(cur[1]), "flow_max": str(cur[2]), "flow_days": cur[3]}


def verify_source(sample=5, num=5):
    """实拉新浪与库内已有行逐字段比对，量化两源口径差异"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ts_code, trade_date, main_net, super_net, large_net, main_pct
            FROM stock_money_flow WHERE main_net IS NOT NULL
            ORDER BY trade_date DESC, ts_code LIMIT 60
        """)).mappings().all()
    s = requests.Session()
    s.trust_env = False
    out = {"pairs": [], "errors": []}
    for r in rows:
        if len(out["pairs"]) >= sample:
            break
        try:
            j = s.get(SINA_URL, params={"page": 1, "num": num, "sort": "opendate",
                                        "asc": 0, "daima": _daima(r["ts_code"])},
                      headers={"Referer": REFERER, "User-Agent": UA_POOL[0]},
                      timeout=20).json()
        except Exception as e:
            out["errors"].append(f'{r["ts_code"]}: {type(e).__name__}: {str(e)[:50]}')
            time.sleep(1.5)
            continue
        rec = next((x for x in j if x.get("opendate") == str(r["trade_date"])), None)
        if not rec:
            time.sleep(0.6)
            continue
        sm = _num(rec.get("netamount"))
        ss = _num(rec.get("r0_net"))
        dbm = float(r["main_net"] or 0)
        out["pairs"].append({
            "ts_code": r["ts_code"], "date": str(r["trade_date"]),
            "sina_main": sm, "db_main": dbm,
            "rel_diff": round(abs(sm - dbm) / max(abs(dbm), 1.0), 4),
            "sina_super": ss, "db_super": (float(r["super_net"]) if r["super_net"] is not None else None),
            "sina_pct": (round(_num(rec.get("ratioamount")) * 100, 3)
                         if _num(rec.get("ratioamount")) is not None else None),
            "db_pct": (float(r["main_pct"]) if r["main_pct"] is not None else None),
        })
        time.sleep(0.6)
    return out


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-09-09", help="起始交易日 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="截止交易日, 默认取库内 MAX(trade_date)")
    ap.add_argument("--workers", type=int, default=10, help="并发进程数")
    ap.add_argument("--interval", type=float, default=0.05, help="每进程请求最小间隔(秒)")
    ap.add_argument("--num", type=int, default=400, help="新浪 num 参数(单次回溯根数)")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只(试跑)")
    ap.add_argument("--max-requests", type=int, default=350,
                    help="单次运行的请求预算(0=不限)，用于规避新浪 IP 反爬")
    ap.add_argument("--fail-threshold", type=int, default=8, help="连续失败熔断阈值")
    ap.add_argument("--cooldown", type=float, default=120.0, help="熔断冷却秒数")
    ap.add_argument("--overwrite", action="store_true",
                    help="覆盖库内已有行(默认 INSERT IGNORE 只补缺口，不破坏东财口径数据)")
    ap.add_argument("--probe", action="store_true", help="仅探测数据源可用性")
    ap.add_argument("--host", choices=list(HOSTS.keys()), default="vip",
                    help="接口域名(vip/money)。两个域名限流独立，可按域名并行分片提速")
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="分片: 只处理第 I 片(0 基)/共 N 片。配合 --host 多进程并行")
    ap.add_argument("--verify-source", action="store_true", help="与库内已有行比对口径差异")
    args = ap.parse_args()

    global HOST
    HOST = args.host

    if args.probe:
        t0 = time.time()
        p = probe(num=args.num)
        p["elapsed"] = round(time.time() - t0, 2)
        print(json.dumps(p, ensure_ascii=False, indent=2))
        if not p["reachable"]:
            print("\n[结论] 新浪 MoneyFlow 当前不可用(IP 限流中)，请稍后重试。", file=sys.stderr)
            return 2
        return 0

    if args.verify_source:
        print(json.dumps(verify_source(), ensure_ascii=False, indent=2))
        return 0

    with engine.connect() as conn:
        end = args.end or str(conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_daily_quote")).scalar())

    st = stats(args.start, end)
    print(f"[区间] {args.start} ~ {end}")
    print(f"[现状] stock_money_flow: {st['flow_rows']:,} 行, {st['flow_min']} ~ {st['flow_max']} "
          f"({st['flow_days']} 个交易日)")
    print(f"[规模] 日线 {st['days']} 天 × 活跃股 {st['active']} 只 ≈ {st['days'] * st['active']:,} 行")

    codes = pending_stocks(args.start, end)
    try:
        codes = shard_filter(codes, args.shard)
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    if args.limit:
        codes = codes[:args.limit]
    print(f"[待补] {len(codes)} 只  (缺行即视为待补，可断点续跑)")
    print(f"[并发] {args.workers} 进程, 间隔 {args.interval}s, 请求预算 {args.max_requests or '不限'}, "
          f"域名 {args.host}, 分片 {args.shard or '全量'}, "
          f"{'覆盖已有行' if args.overwrite else '只补缺口(INSERT IGNORE)'}")
    if not codes:
        print("[完成] 目标区间已无缺口。")
        return 0

    mgr = mp.Manager()
    shared = {
        "lock": mgr.Lock(),
        "interval": mgr.Value("d", args.interval),
        "next_ts": mgr.Value("d", 0.0),        # 全局下一个可发请求的时刻(跨进程共享节奏)
        "fail_streak": mgr.Value("i", 0),
        "cooldown_until": mgr.Value("d", 0.0),
        "cooldowns": mgr.Value("i", 0),
        "budget": mgr.Value("i", args.max_requests),
        "used": mgr.Value("i", 0),
    }
    tasks = [(c, args.start, end, args.num, args.retries, not args.overwrite)
             for c in codes]
    t0, done, ok, failed, skipped, written = time.time(), 0, 0, 0, 0, 0
    errs = []
    with mp.Pool(args.workers, initializer=_init_worker,
                 initargs=(shared, args.interval, args.fail_threshold,
                           args.cooldown, args.host)) as pool:
        for res in pool.imap_unordered(_worker, tasks, chunksize=1):
            done += 1
            if res.get("skipped"):
                skipped += 1
            elif res["ok"]:
                ok += 1
                written += res["written"]
            else:
                failed += 1
                if len(errs) < 15:
                    errs.append(res["error"])
            if done % 25 == 0 or done == len(tasks):
                el = time.time() - t0
                print(f"  {done}/{len(tasks)}  入库 {written:,} 行  失败 {failed}  "
                      f"熔断 {shared['cooldowns'].value}  用时 {el:.0f}s", flush=True)
            if skipped and skipped >= args.workers:
                print("  [停止] 请求预算已用尽，本次到此为止（可稍后重跑续补）", flush=True)
                pool.terminate()
                break

    summ = {"start": args.start, "end": end, "candidates": len(codes),
            "processed": done, "ok": ok, "failed": failed,
            "rows_written": written, "requests_used": shared["used"].value,
            "cooldowns": shared["cooldowns"].value,
            "elapsed_sec": round(time.time() - t0, 1), "errors": errs}
    print(json.dumps(summ, ensure_ascii=False, indent=2))
    remain = pending_stocks(args.start, end)
    my_remain = shard_filter(remain, args.shard)
    print(f"[剩余缺口] {len(remain)} 只")
    if args.shard:
        print(f"[本片剩余] {len(my_remain)} 只")
    # 容忍个位数比例的偶发失败(http=504 等): 缺口由下次运行 pending_stocks 续跑补齐,
    # 失败率 ≤1% 视为整体成功, 避免 daily_update fallback 因 2/5216 失败整步报红(2026-10-20)
    fail_rate = failed / done if done else 0.0
    return 0 if fail_rate <= 0.01 else 1


if __name__ == "__main__":
    sys.exit(main())
