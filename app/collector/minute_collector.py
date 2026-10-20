# -*- coding: utf-8 -*-
"""分时（分钟级）行情采集
数据源: 新浪财经 CN_MarketData.getKLineData（scale=5/15/30/60 分钟）
说明: 与日K 同接口，volume 单位"股"入库前 /100 转"手"。
      2026-09-09 起取消"仅保留最近3天自动清理"，分时数据前滚积累（保留历史回填成果）。

【2026-09-15 修正】"新浪 datalen 上限 1023 根（约21个交易日）"这一旧结论**实测不成立**：
      scale=5 时 datalen=1500 返回 1500 根（约 32 个交易日）、datalen=3000 返回 3000 根（约 63 天）；
      scale=240（日线）datalen=3000 可回溯到 2014 年。故**历史洞无需 baostock**：
      `backfill_minutes_sina()` 单请求即可取回某票最近 N 个交易日全部分时。
      baostock(见 minute_backfill.py) 当前已不可用——login 成功但 `rs.next()` 永久阻塞
      （响应头返回、数据体永不返回，连日线查询亦然），该模块仅作留档。

限流: 单域名连续请求约数百次即返回 HTTP 456 + HTML 挑战页，恢复约 5 分钟。
      已实测 `vip.stock.finance.sina.com.cn` 与 `money.finance.sina.com.cn` 挂同一路径、
      响应体逐字节一致，但**配额互相独立** → 回填时双域名轮询（一方 456 另一方仍可用）。
"""
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import requests
from sqlalchemy import text

from app import config
from app.collector import sina_util
from app.db import engine

SINA_KLINE_PATH = "/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_KLINE_URL = "https://money.finance.sina.com.cn" + SINA_KLINE_PATH
# 双域名配额独立：回填时轮询使用（money 域名更易吃 456，vip 域名实测更宽松）
SINA_KLINE_HOSTS = ["https://vip.stock.finance.sina.com.cn",
                    "https://money.finance.sina.com.cn"]
HEADERS = {"Referer": "https://finance.sina.com.cn"}

BARS_PER_DAY = 48      # 5分钟线: 4小时交易 = 48 根/日
# 历史回填单请求根数：5 分钟 × 1500 根 ≈ 32 个交易日，足够"保留最近 30 个交易日"
# （含今日未收盘的零头，留 2 天余量）
MIN_BACKFILL_BARS = 1500

# ---- 新浪反爬熔断：连续失败 N 次判定封禁，剩余票快速失败 ----
# 全量 5218 票分钟K必然触发新浪日限(~400次), 熔断后改用
# run_collect.py --minbackfill(baostock) 按区间补缺, 避免 3次重试×2s 的全场空转
SINA_CIRCUIT_LIMIT = 100
_SINA_STATE = {"consecutive_fail": 0, "open": False}


def _sina_available() -> bool:
    return not _SINA_STATE["open"]


def _mark_sina_result(ok: bool):
    if ok:
        _SINA_STATE["consecutive_fail"] = 0
    else:
        _SINA_STATE["consecutive_fail"] += 1
        if _SINA_STATE["consecutive_fail"] >= SINA_CIRCUIT_LIMIT and not _SINA_STATE["open"]:
            _SINA_STATE["open"] = True
            print(f"[minute] 新浪连续失败 {SINA_CIRCUIT_LIMIT} 次, 判定反爬熔断, "
                  f"剩余票快速失败（可跑 --minbackfill 用 baostock 补缺）", flush=True)

_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        _tls.session = s
    return s


def _sina_code(symbol: str) -> str:
    return ("sh" if symbol.startswith(("60", "68")) else "sz") + symbol


def _clean(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


UPSERT_SQL = text("""
    INSERT INTO stock_minute_quote
        (ts_code, scale, bar_time, open, high, low, close, vol)
    VALUES
        (:ts_code, :scale, :bar_time, :open, :high, :low, :close, :vol)
    ON DUPLICATE KEY UPDATE
        open=VALUES(open), high=VALUES(high), low=VALUES(low),
        close=VALUES(close), vol=VALUES(vol)
""")


def _sina_rows(ts_code: str, klines: list, scale: int) -> list:
    """新浪 K 线原始 JSON -> stock_minute_quote 行（volume 股 -> 手）。"""
    df = pd.DataFrame(klines)
    df = df.rename(columns={"day": "bar_time", "volume": "vol"})
    df["bar_time"] = pd.to_datetime(df["bar_time"])
    for c in ["open", "close", "high", "low", "vol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["vol"] = df["vol"] / 100  # 股 -> 手
    df["ts_code"] = ts_code
    df["scale"] = scale
    rows = df[["ts_code", "scale", "bar_time", "open", "high", "low",
               "close", "vol"]].to_dict(orient="records")
    rows = [{k: _clean(v) for k, v in row.items()} for row in rows]
    for row in rows:
        row["bar_time"] = row["bar_time"].to_pydatetime()
    return rows


def _collect_one(ts_code: str, symbol: str, scale: int, datalen: int) -> tuple[str, int, str | None]:
    """单票分时采集+入库。返回 (ts_code, 写入行数, 失败原因)。
    新浪反爬熔断后快速失败（不浪费请求/重试时间）。"""
    if not _sina_available():
        return ts_code, 0, "sina_circuit_open(反爬)"
    sc = _sina_code(symbol)
    try:
        last_err = None
        for _ in range(config.RETRY_TIMES):
            try:
                r = _session().get(SINA_KLINE_URL, params={
                    "symbol": sc, "scale": scale, "ma": "no", "datalen": datalen,
                }, headers=HEADERS, timeout=10)
                klines = r.json() or []
                break
            except Exception as e:
                last_err = e
                time.sleep(2)
        else:
            _mark_sina_result(False)
            raise last_err
        if not klines:
            _mark_sina_result(True)   # 空数据=正常响应(停牌等), 不算反爬
            return ts_code, 0, "empty"

        rows = _sina_rows(ts_code, klines, scale)
        with engine.begin() as conn:
            conn.execute(UPSERT_SQL, rows)
        _mark_sina_result(True)
        return ts_code, len(rows), None
    except Exception as e:
        return ts_code, 0, str(e)[:200]


def sync_minute_quotes(scale: int = 5, days: int = 1, limit: int = None) -> dict:
    """全市场分时采集（8 线程并发）。
    days=1 采当日 48 根；days=2 采近两天 96 根（ datalen=48*days ）。"""
    datalen = BARS_PER_DAY * days * (scale // 5 if scale >= 5 else 1)
    with engine.connect() as conn:
        stocks = conn.execute(
            text("SELECT ts_code, symbol FROM stock_basic WHERE is_active=1 ORDER BY ts_code")
        ).fetchall()
    if limit:
        stocks = stocks[:limit]

    ok, failed = 0, []
    total_rows = 0
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_collect_one, ts_code, symbol, scale, datalen): ts_code
                   for ts_code, symbol in stocks}
        for fut in as_completed(futures):
            ts_code, n, err = fut.result()
            done += 1
            if err:
                failed.append({"ts_code": ts_code, "reason": err})
            else:
                ok += 1
                total_rows += n
            if done % 500 == 0:
                print(f"[minute] progress {done}/{len(stocks)} ok={ok} "
                      f"failed={len(failed)}", flush=True)

    return {
        "total": len(stocks), "ok": ok, "failed_count": len(failed),
        "rows": total_rows, "scale": scale, "failed_list": failed[:50],
    }



# ---------------------------------------------------------------------------
# 历史分时回填（新浪双域名，窗口按"最近 N 个交易日"自动推出）
# 双域名节奏调度 / 456 熔断 / 重试轮 全部复用 app.collector.sina_util
# 单请求根数常量 MIN_BACKFILL_BARS 定义在本文件顶部（与分时接口说明放一起）
# ---------------------------------------------------------------------------
def backfill_minutes_sina(target_days: int = 30, scale: int = 5,
                          datalen: int = MIN_BACKFILL_BARS, limit: int = None,
                          members_only: bool = False, **kw) -> dict:
    """用新浪回填"最近 target_days 个交易日"的分时（UPSERT 幂等，可反复重跑）。

    - 窗口自动推导：取 stock_daily_quote 最近 target_days 个交易日，最早那天为起点，
      不再需要手工传 --start-date/--end-date。
    - 断点续跑：已覆盖 >= target_days-3 个交易日的票直接跳过（容忍少量停牌）。
    - members_only=True 只回填有板块归属的股票（板块分时合成只需这些；可省约一半请求）。
    - 双域名轮询 + 456 自动冷却：单域名被封不影响整体进度，已成功的票不会重做。
    """
    with engine.connect() as conn:
        days = [str(r[0]) for r in conn.execute(text(
            "SELECT DISTINCT trade_date FROM stock_daily_quote "
            "ORDER BY trade_date DESC LIMIT :k"), {"k": int(target_days)}).fetchall()]
        if not days:
            return {"error": "stock_daily_quote 无数据"}
        start_day = min(days)
        need = max(1, int(target_days) - 3)
        if members_only:
            codes = conn.execute(text(
                "SELECT DISTINCT b.ts_code, b.symbol FROM stock_basic b "
                "JOIN stock_board_member m ON m.ts_code=b.ts_code "
                "WHERE b.is_active=1 ORDER BY b.ts_code")).fetchall()
        else:
            codes = conn.execute(text(
                "SELECT ts_code, symbol FROM stock_basic WHERE is_active=1 "
                "ORDER BY ts_code")).fetchall()
        done = {r[0] for r in conn.execute(text(
            "SELECT ts_code FROM stock_minute_quote WHERE scale=:s AND bar_time >= :sd "
            "GROUP BY ts_code HAVING COUNT(DISTINCT DATE(bar_time)) >= :need"),
            {"s": scale, "sd": start_day, "need": need}).fetchall()}

    pending = [(c, s) for c, s in codes if c not in done]
    if limit:
        pending = pending[:limit]

    def _store(item, klines):
        rows = _sina_rows(item[0], klines, scale)
        with engine.begin() as conn:
            conn.execute(UPSERT_SQL, rows)
        return len(rows)

    print(f"[min-backfill] 窗口 {start_day}~{days[0]}（{len(days)} 个交易日）"
          f"池 {len(codes)} 只 待补 {len(pending)} 只 datalen={datalen}"
          f"{' 仅成分股' if members_only else ''}", flush=True)
    stats = sina_util.hosted_backfill(
        pending, scale=scale, datalen=datalen, store=_store, label="min-backfill", **kw)
    stats.update({"scale": scale, "datalen": datalen, "start_day": start_day,
                  "end_day": days[0], "target_days": len(days),
                  "universe": len(codes), "skipped": len(done),
                  "pending": len(pending)})
    return stats
