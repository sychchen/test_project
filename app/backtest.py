# -*- coding: utf-8 -*-
"""策略回测模块
思路: 以 N 个交易日前的交易日 t0 为基准日，将行情截断至 t0 复用策略逻辑模拟当日选股，
再统计 t0 收盘买入 -> 最新交易日收盘的收益表现（胜率/平均收益/极值）。
数据按 days_ago 反推窗口加载（t0 前留足策略预热段），不全表加载。
另提供「纪律化退出」模拟(短线口径): -stop_pct% 硬止损 / +be_trigger% 上移保本 /
max_hold 日强制平仓, 贴近实盘纪律(源 docs/03-一周策略.md 风控规则)。

复权口径(2026-09-21 接入 stock_adjust_factor):
    stock_daily_quote 为不复权价且 pre_close=close.shift(1), 除权日收益被分红/送转
    缺口污染。回测的所有收益率(t0→t_end 与纪律化退出)一律按复权价计算:
        adj_close(t) = close(t) / cumf(t),  cumf(t) = ∏ factor_after(e≤t)
        等价于 adj_ret(t) = naive_ret(t) / factor_after(t) (事件日), 非事件日不变。
    展示字段 buy_price/now_price 保持不复权原价(与实盘下单价一致),
    return_pct/exit_return 为复权口径 —— 两者数值不再满足 now/buy-1 恒等式, 属预期。
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import filter_universe, get_strategy

# 触发策略注册
import app.pick_service  # noqa: F401

# 估值字段: 回测时须用 t0 当日快照, 而非 stock_basic 最新值(防未来函数)
_VAL_COLS = ["pe_ttm", "pb", "total_mv", "circ_mv"]


def _apply_adjust(quotes: pd.DataFrame) -> pd.DataFrame:
    """按 stock_adjust_factor 事件表生成复权列 cumf / adj_close(原地追加)。
    无事件股票 cumf=1, adj_close=close。事件覆盖最近 ~2.6 年(腾讯 qfqday 深度)。"""
    quotes = quotes.copy()
    if quotes.empty:
        return quotes.assign(cumf=1.0, adj_close=lambda d: d["close"])
    ev = pd.read_sql(text(
        "SELECT ts_code, ex_date, factor_after FROM stock_adjust_factor"), engine)
    if ev.empty:
        return quotes.assign(cumf=1.0, adj_close=lambda d: d["close"])
    ev["trade_date"] = pd.to_datetime(ev["ex_date"])
    ev["factor_after"] = pd.to_numeric(ev["factor_after"], errors="coerce")
    ev = (ev.dropna(subset=["factor_after"])
            .sort_values(["ts_code", "trade_date"]))
    ev["cumf"] = ev.groupby("ts_code")["factor_after"].cumprod()
    ev = ev.sort_values("trade_date")  # merge_asof 要求 on 键全局有序

    # 以独立 key 帧做 asof 合并, 再按原索引映射回 quotes —— 不改动 quotes 的
    # 行序与 trade_date dtype, 避免 cumf 值与行错位(2026-09-21 实测踩坑)
    key = pd.DataFrame({
        "ts_code": quotes["ts_code"].values,
        "trade_date": pd.to_datetime(quotes["trade_date"]).values,
    }, index=quotes.index)
    key = key.sort_values(["trade_date", "ts_code"], kind="mergesort")
    merged = pd.merge_asof(
        key, ev[["ts_code", "trade_date", "cumf"]],
        on="trade_date", by="ts_code", direction="backward")
    cumf = pd.Series(merged["cumf"].fillna(1.0).astype(float).values,
                     index=key.index)
    quotes["cumf"] = cumf.reindex(quotes.index)
    quotes["cumf"] = quotes["cumf"].fillna(1.0)
    quotes["adj_close"] = quotes["close"] / quotes["cumf"]
    return quotes


def _overlay_valuation_asof(basics: pd.DataFrame, t0) -> pd.DataFrame:
    """用 t0 当日 stock_daily_quote 中的估值快照覆盖 basics 同名字段。
    快照自 2026-09-08 起前滚积累, 更早日期快照为 NULL 时回退 stock_basic
    最新值(口径上仍有残留偏差, 随积累自然消退)。"""
    snap = pd.read_sql(
        text("SELECT ts_code, pe_ttm, pb, total_mv, circ_mv "
             "FROM stock_daily_quote WHERE trade_date=:t0"),
        engine, params={"t0": t0})
    if snap.empty:
        return basics
    snap = snap.set_index("ts_code")
    b = basics.set_index("ts_code")
    for c in _VAL_COLS:
        if c in b.columns and c in snap.columns:
            b[c] = pd.to_numeric(snap[c], errors="coerce").combine_first(
                pd.to_numeric(b[c], errors="coerce"))
    return b.reset_index()


def _simulate_exit(bars: pd.DataFrame, buy: float, stop_pct: float,
                   be_trigger: float, max_hold: int) -> tuple[float, int, str] | None:
    """纪律化退出模拟。bars: t0 之后日线(升序)。返回 (收益%, 持有交易日数, 原因)。
    原因: 止损/保本/到期/持有中。日内路径保守处理: 先按当前止损线判 low,
    触及保本线次日起生效; 跳空低开按开盘价离场。"""
    stop = buy * (1 - stop_pct / 100)
    days, last_c = 0, None
    for _, r in bars.iterrows():
        if pd.isna(r["low"]) or pd.isna(r["close"]):
            continue
        days += 1
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        if l <= stop:
            px = float(stop) if pd.notna(o) and o >= stop else float(o if pd.notna(o) else l)
            return round((px / buy - 1) * 100, 2), days, ("保本" if stop >= buy else "止损")
        if pd.notna(h) and h >= buy * (1 + be_trigger / 100):
            stop = max(stop, buy)  # 保本线次日生效(保守)
        last_c = c
        if days >= max_hold:
            return round((float(c) / buy - 1) * 100, 2), days, "到期"
    if last_c is None:
        return None
    return round((float(last_c) / buy - 1) * 100, 2), days, "持有中"


def run_backtest(strategy_code: str, days_ago: int = 5,
                 stop_pct: float = 3.0, breakeven_trigger: float = 5.0,
                 max_hold_days: int = 3) -> dict:
    strategy = get_strategy(strategy_code)

    # 只加载所需窗口：t0 之前还需要策略预热段（最长约 60 个交易日）→ 按 days_ago 反推自然日
    since_days = int(days_ago * 2) + 200
    quotes = pd.read_sql(text(
        "SELECT * FROM stock_daily_quote WHERE trade_date >= "
        "(SELECT d FROM (SELECT MAX(trade_date) d FROM stock_daily_quote) x) "
        f"- INTERVAL {since_days} DAY"), engine)
    basics = pd.read_sql(text("SELECT * FROM stock_basic WHERE is_active=1"), engine)
    quotes, basics = filter_universe(quotes, basics)  # 只选大A, 剔除科创板
    if quotes.empty:
        raise RuntimeError("stock_daily_quote 无数据，请先执行行情采集")

    trade_dates = sorted(quotes["trade_date"].unique())
    if days_ago < 1 or days_ago >= len(trade_dates):
        raise ValueError(f"days_ago 需在 1~{len(trade_dates)-1} 之间（库内仅 {len(trade_dates)} 个交易日）")
    t0 = trade_dates[-(days_ago + 1)]
    t_end = trade_dates[-1]

    # 复权列接入(收益口径), 之后选股/统计均使用带 adj_close 的 quotes
    quotes = _apply_adjust(quotes)

    # 截断行情至 t0，模拟当日视角选股（估值同步截断至 t0 快照，防未来函数）
    # 策略参数取库表配置口径（stock_strategy_config），与实盘选股一致
    from app.strategy.config_store import db_params
    hist = quotes[quotes["trade_date"] <= t0]
    basics = _overlay_valuation_asof(basics, t0)
    items = strategy.select(hist, basics, db_params(strategy_code))

    # t0 / 最新交易日 复权收盘价(收益口径) 与 不复权收盘价(展示口径)
    day0 = quotes[quotes["trade_date"] == t0].set_index("ts_code")
    day1 = quotes[quotes["trade_date"] == t_end].set_index("ts_code")
    px0, px1 = day0["adj_close"], day1["adj_close"]
    raw_px0, raw_px1 = day0["close"], day1["close"]
    cumf0 = day0["cumf"]

    # ---- 幸存者偏差修复(接入 P1 停复牌, 2026-09-23) ----
    # t0 买入后至 t_end 期间停牌的股票在 t_end 无行情行, 原逻辑直接丢弃 ->
    # 统计只含正常交易股票。改为: 以停牌前最后可得复权收盘计浮盈, 并标注停牌状态。
    susp = pd.read_sql(text(
        "SELECT ts_code FROM stock_trade_suspend "
        "WHERE trade_date > :t0 AND trade_date <= :tend "
        "GROUP BY ts_code"), engine,
        params={"t0": t0, "tend": t_end})
    suspended_set = set(susp["ts_code"]) if not susp.empty else set()

    details = []
    for it in items:
        if it.ts_code not in px0.index:
            continue
        buy = float(px0[it.ts_code])
        if not buy:
            continue
        if it.ts_code in px1.index:  # t_end 正常交易
            now = float(px1[it.ts_code])
            raw_now = float(raw_px1[it.ts_code])
            susp_flag = it.ts_code in suspended_set  # 期间曾停牌已复牌
            susp_now_flag = False
        else:
            # t_end 无行情行: 停牌中(用最后可得收盘) 或 退市/数据缺失
            tail = quotes[(quotes["ts_code"] == it.ts_code)
                          & (quotes["trade_date"] <= t_end)]
            if tail.empty:
                continue
            last = tail.sort_values("trade_date").iloc[-1]
            now = float(last["adj_close"])
            raw_now = float(last["close"])  # 停牌前最后不复权收盘(展示口径)
            susp_flag = True  # 停牌中(或长期无行情, 同样不应被静默剔除)
            susp_now_flag = True
        ret = (now / buy - 1) * 100
        details.append({
            "ts_code": it.ts_code, "name": it.name,
            "buy_date": str(t0), "buy_price": round(float(raw_px0[it.ts_code]), 3),
            "now_price": round(raw_now, 3),
            "return_pct": round(ret, 2),
            "suspended": susp_flag,
            "suspended_now": susp_now_flag,
        })
    details.sort(key=lambda x: x["return_pct"], reverse=True)

    rets = [d["return_pct"] for d in details]
    stats = {}
    if rets:
        s = pd.Series(rets)
        stats = {
            "hits": len(rets),
            "win_rate": round(float((s > 0).mean() * 100), 1),
            "avg_return": round(float(s.mean()), 2),
            "median_return": round(float(s.median()), 2),
            "max_return": round(float(s.max()), 2),
            "max_loss": round(float(s.min()), 2),
            "suspended_hits": sum(1 for d in details if d["suspended"]),
            "suspended_now": sum(1 for d in details if d["suspended_now"]),
        }

    # ---- 纪律化退出模拟(短线: 硬止损/上移保本/到期强平) ----
    # 模拟路径用复权价(除权缺口不误触止损), 买入价同步换算为 t0 复权口径
    disc_details: list[dict] = []
    for d in details:
        fwd = (quotes[(quotes["ts_code"] == d["ts_code"])
                      & (quotes["trade_date"] > t0)]
               .sort_values("trade_date"))
        if fwd.empty or d["ts_code"] not in cumf0.index:
            continue
        f0 = float(cumf0[d["ts_code"]])
        adj_buy = float(raw_px0[d["ts_code"]]) / f0
        fwd = fwd.copy()
        for col in ("open", "high", "low", "close"):
            fwd[col] = fwd[col] / fwd["cumf"]
        sim = _simulate_exit(fwd, adj_buy, stop_pct, breakeven_trigger, max_hold_days)
        if sim is None:
            continue
        ret, hd, reason = sim
        disc_details.append({"ts_code": d["ts_code"], "name": d["name"],
                             "exit_return": ret, "hold_days": hd, "exit_reason": reason})
    discipline = {}
    if disc_details:
        sr = pd.Series([x["exit_return"] for x in disc_details])
        hd_s = pd.Series([x["hold_days"] for x in disc_details])
        exits: dict[str, int] = {}
        for x in disc_details:
            exits[x["exit_reason"]] = exits.get(x["exit_reason"], 0) + 1
        discipline = {
            "params": {"stop_pct": stop_pct, "breakeven_trigger": breakeven_trigger,
                       "max_hold_days": max_hold_days},
            "stats": {
                "hits": len(disc_details),
                "win_rate": round(float((sr > 0).mean() * 100), 1),
                "avg_return": round(float(sr.mean()), 2),
                "median_return": round(float(sr.median()), 2),
                "max_return": round(float(sr.max()), 2),
                "max_loss": round(float(sr.min()), 2),
                "avg_hold_days": round(float(hd_s.mean()), 1),
            },
            "exits": exits,
            "details": disc_details,
        }

    return {
        "strategy_code": strategy.code, "strategy_name": strategy.name,
        "buy_date": str(t0), "end_date": str(t_end),
        "hold_trading_days": days_ago, "stats": stats,
        "discipline": discipline,
        "top10": details[:10], "bottom5": details[-5:] if len(rets) > 10 else [],
    }
