# -*- coding: utf-8 -*-
"""MACD 金叉策略: DIF 上穿 DEA（金叉），且 DIF 在零轴附近或上方
MACD 参数: 12/26/9（EMA 基于收盘价）

历史长度要求（2026-09-15 修正）:
  EMA 是递归平滑，DIF/DEA 需足够长样本才收敛。原 guard 为 len>=10，对次新股会
  算出严重失真的 DIF/DEA（12/26/9 至少需 ~35 根，推荐 60+）。现要求 min_bars
  (默认 60)。实测改前 4590 只中仅 15 只不足 60 根且无一命中 —— 本次修正不改变
  现有信号，只消除次新股产生伪金叉的隐患。
"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


class MacdGoldenStrategy(BaseStrategy):
    code = "MACD_GOLDEN"
    name = "MACD金叉"
    default_params = {"fast": 12, "slow": 26, "signal": 9, "above_zero": False,
                      "min_bars": 60}

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        # EMA(12/26/9) 收敛所需的最少样本；不足则信号不可靠，直接跳过
        min_bars = max(int(p.get("min_bars", 60)), p["slow"] + p["signal"])
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:  # 剔除 ST 股
                continue
            # 样本不足(多为次新股) -> 跳过，避免产出未收敛的伪金叉
            if len(g) < min_bars:
                continue
            g = g.sort_values("trade_date")
            close = g["close"].dropna()
            if len(close) < min_bars:
                continue
            ema_fast = close.ewm(span=p["fast"], adjust=False).mean()
            ema_slow = close.ewm(span=p["slow"], adjust=False).mean()
            dif = ema_fast - ema_slow
            dea = dif.ewm(span=p["signal"], adjust=False).mean()
            if len(dif) < 2:
                continue
            d0, d1 = dif.iloc[-2], dif.iloc[-1]
            e0, e1 = dea.iloc[-2], dea.iloc[-1]
            # 金叉: 昨 DIF<=DEA, 今 DIF>DEA
            if not (d0 <= e0 and d1 > e1):
                continue
            if p["above_zero"] and d1 <= 0:
                continue
            last = g.iloc[-1]
            items.append(PickItem(
                ts_code=ts_code, name=name,
                trade_date=last["trade_date"],
                close_price=float(last["close"]), pct_chg=float(last["pct_chg"] or 0),
                score=round(float(d1 - e1), 4),
                hit_reason=(
                    f"MACD金叉: DIF {d0:.3f}->{d1:.3f} 上穿 DEA {e0:.3f}->{e1:.3f}"
                    + ("（零轴上方）" if d1 > 0 else "（零轴下方）")
                    + f", 样本{len(close)}根"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(MacdGoldenStrategy())
