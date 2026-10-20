# -*- coding: utf-8 -*-
"""个股筹码分布(获利盘/成本分布)本地衍生 —— 换手率衰减法。

背景: 东财 stock_cyq_em 依赖 push2his(2026-09-23 起 WAF 拦截)且需逐股 5,221 请求,
覆盖度审计 P7 按用户确认改为本地衍生: 由库内 stock_daily_quote 的
日线(high/low/close) + 换手率(turnover_rate, baostock 补齐 250 个交易日)自算。

算法(逐股时间衰减模型):
  - 价格网格: 该股窗口内 [min(low)*0.85, max(high)*1.15] 均分 120 档;
  - 每日: 旧筹码按 (1-t) 衰减(t=当日换手率, 截断到 [0,1]), 新筹码权重 t 按
    三角分布摊在当日 [low, high](峰值在近似均价 (low+high+2*close)/4);
    停牌日(无行情行/换手率 NULL)筹码不变;
  - 窗口首日假设全部筹码位于首日价格区间, 250 日预热后初始假设影响衰减殆尽;
  - 指标: 获利盘比例(收盘价下方筹码占比)、筹码加权平均成本、
    90% 成本上下界(5%/95% 分位)、90% 成本集中度。

口径注意: 东财 cyq 用分钟级成交, 本表为日线近似 —— 适合趋势与横截面相对比较,
不宜与东财数值直接比对。只落最近 N 个交易日(默认 30), 每次全窗口重算, 幂等。
"""
import time

import numpy as np
from sqlalchemy import text

from app.db import engine
from app.collector.akshare_collector import REQ_INTERVAL  # noqa: F401 (保持导入习惯一致)

N_BUCKETS = 120
WARMUP_DAYS = 250


def _upsert_cyq(rows: list, batch: int = 5000):
    if not rows:
        return
    sql = text("""
        INSERT INTO stock_cyq
            (ts_code, trade_date, profit_ratio, avg_cost,
             cost_90_low, cost_90_high, conc_90)
        VALUES (:ts_code, :trade_date, :profit_ratio, :avg_cost,
                :cost_90_low, :cost_90_high, :conc_90)
        ON DUPLICATE KEY UPDATE
            profit_ratio=VALUES(profit_ratio), avg_cost=VALUES(avg_cost),
            cost_90_low=VALUES(cost_90_low), cost_90_high=VALUES(cost_90_high),
            conc_90=VALUES(conc_90), etl_update_time=CURRENT_TIMESTAMP""")
    with engine.begin() as conn:
        for i in range(0, len(rows), batch):
            conn.execute(sql, rows[i:i + batch])


def _tri_weights(centers: np.ndarray, low: float, high: float, peak: float) -> np.ndarray:
    """[low, high] 上的三角分布权重(峰值 peak), 归一化到和为 1。"""
    eps = 1e-9
    if high - low < eps:  # 一字板: 全部筹码落在最近档
        w = np.zeros_like(centers)
        w[int(np.argmin(np.abs(centers - low)))] = 1.0
        return w
    peak = min(max(peak, low), high)
    w = np.where(
        centers <= peak,
        (centers - low) / max(peak - low, eps),
        (high - centers) / max(high - peak, eps),
    )
    w[(centers < low) | (centers > high)] = 0.0
    s = w.sum()
    return w / s if s > eps else np.zeros_like(w)


def sync_cyq(days: int = 30, warmup: int = WARMUP_DAYS) -> dict:
    """计算并落库最近 days 个交易日的筹码指标(全市场, 幂等覆盖)。"""
    t0 = time.time()
    with engine.connect() as conn:
        dates = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT trade_date FROM stock_daily_quote "
            "WHERE turnover_rate IS NOT NULL ORDER BY trade_date")).fetchall()]
        if not dates:
            return {"rows": 0, "note": "no quotes with turnover_rate"}
        window = dates[-warmup:]
        out_dates = set(dates[-days:])
        qrows = conn.execute(text(
            "SELECT ts_code, trade_date, high, low, close, turnover_rate "
            "FROM stock_daily_quote WHERE trade_date >= :d0 "
            "ORDER BY ts_code, trade_date"), {"d0": window[0]}).fetchall()

    by_stock = {}
    for ts, td, high, low, close, turn in qrows:
        by_stock.setdefault(ts, []).append((td, float(high), float(low),
                                            float(close),
                                            None if turn is None else float(turn)))
    centers_cache = {}
    rows, skipped = [], 0
    for ts, seq in by_stock.items():
        lows = np.array([x[2] for x in seq])
        highs = np.array([x[1] for x in seq])
        if lows.min() <= 0:
            skipped += 1
            continue
        g_lo, g_hi = lows.min() * 0.85, highs.max() * 1.15
        centers = centers_cache.get((g_lo, g_hi))
        if centers is None:
            centers = np.linspace(g_lo, g_hi, N_BUCKETS)
            centers_cache[(g_lo, g_hi)] = centers
        dist = np.zeros(N_BUCKETS)
        for idx, (td, high, low, close, turn) in enumerate(seq):
            if idx == 0:
                dist = _tri_weights(centers, low, high, (low + high + 2 * close) / 4)
            else:
                t = 0.0 if turn is None else min(max(turn / 100.0, 0.0), 1.0)
                if t > 0:
                    dist = dist * (1 - t) + _tri_weights(
                        centers, low, high, (low + high + 2 * close) / 4) * t
            if td in out_dates and dist.sum() > 0.5:
                total = dist.sum()
                cdf = np.cumsum(dist) / total
                q05 = float(np.interp(0.05, cdf, centers))
                q95 = float(np.interp(0.95, cdf, centers))
                avg_cost = float((centers * dist).sum() / total)
                denom = q95 + q05
                rows.append({
                    "ts_code": ts, "trade_date": td,
                    "profit_ratio": round(float(np.interp(close, centers, cdf)), 4),
                    "avg_cost": round(avg_cost, 3),
                    "cost_90_low": round(q05, 3),
                    "cost_90_high": round(q95, 3),
                    "conc_90": round((q95 - q05) / denom, 4) if denom > 0 else None,
                })

    _upsert_cyq(rows)
    print(f"[cyq] 落库 {len(rows)} 行(窗口 {window[0]}~{window[-1]}, "
          f"输出最近 {days} 日), 跳过 {skipped} 只, 耗时 {time.time()-t0:.0f}s", flush=True)
    return {"rows": len(rows), "out_days": days, "skipped": skipped,
            "window_start": str(window[0]), "window_end": str(window[-1])}
