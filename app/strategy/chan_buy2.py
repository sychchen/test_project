# -*- coding: utf-8 -*-
"""缠论二买策略（CHAN_BUY2，简化缠论的日线量化）

缠论原文口径 → 本实现简化映射（简化点逐项明示，不夸大精度）：
  一买 = 下跌末段底背驰的底分型低点；
  二买 = 一买反弹后的回踩不再跌破一买低点、并再次底分型确认的买点（二买高于一买）。

实现步骤（日线）：
  1) 一买低点 L1：最近 lookback_days 窗口内的底分型低点（当日 low 为左右各 1 根
     最低；K 线包含关系不处理 —— 简化项），候选从近到远取首个结构完整者；
  2) 反弹：L1 之后最高价 H1，反弹幅度 ≥ rebound_min_pct；
  3) 回踩：H1 之后最低 low = L2，要求 L2 ≥ L1×(1-break_tolerance_pct)（不破一买），
     回踩幅度 (H1-L2)/H1 ∈ [pullback_min_pct, pullback_max_pct]，
     且 L2 本身为底分型；
  4) 确认：最近一根 K 线距 L2 ≤ confirm_days 日、收阳（pct_chg>0）、收盘 > L2；
  5) 评分加成：回踩缩量（回踩段均量 < 反弹段均量）+15、
     MACD DIF 底背驰（L2 处 DIF > L1 处 DIF，价格双低而动能抬高）+15、
     收盘站上 MA5 +5；require_vol_shrink=True 时缩量转为硬过滤。

score = 40 + min(反弹幅度,20) + 缩量15 + DIF背驰15 + 站上MA5 5 - 回踩幅度
"""
import numpy as np
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _bottom_fractal(low: np.ndarray, i: int) -> bool:
    """简化底分型：第 i 根 low 严格低于左右各 1 根（不处理包含关系）"""
    return low[i] < low[i - 1] and low[i] < low[i + 1]


class ChanBuy2Strategy(BaseStrategy):
    code = "CHAN_BUY2"
    name = "缠论二买"
    default_params = {
        "lookback_days": 60,        # 结构回看窗口（交易日）
        "rebound_min_pct": 5.0,     # 一买后反弹最小幅度%
        "pullback_min_pct": 2.0,    # 回踩最小幅度%（没回踩不算二买）
        "pullback_max_pct": 15.0,   # 回踩最大幅度%（跌太深结构走坏）
        "confirm_days": 3,          # L2 底分型后确认窗口
        "break_tolerance_pct": 0.0, # 破一买低点容差%（0=严格不破）
        "require_vol_shrink": False,  # 缩量回踩是否硬过滤（默认加分项）
        "min_hist_days": 60,
        "top_n": 50,
    }

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        lb = int(p["lookback_days"])
        reb_min = float(p["rebound_min_pct"])
        pb_lo, pb_hi = float(p["pullback_min_pct"]), float(p["pullback_max_pct"])
        cfm = int(p["confirm_days"])
        tol = float(p["break_tolerance_pct"])
        shrink_hard = bool(p.get("require_vol_shrink", False))
        min_hist = int(p["min_hist_days"])
        top_n = int(p.get("top_n", 50))
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:
                continue
            g = g.sort_values("trade_date")
            if len(g) < min_hist:
                continue
            low = pd.to_numeric(g["low"], errors="coerce").values
            high = pd.to_numeric(g["high"], errors="coerce").values
            close = pd.to_numeric(g["close"], errors="coerce").values
            vol = pd.to_numeric(g["vol"], errors="coerce").values
            dates = g["trade_date"].tolist()
            n = len(g)
            dif = (_ema(g["close"], 12) - _ema(g["close"], 26)).values
            last = g.iloc[-1]
            if np.isnan(low).any() or np.isnan(close).any():
                continue

            w0 = max(1, n - lb)          # 窗口起点（留 1 根给分型左臂）
            found = None
            # 候选一买：窗口内底分型，从近到远，首个结构完整者命中
            cands = [i for i in range(w0 + 1, n - 6)  # 一买后至少留 5 根走反弹+回踩
                     if _bottom_fractal(low, i)]
            for i1 in reversed(cands):
                L1 = low[i1]
                if L1 <= 0:
                    continue
                # 反弹段 (i1, n)
                seg_h = high[i1 + 1:]
                if len(seg_h) < 2:
                    continue
                h1_rel = int(np.nanargmax(seg_h))
                iH = i1 + 1 + h1_rel
                H1 = high[iH]
                rebound_pct = (H1 / L1 - 1) * 100
                if rebound_pct < reb_min or iH + 1 >= n:
                    continue
                # 回踩段 (iH, n)
                seg_l = low[iH + 1:]
                if len(seg_l) < 1:
                    continue
                l2_rel = int(np.nanargmin(seg_l))
                i2 = iH + 1 + l2_rel
                L2 = low[i2]
                if L2 < L1 * (1 - tol / 100):        # 破一买低点 → 二买不成立
                    continue
                pullback_pct = (1 - L2 / H1) * 100
                if not (pb_lo <= pullback_pct <= pb_hi):
                    continue
                # L2 须为底分型（右臂允许就是最后一根 → 用 i2+1<=n-1 判断）
                if i2 + 1 > n - 1 or not _bottom_fractal(low, i2):
                    continue
                # 确认：最后一根距 L2 ≤ confirm_days、收阳、收盘脱离 L2
                if n - 1 - i2 > cfm:
                    continue
                if not (close[-1] > L2 and (last["pct_chg"] or 0) > 0):
                    continue
                # 缩量回踩：回踩段均量 < 反弹段均量
                v_reb = np.nanmean(vol[i1 + 1:iH + 1]) if iH > i1 else np.nan
                v_pb = np.nanmean(vol[iH + 1:i2 + 1]) if i2 > iH else np.nan
                shrink = (not np.isnan(v_reb) and not np.isnan(v_pb)
                          and v_reb > 0 and v_pb < v_reb)
                if shrink_hard and not shrink:
                    continue
                # DIF 底背驰：L2 处 DIF > L1 处 DIF（价格相近双低而动能抬高）
                dif_up = dif[i2] > dif[i1]
                above_ma5 = (pd.notna(last["ma5"]) and close[-1] > last["ma5"])
                score = round(40 + min(rebound_pct, 20) + (15 if shrink else 0)
                              + (15 if dif_up else 0) + (5 if above_ma5 else 0)
                              - pullback_pct, 3)
                found = PickItem(
                    ts_code=ts_code, name=name, trade_date=dates[-1],
                    close_price=float(close[-1]),
                    pct_chg=float(last["pct_chg"] or 0), score=score,
                    hit_reason=(
                        f"一买L1={L1:.2f}({dates[i1]}), 反弹{rebound_pct:.1f}%后"
                        f"回踩{pullback_pct:.1f}%不破一买(L2={L2:.2f},{dates[i2]}), "
                        f"{'缩量' if shrink else '放量'}回踩, DIF{'抬高' if dif_up else '走低'}, "
                        f"{'站上MA5' if above_ma5 else 'MA5下方'}, 确认日{dates[-1]}"
                    ))
                break
            if found is not None:
                items.append(found)

        items.sort(key=lambda x: x.score, reverse=True)
        return items[:top_n]


register(ChanBuy2Strategy())
