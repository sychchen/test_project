# -*- coding: utf-8 -*-
"""日线行情采集
主源: 腾讯证券 web.ifzq.gtimg.cn fqkline（不复权日K, 与历史新浪数据口径一致）
兜底: 新浪 CN_MarketData.getKLineData（腾讯单票多次失败时降级）
说明: 新浪接口在公司网络约 400 次请求后触发 IP 反爬（2026-09-11 实测, 反爬后
      每票重试 3 次×2s 空转, 4700 票耗时 40+ 分钟且大量失败）; 腾讯接口高并发
      稳定, 16 线程全市场约 2-3 分钟。
      两源K线字段口径一致: [日期, 开, 收, 高, 低, 成交量(手)], 涨跌幅自行计算,
      无成交额(留空, 由 moneyflow 采集回填)。
"""
import datetime as dt
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from sqlalchemy import text

from app import config
from app.collector import sina_util
from app.db import engine

TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TX_HEADERS = {"Referer": "https://gu.qq.com"}
SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/CN_MarketData.getKLineData")
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}

# 腾讯接口耐并发，16 线程实测稳定；新浪仅作兜底
MAX_WORKERS = 16

# 长历史回填（单票一次拉 1023 根，响应体大）：16 线程会在跑到一半时触发腾讯 WAF，
# 故用更保守的并发与节奏，见 backfill_history() 说明。
HIST_WORKERS = 6
HIST_SLEEP = 0.1

# ---- 双源熔断：腾讯 WAF / 新浪反爬触发时快速切换，避免全市场空转 ----
TX_COOLDOWN = 600          # 腾讯 WAF 封禁后冷却 10 分钟再试
SINA_CIRCUIT_LIMIT = 100   # 新浪连续失败 100 次判定反爬，其余票快速失败
_TX_STATE = {"blocked_until": 0.0}
_SINA_STATE = {"consecutive_fail": 0, "open": False}


def _tx_available() -> bool:
    return time.time() >= _TX_STATE["blocked_until"]


def _mark_tx_blocked():
    was_available = time.time() >= _TX_STATE["blocked_until"]
    _TX_STATE["blocked_until"] = time.time() + TX_COOLDOWN
    if was_available:  # 每次冷却期只提示一次
        print(f"[quote] 腾讯 WAF 熔断, 冷却 {TX_COOLDOWN}s, 后续票直接走新浪", flush=True)


def _sina_available() -> bool:
    return not _SINA_STATE["open"]


def _mark_sina_result(ok: bool):
    if ok:
        _SINA_STATE["consecutive_fail"] = 0
    else:
        _SINA_STATE["consecutive_fail"] += 1
        if _SINA_STATE["consecutive_fail"] >= SINA_CIRCUIT_LIMIT and not _SINA_STATE["open"]:
            _SINA_STATE["open"] = True
            print(f"[quote] 新浪连续失败 {SINA_CIRCUIT_LIMIT} 次, 判定反爬熔断, "
                  f"剩余票快速失败（稍后重跑 --quotes 即可, 单票无第三数据源可补）", flush=True)

# 每线程独立直连会话（绕过系统代理；requests.Session 非线程安全，故用 thread-local）
_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        _tls.session = s
    return s

UPSERT_SQL = text("""
    INSERT INTO stock_daily_quote
        (ts_code, trade_date, open, high, low, close, pre_close, pct_chg,
         vol, amount, ma5, ma10, ma20, vol_ma20, cont_days, cont_pct, vol_ratio)
    VALUES
        (:ts_code, :trade_date, :open, :high, :low, :close, :pre_close, :pct_chg,
         :vol, :amount, :ma5, :ma10, :ma20, :vol_ma20, :cont_days, :cont_pct, :vol_ratio)
    ON DUPLICATE KEY UPDATE
        open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close),
        pre_close=VALUES(pre_close), pct_chg=VALUES(pct_chg),
        vol=VALUES(vol), amount=VALUES(amount),
        ma5=VALUES(ma5), ma10=VALUES(ma10), ma20=VALUES(ma20), vol_ma20=VALUES(vol_ma20),
        cont_days=VALUES(cont_days), cont_pct=VALUES(cont_pct),
        vol_ratio=VALUES(vol_ratio),
        etl_update_time=NOW()
""")

# 历史向前扩展专用：只插不覆盖。已有行含 amount/估值（由 moneyflow / valuation 回填），
# 用 UPSERT 会把 amount 冲成 NULL，故历史扩展一律 INSERT IGNORE。
INSERT_IGNORE_SQL = text("""
    INSERT IGNORE INTO stock_daily_quote
        (ts_code, trade_date, open, high, low, close, pre_close, pct_chg,
         vol, amount, ma5, ma10, ma20, vol_ma20, cont_days, cont_pct, vol_ratio)
    VALUES
        (:ts_code, :trade_date, :open, :high, :low, :close, :pre_close, :pct_chg,
         :vol, :amount, :ma5, :ma10, :ma20, :vol_ma20, :cont_days, :cont_pct, :vol_ratio)
""")


def cont_days_series(pct_chg, prev: int | None = None) -> list:
    """连涨天数序列：连续上涨 = +n，连续下跌 = -n，平盘/无数据 = 0。
    口径：昨天涨今天跌 -> -1；昨天跌今天涨 -> 1（方向反转即从 ±1 重新计数）。
    prev 为窗口前一根的连涨天数，用于窗口截断时接续计数。"""
    run = int(prev) if prev else 0
    out = []
    for v in pct_chg:
        if v is None or (isinstance(v, float) and math.isnan(v)) or float(v) == 0:
            run = 0
        elif run == 0 or (run > 0) != (float(v) > 0):
            run = 1 if float(v) > 0 else -1
        else:
            run += 1 if float(v) > 0 else -1
        out.append(run)
    return out


def cont_pct_series(pct_chg, cont_days) -> list:
    """连涨涨幅(%)：当前连续涨/跌段内的累计涨跌幅。

    口径：与 cont_days 同一段——|cont_days|==1（段起点）时重置为本日涨跌幅；
    之后在段内按 (1+pct/100) 累乘，返回 (累计因子-1)*100（连跌段为负值）。
    平盘 / 无数据（cont_days 为 0 或 None）→ None。
    例：连涨3天 +2%、+3%、+1% → +6.11%；连跌2天 -2%、-3% → -4.94%。
    """
    out, cum = [], 1.0
    for p, cd in zip(pct_chg, cont_days):
        if cd is None or int(cd) == 0 or p is None or (isinstance(p, float) and math.isnan(p)):
            cum = 1.0
            out.append(None)
            continue
        p = float(p)
        cum = (1 + p / 100.0) if abs(int(cd)) == 1 else cum * (1 + p / 100.0)
        out.append((cum - 1) * 100)
    return out


def _sina_code(symbol: str) -> str:
    return ("sh" if symbol.startswith(("60", "68")) else "sz") + symbol


def _fetch_tx_raw(symbol: str, count: int) -> list:
    """腾讯 fqkline 拉取单票最近 count 根不复权日K。
    param 末段留空 = 不复权（与新浪口径一致; 'qfq' 则为前复权）。
    返回 [[日期, 开, 收, 高, 低, 成交量(手)], ...]，空列表表示无数据。
    WAF 拦截(501/HTML)时抛出 TxWafError 由调用方熔断。"""
    sc = _sina_code(symbol)
    r = _session().get(TX_KLINE_URL, params={
        "param": f"{sc},day,,,{count},",
    }, headers=TX_HEADERS, timeout=10)
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        raise TxWafError(f"waf status={r.status_code}")
    payload = r.json()
    data = (payload.get("data") or {}).get(sc) or {}
    return data.get("day") or data.get("qfqday") or []


class TxWafError(Exception):
    """腾讯 WAF 拦截（HTML 挑战页/501），需熔断而非重试"""


def _sina_norm(klines: list) -> list:
    """新浪 K 线 dict 列表 -> 腾讯口径 [日期, 开, 收, 高, 低, 成交量(手)]。

    两源归一化后共用 _build_daily_df；注意新浪返回的是 dict（含 day/open/close/
    high/low/volume 键），**不是**腾讯那种定长数组，别直接喂给 _build_daily_df。
    """
    return [[k["day"], k["open"], k["close"], k["high"], k["low"],
             float(k["volume"]) / 100] for k in klines]


def _fetch_sina_raw(symbol: str, count: int, host: str = None) -> list:
    """新浪兜底源（host 可覆盖，用于双域名轮询回填）。
    返回 [[日期, 开, 收, 高, 低, 成交量(手)], ...]，与腾讯口径一致。"""
    sc = _sina_code(symbol)
    url = (host + sina_util.SINA_KLINE_PATH) if host else SINA_KLINE_URL
    r = _session().get(url, params={
        "symbol": sc, "scale": 240, "ma": "no", "datalen": count,
    }, headers=SINA_HEADERS, timeout=15)
    return _sina_norm(r.json() or [])


def _fetch_one(symbol: str, count: int) -> pd.DataFrame:
    """拉取单票最近 count 根日K（腾讯主源 + 新浪兜底，双源均带熔断），并计算均线。"""
    klines: list = []
    last_err = None
    if _tx_available():
        for _ in range(2):                       # 腾讯主源最多试 2 次
            try:
                klines = _fetch_tx_raw(symbol, count)
                break
            except TxWafError as e:
                _mark_tx_blocked()               # WAF 是 IP 级封禁, 重试无意义
                last_err = e
                break
            except Exception as e:
                last_err = e
                time.sleep(0.5)
    if not klines:
        if _sina_available():
            try:
                klines = _fetch_sina_raw(symbol, count)
                _mark_sina_result(True)
                last_err = None
            except Exception as e:
                _mark_sina_result(False)
                last_err = e
        elif last_err is None:
            last_err = RuntimeError("sina_circuit_open(反爬)")
    if last_err is not None and not klines:
        raise last_err
    return _build_daily_df(klines)


def _build_daily_df(klines: list) -> pd.DataFrame:
    """[日期, 开, 收, 高, 低, 成交量(手)] -> 含均线/连涨/量比的日线 DataFrame。
    腾讯与新浪两源归一化后共用本函数（口径必须一致）。"""
    if not klines:
        return pd.DataFrame()
    # 条目可能带第 7 个附加字段(dict)，只取前 6 列
    df = pd.DataFrame([k[:6] for k in klines],
                      columns=["trade_date", "open", "close", "high", "low", "vol"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for c in ["open", "close", "high", "low", "vol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["pre_close"] = df["close"].shift(1)
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1) * 100
    df["amount"] = None  # 两源该接口均无成交额字段（由 moneyflow 采集回填）
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["cont_days"] = cont_days_series(df["pct_chg"])
    df["cont_pct"] = cont_pct_series(df["pct_chg"], df["cont_days"])
    # 量比 = 当日成交量 / 前5个交易日均量（标准日线口径；前5日均量取 t-1..t-5）
    df["vol_ratio"] = df["vol"] / df["vol"].shift(1).rolling(5).mean()
    # turn_over_rate(换手率) 依赖流通市值(circ_mv)，由 snapshot_valuation 在估值落库后计算
    # 不在此处裁剪行数：均线需预热窗口，由调用方按目标范围裁剪后入库
    df = df.where(pd.notna(df), None)
    return df


def _clean(v):
    """float nan -> None（次新股数据不足时 ma20 等为 nan，MySQL 拒绝 nan）"""
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _collect_one(ts_code: str, symbol: str, count: int,
                 keep_days: int = None, start_date=None, end_date=None,
                 before_date=None
                 ) -> tuple[str, int, str | None]:
    """单票采集+入库。返回 (ts_code, 写入行数, 失败原因)。
    keep_days: 仅保留最近 N 个交易日（增量模式）；
    start_date/end_date: 仅保留区间内交易日（区间模式）；
    before_date: 仅保留早于该日的行（长历史向前扩展模式，不触碰已有行）。"""
    try:
        df = _fetch_one(symbol, count)
        if df.empty:
            return ts_code, 0, "empty"
        if before_date is not None:
            df = df[df["trade_date"] < before_date]
        elif start_date is not None:
            df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
        elif keep_days is not None:
            dates = sorted(df["trade_date"].unique())[-keep_days:]
            df = df[df["trade_date"].isin(dates)]
        if df.empty:
            return ts_code, 0, "no_rows_in_range"
        df["ts_code"] = ts_code
        rows = df[["ts_code", "trade_date", "open", "high", "low", "close",
                   "pre_close", "pct_chg", "vol", "amount",
                   "ma5", "ma10", "ma20", "vol_ma20", "cont_days",
                   "cont_pct", "vol_ratio"]].to_dict(orient="records")
        rows = [{k: _clean(v) for k, v in row.items()} for row in rows]
        with engine.begin() as conn:
            conn.execute(UPSERT_SQL, rows)
        return ts_code, len(rows), None
    except Exception as e:
        return ts_code, 0, str(e)[:200]


def _parse_date(d: str):
    from datetime import datetime as _dt
    d = d.strip().replace("-", "")
    return _dt.strptime(d, "%Y%m%d").date()


def sync_daily_quotes(days: int = 2, limit: int = None,
                      start_date: str = None, end_date: str = None) -> dict:
    """全市场日线采集（腾讯主源 16 线程并发，新浪兜底）。
    默认增量模式：仅采集最近 2 个交易日（days 可调）。
    区间模式：传 start_date/end_date（YYYY-MM-DD 或 YYYYMMDD）采集指定时间段。
    limit 用于调试（只采前 N 只）。
    说明：接口按 count 返回最近 N 根，区间模式通过多拉取+日期过滤实现，
    count 含 25 根均线预热窗口。"""
    sd = ed = None
    if start_date:
        sd, ed = _parse_date(start_date), _parse_date(end_date or start_date)
        if sd > ed:
            sd, ed = ed, sd
        from datetime import date as _date
        span = max((ed - sd).days, 1)
        lag = max((_date.today() - ed).days, 0)   # 结束日距今天的天数也要多拉
        count = min(int((span + lag) / 1.4) + 30, 1023)
        keep = None
    else:
        days = max(int(days or 2), 1)
        count = min(days + 30, 1023)              # 最近 N 个交易日 + 预热
        keep = days
    with engine.connect() as conn:
        stocks = conn.execute(
            text("SELECT ts_code, symbol FROM stock_basic WHERE is_active=1 ORDER BY ts_code")
        ).fetchall()
    if limit:
        stocks = stocks[:limit]

    ok, failed = 0, []
    total_rows = 0
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_collect_one, ts_code, symbol, count, keep, sd, ed): ts_code
                   for ts_code, symbol in stocks}
        for fut in as_completed(futures):
            ts_code, n, err = fut.result()
            done += 1
            if err:
                failed.append({"ts_code": ts_code, "reason": err})
            else:
                ok += 1
                total_rows += n
            if done % 200 == 0:
                print(f"[quote] progress {done}/{len(stocks)} ok={ok} "
                      f"failed={len(failed)}", flush=True)
            time.sleep(0)  # 让出 GIL，限速由各请求自身耗时控制

    return {
        "total": len(stocks), "ok": ok, "failed_count": len(failed),
        "rows": total_rows, "failed_list": failed[:50],
        "mode": "range" if start_date else "incremental",
        "keep_days": keep, "start_date": str(sd) if sd else None,
        "end_date": str(ed) if ed else None,
    }


def backfill_history(count: int = 1023, limit: int = None,
                     workers: int = HIST_WORKERS, sleep: float = HIST_SLEEP) -> dict:
    """把 stock_daily_quote 往前扩（腾讯 fqkline 单票最多 1023 根 ≈ 4 年）。

    **按"每只股票自己的最早交易日"判定新增范围**（`trade_date < 该股 MIN(trade_date)`），
    只补各自缺失的更早历史，已有行一律不碰 —— 现有行的 amount 由 moneyflow 回填、
    pe_ttm/pb/total_mv/circ_mv 由 --valsnap 落库、turnover_rate 由 indicator_collector
    计算，而本函数的 UPSERT 会把 amount 写成 NULL，覆盖已有行会造成数据丢失。

    因此本函数**可反复执行**：WAF 中断后用同样命令重跑，只会继续补还没补上的股票。

    并发/节奏：比日常增量（16 线程）更保守。单票一次拉 1023 根、响应体大得多，
    实测 16 线程跑约一半股票就触发腾讯 WAF（HTTP 501），之后退到新浪也会被反爬熔断。
    故默认 HIST_WORKERS=6 + 每票 HIST_SLEEP 秒间隔。
    """
    with engine.connect() as conn:
        mins = {tc: d for tc, d in conn.execute(text(
            "SELECT ts_code, MIN(trade_date) FROM stock_daily_quote "
            "GROUP BY ts_code")).fetchall()}
        stocks = conn.execute(
            text("SELECT ts_code, symbol FROM stock_basic WHERE is_active=1 ORDER BY ts_code")
        ).fetchall()
    if not mins:
        raise RuntimeError("stock_daily_quote 为空, 请先执行 --quotes")
    targets = [(tc, sym, mins[tc]) for tc, sym in stocks if tc in mins]
    if limit:
        targets = targets[:limit]

    ok, failed, skipped = 0, [], 0
    total_rows = 0
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_collect_one, tc, sym, count, None, None, None, bd): tc
                   for tc, sym, bd in targets}
        for fut in as_completed(futures):
            ts_code, n, err = fut.result()
            done += 1
            if err == "no_rows_in_range":
                skipped += 1            # 该股本来就没有更早的数据（次新股）
            elif err:
                failed.append({"ts_code": ts_code, "reason": err})
            else:
                ok += 1
                total_rows += n
            if done % 500 == 0:
                print(f"[histbackfill] progress {done}/{len(targets)} ok={ok} "
                      f"skip={skipped} failed={len(failed)} rows={total_rows} "
                      f"elapsed={time.time()-t0:.0f}s", flush=True)
            time.sleep(sleep)
    print(f"[histbackfill] 完成: 新增 {total_rows} 行, ok={ok} 无事可做={skipped} "
          f"failed={len(failed)} 耗时 {time.time()-t0:.0f}s", flush=True)
    return {"total": len(targets), "ok": ok, "skipped": skipped,
            "failed_count": len(failed), "rows": total_rows,
            "failed_list": failed[:50], "count": count}


# ---------------------------------------------------------------------------
# 长历史向前扩展（新浪，腾讯 WAF 封禁时的替代通道）
# ---------------------------------------------------------------------------
HIST_SINA_BARS = 1030      # 单请求根数：日线 × 1030 ≈ 4.2 年（与腾讯 1023 根同深度）


def backfill_history_sina(count: int = HIST_SINA_BARS, limit: int = None,
                          hosts: list = None, **kw) -> dict:
    """用新浪把 stock_daily_quote 向前扩展到约 count 个交易日（默认 4.2 年）。

    与腾讯版 backfill_history 相同的幂等口径：
    - 只写"早于该股库内最早交易日"的行，绝不覆盖已有行（已有行含 amount/估值）；
    - 用 `INSERT IGNORE` 双保险；
    - 断点续跑：先用 000001 探一根确定本次可达的最早日期（reach），
      库内最早日已 <= reach 的股票直接跳过 —— 重跑只会补还没补上的。
    双域名轮询 + 456 冷却复用 sina_util.hosted_backfill。
    """
    host_list = hosts or sina_util.SINA_HOSTS
    probe = sina_util.kline_request(host_list[0], "000001", 240, count)
    if not probe:
        return {"error": "新浪探针无数据（可能被封）"}
    reach = str(probe[0]["day"])[:10]
    skip_floor = (dt.date.fromisoformat(reach) + dt.timedelta(days=20)).isoformat()

    with engine.connect() as conn:
        basics = conn.execute(text(
            "SELECT ts_code, symbol, list_date FROM stock_basic WHERE is_active = 1 "
            "ORDER BY ts_code")).fetchall()
        # MySQL 5.5 无 hash join：stock_basic × stock_daily_quote(320万行) 的
        # LEFT JOIN + GROUP BY 会退化成 5218 次索引扫描（实测 >3 分钟无返回）。
        # 改为单表 GROUP BY（走 (ts_code, trade_date) 索引，秒级）后在 Python 侧合并。
        mins = {r[0]: str(r[1]) for r in conn.execute(text(
            "SELECT ts_code, MIN(trade_date) FROM stock_daily_quote "
            "GROUP BY ts_code")).fetchall()}

    def _needs_backfill(tc, mn, ld):
        """库内最早日已达到本次可达深度 = 无需补；已到上市日附近（7 天内）= 次新股，无历史可补。

        容差 20 个自然日：本次用 1030 根、腾讯版用 1023 根，新浪的窗口天然比腾讯深约
        7 个交易日；若按 reach 精确比较，所有已由腾讯扩展过的股票都会被误判为"还差几天"。
        """
        if mn is None:
            return True
        if mn <= skip_floor:
            return False
        if ld is not None:
            try:
                gap = (dt.date.fromisoformat(mn) - dt.date.fromisoformat(str(ld))).days
                if gap <= 7:
                    return False
            except Exception:
                pass
        return True

    pending = [(tc, sym, mins.get(tc)) for tc, sym, ld in basics
               if _needs_backfill(tc, mins.get(tc), ld)]
    if limit:
        pending = pending[:limit]

    def _store(item, klines):
        ts_code, symbol, own_min = item
        df = _build_daily_df(_sina_norm(klines))
        if df.empty:
            return 0
        if own_min is not None:
            # own_min 由 SQL 的 MIN(trade_date) 经 str() 而来（_needs_backfill 需字符串比
            # skip_floor）；而 df["trade_date"] 是 datetime.date，直接比较会 TypeError。
            own_min_d = (own_min if isinstance(own_min, dt.date)
                         else dt.date.fromisoformat(str(own_min)[:10]))
            df = df[df["trade_date"] < own_min_d]
        if df.empty:
            return 0
        df["ts_code"] = ts_code
        out = df[["ts_code", "trade_date", "open", "high", "low", "close",
                  "pre_close", "pct_chg", "vol", "amount",
                  "ma5", "ma10", "ma20", "vol_ma20", "cont_days",
                  "cont_pct", "vol_ratio"]].to_dict(orient="records")
        out = [{k: _clean(v) for k, v in r.items()} for r in out]
        with engine.begin() as conn:
            conn.execute(INSERT_IGNORE_SQL, out)
        return len(out)

    print(f"[hist-sina] 本次可达最早日 reach={reach}，"
          f"全市场 {len(basics)} 只 / 待扩展 {len(pending)} 只", flush=True)
    stats = sina_util.hosted_backfill(
        pending, scale=240, datalen=count, store=_store, label="hist-sina", **kw)
    stats.update({"reach": reach, "universe": len(basics),
                  "skipped": len(basics) - len(pending), "pending": len(pending),
                  "count": count})
    return stats
