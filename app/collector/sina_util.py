# -*- coding: utf-8 -*-
"""新浪 K 线接口（`CN_MarketData.getKLineData`）的公共工具：

双域名发送节奏调度 + HTTP 456 熔断 + 通用回填驱动器。分时回填
（minute_collector.backfill_minutes_sina）与日线历史扩展
（quote_collector.backfill_history_sina）共用本模块，避免两处各写一套限流逻辑。

实测要点（2026-09-15，均为本机实测结论）:
1. `datalen` **没有 1023 上限**（旧结论已被证伪）：
   - scale=5（5 分钟）: datalen=1500 → 1500 根（约 32 个交易日）；datalen=3000 → 3000 根（约 63 天）
   - scale=240（日线）: datalen=1030 → 1030 根（约 4.2 年）；datalen=3000 → 回溯到 2014 年
   → 历史分时/日线回填**不再需要 baostock**（其 `rs.next()` 已永久阻塞）。
2. `vip.stock.finance.sina.com.cn` 与 `money.finance.sina.com.cn`：
   同一路径、响应体逐字节一致，但**限流配额互相独立**（一方 456 时另一方仍 200）
   → 双域名轮询可翻倍吞吐。
3. 单域名突发上限约 150~200 次后返回 HTTP 456 + HTML 挑战页，约 5 分钟恢复
   ⇒ 稳态约 0.67~0.83 req/s/域名。间隔设得比稳态还快只会"1 分钟烧完配额 + 白等 5 分钟"。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

SINA_KLINE_PATH = "/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_HOSTS = ["https://vip.stock.finance.sina.com.cn",
              "https://money.finance.sina.com.cn"]
HEADERS = {"Referer": "https://finance.sina.com.cn"}

DEFAULT_WORKERS = 4        # 并发线程（约每域名 2 个）
DEFAULT_INTERVAL = 1.2     # 单域名两次请求最小间隔(秒) ≈ 0.83 req/s/域名
DEFAULT_COOLDOWN = 300     # 命中 456 后该域名冷却秒数（实测恢复约 5 分钟）
DEFAULT_MAX_PASSES = 6     # 失败重试轮数上限
FAIL_STREAK = 3            # 同一域名连续失败多少次判定被封

_tls = threading.local()
_lock = threading.Lock()


def session() -> requests.Session:
    """线程内复用连接（trust_env=False 绕过系统代理，公司代理会拦 localhost/外网）。"""
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        _tls.session = s
    return s


def sina_code(symbol: str) -> str:
    """6 位代码 -> 新浪代码（沪市 60/68 加 sh，其余 sz）。"""
    return ("sh" if symbol.startswith(("60", "68")) else "sz") + symbol


class HostState:
    """单个新浪域名的发送节奏与熔断状态。"""
    __slots__ = ("base", "next_ts", "blocked_until", "fail_streak", "ok", "bad")

    def __init__(self, base: str):
        self.base = base
        self.next_ts = 0.0        # 下一次允许发送的时间戳
        self.blocked_until = 0.0  # 冷却截止时间戳
        self.fail_streak = 0
        self.ok = 0
        self.bad = 0


def lease_host(hosts: list, interval: float) -> HostState:
    """挑一个可用域名并占用一个发送时隙；全部冷却中则等到最早解封（阻塞直到可用）。"""
    while True:
        now = time.time()
        with _lock:
            cand = [h for h in hosts if h.blocked_until <= now]
            if cand:
                h = min(cand, key=lambda x: x.next_ts)
                wait = h.next_ts - now
                if wait <= 0:
                    h.next_ts = now + interval
                    return h
            else:
                wait = min(h.blocked_until for h in hosts) - now
        time.sleep(max(0.05, min(wait, 2.0)))


def kline_request(host_base: str, symbol: str, scale: int, datalen: int,
                  sess: requests.Session = None, timeout: float = 20) -> list:
    """取回单票最近 datalen 根 K 线。

    456 挑战页 / 非 JSON / 空体一律抛 RuntimeError（供上层熔断识别，勿吞异常）。
    """
    sess = sess or session()
    r = sess.get(host_base + SINA_KLINE_PATH, params={
        "symbol": sina_code(symbol), "scale": scale, "ma": "no", "datalen": datalen,
    }, headers=HEADERS, timeout=timeout)
    txt = (r.text or "").lstrip()
    if r.status_code != 200 or not txt.startswith("["):
        raise RuntimeError(f"http {r.status_code} body={txt[:48]!r}")
    kl = r.json()
    if not isinstance(kl, list):
        raise RuntimeError(f"非列表响应: {str(kl)[:48]}")
    return kl


def hosted_backfill(items, *, scale: int, datalen: int, store, symbol_of=None,
                    hosts=None, workers: int = DEFAULT_WORKERS,
                    interval: float = DEFAULT_INTERVAL,
                    cooldown: float = DEFAULT_COOLDOWN,
                    max_passes: int = DEFAULT_MAX_PASSES,
                    label: str = "sina", progress_every: int = 200) -> dict:
    """双域名轮询回填驱动器（幂等重跑友好）。

    参数:
      items:        待处理元素（一般 (ts_code, symbol)）；list() 后使用
      store:        (item, klines) -> 写入行数；抛异常视为该票失败、进入重试轮
      symbol_of:    item -> 6 位代码（默认 item[1]）
      scale/datalen: 新浪接口参数（分时 5/1500，日线 240/1030 等）

    返回统计 dict：ok/empty/failed/rows/remaining/hosts/elapsed_sec/failed_list
    - 域名被 456 时该票不会被丢弃，而是收进下一轮重试（最多 max_passes 轮）
    - 已成功的票不会重做（调用方应在外层用库内覆盖度做断点续跑）
    """
    items = list(items)
    symbol_of = symbol_of or (lambda it: it[1])
    host_list = [HostState(b) for b in (hosts or SINA_HOSTS)]
    stats = {"label": label, "total": len(items), "ok": 0, "empty": 0, "failed": 0,
             "rows": 0, "failed_list": []}
    if not items:
        stats.update({"remaining": 0, "elapsed_sec": 0})
        return stats

    plock = threading.Lock()
    cnt = {"i": 0}
    t0 = time.time()

    def _progress():
        n = cnt["i"]
        if n % progress_every == 0:
            print(f"[{label}] progress {n}/{len(items)} ok={stats['ok']} "
                  f"empty={stats['empty']} failed={stats['failed']} "
                  f"rows={stats['rows']} elapsed={time.time()-t0:.0f}s", flush=True)

    def _one(item):
        key = item[0] if isinstance(item, (tuple, list)) else item
        symbol = symbol_of(item)
        sess = session()
        while True:
            h = lease_host(host_list, interval)
            try:
                kl = kline_request(h.base, symbol, scale, datalen, sess)
            except Exception as e:
                with _lock:
                    h.bad += 1
                    h.fail_streak += 1
                    tripped = h.fail_streak >= FAIL_STREAK
                    if tripped:
                        h.blocked_until = time.time() + cooldown
                        h.fail_streak = 0
                if tripped:
                    print(f"[{label}] {h.base} 连续失败→冷却 {int(cooldown)}s: "
                          f"{str(e)[:90]}", flush=True)
                with plock:
                    stats["failed"] += 1
                    if len(stats["failed_list"]) < 200:
                        stats["failed_list"].append({"key": key, "reason": str(e)[:100]})
                return False, item
            with _lock:
                h.fail_streak = 0
                h.ok += 1

            if not kl:
                with plock:
                    stats["empty"] += 1
                    cnt["i"] += 1
                    _progress()
                return True, item
            try:
                rows = int(store(item, kl) or 0)
            except Exception as e:
                with plock:
                    stats["failed"] += 1
                    first = len(stats["failed_list"]) < 3
                    if len(stats["failed_list"]) < 200:
                        stats["failed_list"].append(
                            {"key": key, "reason": f"store:{str(e)[:120]}"})
                # store 异常往往是"代码口径 bug"而非网络问题：不打印会让失败被静默
                # 吞进重试轮，表现为进度 0 却毫无线索。前 3 例立刻打出来。
                if first:
                    print(f"[{label}] store 失败样例 key={key}: "
                          f"{type(e).__name__}: {str(e)[:150]}", flush=True)
                return False, item
            with plock:
                stats["ok"] += 1
                stats["rows"] += rows
                cnt["i"] += 1
                _progress()
            return True, item

    pending = items
    for p in range(1, max_passes + 1):
        if not pending:
            break
        if p > 1:
            print(f"[{label}] 第 {p} 轮: 重试 {len(pending)} 只 "
                  f"(已用 {time.time()-t0:.0f}s)", flush=True)
            time.sleep(min(cooldown, 60))
        retry = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_one, it) for it in pending]
            for f in as_completed(futs):
                ok, it = f.result()
                if not ok:
                    retry.append(it)
        pending = retry

    stats["remaining"] = len(pending)
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    stats["hosts"] = [{"host": h.base, "ok": h.ok, "bad": h.bad} for h in host_list]
    stats["failed_list"] = stats["failed_list"][:50]
    return stats
