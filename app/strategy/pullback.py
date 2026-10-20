# -*- coding: utf-8 -*-
"""缩量回调策略: 处于上升趋势(MA20 上行) 中，近 N 日缩量回调至 MA10 附近，且不破 MA20
逻辑: 趋势未坏前提下的低吸信号

2026-09-15 口径修正:
  ① pullback_days 参数原先未真正生效 —— 代码取 g["high"].tail(n+3)(=6 日) 而非
     n 日，现改为「前 n 日(不含今日)最高价 -> 今日收盘回落幅度」，与参数语义一致。
  ② 量能基线改用「前 vol_ma_days 日均量(不含当日)」。库内 vol_ma20 含当日，
     会把当日缩量算进基线、导致"缩量"判定偏松；同时把命中原因的措辞
     从"量比"改为"缩量比"（原措辞与系统"量比"列同页不同义）。
  ③ 新增 require_bull_ma(默认 True): 要求 MA10 > MA20（多头排列）。
     经典回踩低吸是「多头排列 + 回踩 10 日线不破 20 日线」，原实现只要求
     MA20 上行 + 站上 MA20，会在均线纠缠/空头初期给出信号。
"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


class PullbackStrategy(BaseStrategy):
    code = "PULLBACK"
    name = "缩量回调"
    default_params = {"pullback_days": 3, "vol_shrink": 0.7, "near_ma10_pct": 3.0,
                      "vol_ma_days": 20, "require_bull_ma": True,
                      "pullback_min_pct": 1.0, "pullback_max_pct": 12.0}

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        n, vs, near = p["pullback_days"], p["vol_shrink"], p["near_ma10_pct"]
        vmd = p["vol_ma_days"]
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:
                continue
            if len(g) < max(26, vmd + 1):
                continue
            g = g.sort_values("trade_date")
            last = g.iloc[-1]
            if pd.isna(last["ma20"]) or pd.isna(last["ma10"]) or pd.isna(last["close"]):
                continue
            # 量能基线: 前 vmd 日均量(不含当日) —— 库内 vol_ma20 含当日
            vol_base = g["vol"].shift(1).rolling(vmd).mean().iloc[-1]
            if pd.isna(vol_base) or not vol_base:
                continue
            # 1) 趋势向上: MA20 高于 5 日前；多头排列(可选) MA10 > MA20
            ma20_prev = g["ma20"].iloc[-6] if len(g) >= 26 else g["ma20"].iloc[0]
            if pd.isna(ma20_prev) or not (last["ma20"] > ma20_prev):
                continue
            if p.get("require_bull_ma", True) and not (last["ma10"] > last["ma20"]):
                continue
            # 2) 近 n 日(不含今日)高点回落，有回调动作，但收盘仍守住 MA20
            recent_high = g["high"].iloc[-(n + 1):-1].max()
            if pd.isna(recent_high) or not recent_high:
                continue
            pullback_pct = (recent_high - last["close"]) / recent_high * 100
            lo = p.get("pullback_min_pct", 1.0)
            hi = p.get("pullback_max_pct", 12.0)
            if not (lo <= pullback_pct <= hi):   # 回调 1%~12% 视为良性
                continue
            if last["close"] < last["ma20"]:
                continue
            # 3) 回调缩量: 当日量低于前 vmd 日均量 × vol_shrink
            if not (last["vol"] < vol_base * vs):
                continue
            # 4) 价格贴近 MA10（±near%）
            dist_ma10 = abs(last["close"] - last["ma10"]) / last["ma10"] * 100
            if dist_ma10 > near:
                continue
            score = round(float((1 - last["vol"] / vol_base) * 100), 3)
            items.append(PickItem(
                ts_code=ts_code, name=name,
                trade_date=last["trade_date"],
                close_price=float(last["close"]), pct_chg=float(last["pct_chg"] or 0),
                score=score,
                hit_reason=(
                    f"MA20上行中近{n}日回调{pullback_pct:.1f}%不破MA20, "
                    f"缩量比{last['vol']/vol_base:.2f}(前{vmd}日均量), 贴近MA10({dist_ma10:.1f}%)"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(PullbackStrategy())
