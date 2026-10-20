# -*- coding: utf-8 -*-
"""选股执行服务: 加载行情 -> 执行策略 -> 批次+明细落库"""
import json
import threading
from datetime import datetime

import pandas as pd
from sqlalchemy import bindparam, text

from app import config
from app.db import engine
from app.strategy.base import filter_universe, get_strategy
from app.strategy.config_store import effective_params, is_enabled

# 触发策略注册
import app.strategy.ma_bull  # noqa: F401
import app.strategy.volume_breakout  # noqa: F401
import app.strategy.value  # noqa: F401
import app.strategy.macd_golden  # noqa: F401
import app.strategy.pullback  # noqa: F401
import app.strategy.consecutive_yang  # noqa: F401
import app.strategy.weekly_mainline  # noqa: F401
import app.strategy.zt_pool_follow  # noqa: F401
import app.strategy.value_pctl  # noqa: F401
import app.strategy.fund_flow_diverge  # noqa: F401
import app.strategy.margin_trend  # noqa: F401
import app.strategy.value_quality  # noqa: F401
import app.strategy.fin_quality  # noqa: F401
import app.strategy.chan_buy2  # noqa: F401


def _load_universe(since_days: int = None):
    """加载日线+基本面并做股票池过滤（大A, 剔除科创板）。

    since_days: 只加载最近 N 个自然日的日线（None = 全表）。
    stock_daily_quote 已向前扩展到约 4 年（板块长历史, 见 docs/06 A5-3），
    策略最长回看约 60 个交易日，全表加载会白白多花几倍时间，故默认按窗口加载
    （config.UNIVERSE_LOOKBACK_DAYS）。需要更深历史的回测自行传更大的值。
    """
    sql = "SELECT * FROM stock_daily_quote"
    if since_days:
        sql += (" WHERE trade_date >= (SELECT d FROM (SELECT MAX(trade_date) d"
                " FROM stock_daily_quote) x) - INTERVAL %d DAY" % int(since_days))
    quotes = pd.read_sql(text(sql), engine)
    basics = pd.read_sql(text("SELECT * FROM stock_basic WHERE is_active=1"), engine)
    return filter_universe(quotes, basics)


# ---- 单策略调用的数据缓存（页面"执行选股"等场景，避免每次 93s 全量加载） ----
_UNIVERSE_CACHE = {"sig": None, "data": None}
_UNIVERSE_LOCK = threading.Lock()


def _universe_signature():
    """数据新鲜度签名：任一行 insert/update 或股票数变化都会改变签名。
    新行 etl_update_time 为 NULL，故须同时取 MAX(etl_insert_time)。"""
    with engine.connect() as conn:
        ti, tu, nb = conn.execute(text(
            "SELECT MAX(etl_insert_time), MAX(etl_update_time),"
            "       (SELECT COUNT(*) FROM stock_basic WHERE is_active=1)"
            " FROM stock_daily_quote"
        )).one()
    return (str(ti), str(tu), nb)


def get_universe():
    """带缓存的 universe 获取：签名未变直接复用（毫秒级），变了才重载（约90s）。
    进程内缓存 —— API 服务与 CLI 各自独立，采集后新进程自然拿到新数据。"""
    sig = _universe_signature()
    if _UNIVERSE_CACHE["data"] is not None and _UNIVERSE_CACHE["sig"] == sig:
        return _UNIVERSE_CACHE["data"]
    with _UNIVERSE_LOCK:
        if _UNIVERSE_CACHE["data"] is not None and _UNIVERSE_CACHE["sig"] == sig:
            return _UNIVERSE_CACHE["data"]
        data = _load_universe(config.UNIVERSE_LOOKBACK_DAYS)
        _UNIVERSE_CACHE["sig"], _UNIVERSE_CACHE["data"] = sig, data
        return data


def invalidate_universe_cache():
    _UNIVERSE_CACHE["sig"], _UNIVERSE_CACHE["data"] = None, None


def run_pick(strategy_code: str, params: dict | None = None,
             universe: tuple | None = None) -> dict:
    strategy = get_strategy(strategy_code)

    if universe is not None:
        quotes, basics = universe
    else:
        quotes, basics = get_universe()
    if quotes.empty and strategy_code != "VALUE":
        raise RuntimeError("stock_daily_quote 无数据，请先执行行情采集")

    batch_no = f"P{datetime.now().strftime('%Y%m%d-%H%M%S')}-{strategy_code}"
    eff_params = effective_params(strategy_code, params)  # 默认<库表配置<运行时
    try:
        items = strategy.select(quotes, basics, eff_params)
        trade_date = max((i.trade_date for i in items if i.trade_date), default=None)
        # 策略可给出本次执行结论/原因(如「大盘弱势不开新仓」), 落库便于区分
        # 「防御性 0 命中」与「策略被筛空」——避免把前者误判成死规则。
        note = getattr(strategy, "last_note", None)
        with engine.begin() as conn:
            r = conn.execute(text("""
                INSERT INTO stock_pick_batch
                    (batch_no, strategy_code, strategy_name, trade_date, params,
                     hit_count, status, message)
                VALUES (:batch_no, :sc, :sn, :td, :params, :hc, 'SUCCESS', :msg)
            """), {
                "batch_no": batch_no, "sc": strategy.code, "sn": strategy.name,
                "td": trade_date, "params": json.dumps(strategy.merged_params(eff_params),
                                                       ensure_ascii=False),
                "hc": len(items), "msg": (str(note)[:500] if note else None),
            })
            batch_id = r.lastrowid
            if items:
                conn.execute(text("""
                    INSERT INTO stock_pick_result
                        (batch_id, batch_no, strategy_code, ts_code, name, trade_date,
                         close_price, pct_chg, score, hit_reason)
                    VALUES
                        (:batch_id, :batch_no, :sc, :ts_code, :name, :td,
                         :close, :pct, :score, :reason)
                """), [{
                    "batch_id": batch_id, "batch_no": batch_no, "sc": strategy.code,
                    "ts_code": i.ts_code, "name": i.name, "td": i.trade_date,
                    "close": i.close_price, "pct": i.pct_chg,
                    "score": i.score, "reason": i.hit_reason,
                } for i in items])
            # 同一策略同一基准日只保留最新一次：同事务删除旧批次及其明细
            # （失败则整体回滚，不会出现新旧皆失；<=> 为 NULL 安全等值）
            old_ids = [r[0] for r in conn.execute(text("""
                SELECT b.id FROM stock_pick_batch b
                WHERE b.strategy_code = :sc AND b.trade_date <=> :td AND b.id != :nid
            """), {"sc": strategy.code, "td": trade_date, "nid": batch_id}).fetchall()]
            if old_ids:
                del_res = text(
                    "DELETE FROM stock_pick_result WHERE batch_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                del_batch = text(
                    "DELETE FROM stock_pick_batch WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True))
                conn.execute(del_res, {"ids": old_ids})
                conn.execute(del_batch, {"ids": old_ids})
        return {"batch_no": batch_no, "strategy": strategy.name,
                "trade_date": str(trade_date) if trade_date else None,
                "hit_count": len(items)}
    except Exception as e:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO stock_pick_batch
                    (batch_no, strategy_code, strategy_name, params, hit_count, status, message)
                VALUES (:batch_no, :sc, :sn, :params, 0, 'FAILED', :msg)
            """), {
                "batch_no": batch_no, "sc": strategy.code, "sn": strategy.name,
                "params": json.dumps(params or {}, ensure_ascii=False), "msg": str(e)[:500],
            })
        raise


def pick_asof(strategy_code: str, trade_date: str,
              params: dict | None = None) -> dict:
    """指定基准日选股（不落库, 即时结果）。

    行情/基本面截断至 trade_date 后重放策略: 日线类指标(均线/量能/连涨等)
    为精确 as-of; 财报类按 announce_date ≤ t0 重放, 亦精确。
    依赖快照表的策略(估值分位 VALUE_PCTL / 两融 MARGIN_TREND / 资金流
    FUND_FLOW_DIVERGE)当前读表内最新数据, 对历史基准日存在轻微未来函数——
    前端提示中注明, 后续可逐策略改造为 ≤t0 过滤。
    """
    strategy = get_strategy(strategy_code)
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM stock_daily_quote WHERE trade_date=:d"),
            {"d": trade_date}).scalar()
    if not n:
        raise ValueError(f"{trade_date} 非交易日（库内无该日行情）")
    sql = ("SELECT * FROM stock_daily_quote "
           "WHERE trade_date <= :d AND "
           "trade_date >= :d - INTERVAL %d DAY" % int(config.UNIVERSE_LOOKBACK_DAYS))
    quotes = pd.read_sql(text(sql), engine, params={"d": trade_date})
    basics = pd.read_sql(text("SELECT * FROM stock_basic WHERE is_active=1"), engine)
    quotes, basics = filter_universe(quotes, basics)
    items = strategy.select(quotes, basics, effective_params(strategy_code, params))
    note = getattr(strategy, "last_note", None)
    return {
        "strategy_code": strategy.code, "strategy_name": strategy.name,
        "trade_date": str(trade_date), "note": note,
        "items": [{
            "ts_code": i.ts_code, "name": i.name, "trade_date": i.trade_date,
            "close_price": i.close_price, "pct_chg": i.pct_chg,
            "score": i.score, "hit_reason": i.hit_reason,
        } for i in items],
    }


# ---------------- 盘中实时选股（不落库, 内存临时 bar） ----------------
import re as _re
import time as _time

import requests as _requests

_TX_SNAP_URL = "https://qt.gtimg.cn/q={codes}"
_TX_SNAP_HEADERS = {"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"}
_TX_SNAP_BATCH = 60
_TX_LINE_RE = _re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


def _fetch_rt_snapshot(symbols: list[str]) -> dict:
    """腾讯 qt.gtimg.cn 全市场实时快照(60 只/批, 复用 basic_collector 已验证通道)。
    字段(~分隔, 0 起): 1名称 3现价 4昨收 5今开 6成交量(手) 30时间戳(YYYYMMDDHHMMSS)
    32涨跌幅% 33最高 34最低 36成交量(手) 37成交额(万元)。返回 {symbol: dict}。"""
    sess = _requests.Session()
    sess.trust_env = False
    out: dict = {}
    for i in range(0, len(symbols), _TX_SNAP_BATCH):
        batch = symbols[i:i + _TX_SNAP_BATCH]
        codes = ",".join(("sh" if s.startswith(("60", "68")) else "sz") + s
                         for s in batch)
        last_err = None
        for _ in range(3):
            try:
                r = sess.get(_TX_SNAP_URL.format(codes=codes),
                             headers=_TX_SNAP_HEADERS, timeout=10)
                r.encoding = "gbk"
                last_err = None
                break
            except Exception as e:
                last_err = e
                _time.sleep(1)
        if last_err:
            raise RuntimeError(f"实时快照批次失败({batch[0]}..): {last_err}")
        for m in _TX_LINE_RE.finditer(r.text):
            q = m.group(1)
            f = m.group(2).split("~")
            if len(f) < 47:
                continue

            def _num(x):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return None

            out[q[2:]] = {
                "name": f[1], "close": _num(f[3]), "pre_close": _num(f[4]),
                "open": _num(f[5]), "vol": _num(f[6]), "time": f[30],
                "pct_chg": _num(f[32]), "high": _num(f[33]), "low": _num(f[34]),
                "amount": _num(f[37]),
            }
        _time.sleep(0.05)
    return out


def _recompute_derived(quotes: pd.DataFrame, live_codes: set,
                       circ_map: dict) -> None:
    """对含临时 bar 的股票在内存重算派生列（公式与采集链一致, 仅内存不落库）:
    ma5/ma10/ma20 = close 滚动均值; vol_ma20 = vol 滚动 20 均值(含当日);
    cont_days/cont_pct 复用 quote_collector 序列函数(取尾部 60 根, 连涨段>60 日极少见);
    vol_ratio = 当日量/前5日均量; turnover_rate = vol*close*10000/circ_mv。
    """
    from app.collector.quote_collector import cont_days_series, cont_pct_series
    sub = quotes[quotes["ts_code"].isin(live_codes)]
    for ts_code, g in sub.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date")
        idx = g.index
        close = pd.to_numeric(g["close"], errors="coerce")
        vol = pd.to_numeric(g["vol"], errors="coerce")
        quotes.loc[idx, "ma5"] = close.rolling(5).mean().values
        quotes.loc[idx, "ma10"] = close.rolling(10).mean().values
        quotes.loc[idx, "ma20"] = close.rolling(20).mean().values
        quotes.loc[idx, "vol_ma20"] = vol.rolling(20).mean().values
        pct = [None if pd.isna(v) else float(v) for v in g["pct_chg"]]
        vol_l = [None if pd.isna(v) else float(v) for v in vol]
        tail = pct[-60:]
        cds = cont_days_series(tail)
        cps = cont_pct_series(tail, cds)
        quotes.loc[idx[-1], "cont_days"] = cds[-1] if cds else None
        quotes.loc[idx[-1], "cont_pct"] = cps[-1] if cps else None
        if len(vol_l) >= 6 and vol_l[-1] is not None and all(
                v is not None for v in vol_l[-6:-1]):
            m = sum(vol_l[-6:-1]) / 5.0
            quotes.loc[idx[-1], "vol_ratio"] = None if m == 0 else vol_l[-1] / m
        c = close.iloc[-1]
        v = vol.iloc[-1]
        cmv = circ_map.get(ts_code)
        if pd.notna(c) and pd.notna(v) and cmv and cmv > 0:
            quotes.loc[idx[-1], "turnover_rate"] = round(float(v) * float(c) * 10000 / cmv, 4)


def pick_live(strategy_code: str, params: dict | None = None) -> dict:
    """盘中实时选股（不落库, 即时结果）。

    流程: 腾讯实时快照 -> 以 现价/今开/最高/最低/现量 构造「当日临时 bar」->
    覆盖库内同日行情行 -> 内存重算派生列 -> 直跑策略。全程零数据库写入;
    盘后正式落库仍走每日更新。历史 bar 与盘后落库口径完全一致, 故收盘后
    运行等价于盘后重放。

    近似项（已在 API 注释与页面提示注明）:
    - 快照类策略(估值分位/两融/资金流/涨停池)的快照表为上一盘后数据;
    - 盘中 amount/turnover 为盘中累计值, 流动性下限类参数盘中会偏严;
    - 停牌/未开盘股(现量=0)沿用库内最后一根 bar。
    """
    strategy = get_strategy(strategy_code)
    quotes, basics = get_universe()
    if quotes.empty:
        raise RuntimeError("stock_daily_quote 无数据，请先执行行情采集")
    code_set = set(quotes["ts_code"])
    symbols = sorted({tc.split(".")[0] for tc in code_set})
    snap = _fetch_rt_snapshot(symbols)
    if not snap:
        raise RuntimeError("实时快照为空(腾讯 qt.gtimg.cn 无有效返回)")
    sample = next(iter(snap.values()))
    try:
        snap_date = datetime.strptime(sample["time"][:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        snap_date = datetime.now().date()
    circ_map = dict(zip(basics["ts_code"],
                        pd.to_numeric(basics["circ_mv"], errors="coerce")))

    rows = []
    for sym, q in snap.items():
        ts_code = f"{sym}.{'SH' if sym.startswith(('60', '68')) else 'SZ'}"
        if ts_code not in code_set:
            continue
        close, pre, vol = q["close"], q["pre_close"], q["vol"]
        if not close or not vol:  # 停牌/未开盘 -> 沿用库内最后一根
            continue
        pct = q["pct_chg"]
        if pct is None and pre:
            pct = round((close - pre) / pre * 100, 4)
        rows.append({
            "ts_code": ts_code, "trade_date": snap_date,
            "open": q["open"], "high": q["high"], "low": q["low"],
            "close": close, "pre_close": pre, "pct_chg": pct,
            "vol": vol, "amount": q["amount"] * 1e4 if q["amount"] else None,
        })
    if not rows:
        raise RuntimeError("快照无有效 bar（全市场停牌或快照异常）")
    live = pd.DataFrame(rows)
    # 库内若已含当日(盘后重跑/当日已采集), 临时 bar 覆盖之; 其余历史保留
    q2 = quotes[pd.to_datetime(quotes["trade_date"]).dt.date != snap_date].copy()
    q2 = pd.concat([q2, live], ignore_index=True)
    q2["trade_date"] = pd.to_datetime(q2["trade_date"]).dt.date
    q2 = q2.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    _recompute_derived(q2, set(live["ts_code"]), circ_map)
    items = strategy.select(q2, basics, effective_params(strategy_code, params))
    note = getattr(strategy, "last_note", None)
    return {
        "strategy_code": strategy.code, "strategy_name": strategy.name,
        "trade_date": str(snap_date), "snapshot_time": sample["time"],
        "quote_cnt": len(live),
        "note": note or "盘中实时快照选股, 结果不落库（盘后以每日更新落库为准）",
        "items": [{
            "ts_code": i.ts_code, "name": i.name, "trade_date": i.trade_date,
            "close_price": i.close_price, "pct_chg": i.pct_chg,
            "score": i.score, "hit_reason": i.hit_reason,
        } for i in items],
    }


def run_pick_all(params: dict | None = None) -> list[dict]:
    """执行全部已注册策略（数据只加载一次，7 个策略共享）。
    与逐个 run_pick 相比省掉 (N-1) 次日线加载+股票池过滤。
    策略为 CPU 密集 pandas 计算, 共享数据后顺序执行即为最优（多进程需跨进程
    序列化百万行 DataFrame, 反而更慢）。任一策略失败抛出异常（批次已记 FAILED）。"""
    from app.strategy.base import list_strategies
    universe = _load_universe(config.UNIVERSE_LOOKBACK_DAYS)
    results = []
    for s in list_strategies():
        if not is_enabled(s["code"]):   # 库表配置停用的策略跳过（stock_strategy_config.enabled=0）
            results.append({"strategy": s["name"], "strategy_code": s["code"],
                            "skipped": True, "hit_count": 0})
            continue
        results.append(run_pick(s["code"], params, universe=universe))
    return results
