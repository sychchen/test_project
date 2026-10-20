# -*- coding: utf-8 -*-
"""连续阳线策略: 连续 N 日收阳（close>open），累计涨幅温和（防追高），量能温和放大

2026-09-15 口径修正: 量能基线改用「前 vol_ma_days 日均量(不含当日)」（库内
vol_ma20 含当日），并把命中原因的措辞从"量比"改为"量能倍数"——
原措辞与系统"量比"列（当日量/前5日均量）同页不同义，易误读。
"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


class ConsecutiveYangStrategy(BaseStrategy):
    code = "CONSEC_YANG"
    name = "连续阳线"
    default_params = {"days": 4, "max_total_gain": 15.0, "vol_ma_days": 20,
                      "max_vol_multiple": 3.0}

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        n, max_gain = p["days"], p["max_total_gain"]
        vmd = p["vol_ma_days"]
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:
                continue
            if len(g) < max(n + 1, vmd + 1):
                continue
            g = g.sort_values("trade_date")
            tail = g.tail(n)
            # 连续 n 日收阳
            if not ((tail["close"] > tail["open"]).all()):
                continue
            # 每日上涨（收盘价逐日抬升）
            if not (tail["close"].diff().dropna() > 0).all():
                continue
            last = g.iloc[-1]
            # 累计涨幅 = 相对 n 日前收盘，防追高
            base_close = g["close"].iloc[-(n + 1)]
            total_gain = (last["close"] / base_close - 1) * 100
            if not (0 < total_gain <= max_gain):
                continue
            # 量能温和: 当日量不超过前 vmd 日均量的 max_vol_multiple 倍（排除爆量出货嫌疑）
            vol_base = g["vol"].shift(1).rolling(vmd).mean().iloc[-1]
            vol_mult = None
            if pd.notna(vol_base) and vol_base:
                vol_mult = float(last["vol"] / vol_base)
                if vol_mult > p["max_vol_multiple"]:
                    continue
            score = round(float(total_gain), 3)
            items.append(PickItem(
                ts_code=ts_code, name=name,
                trade_date=last["trade_date"],
                close_price=float(last["close"]), pct_chg=float(last["pct_chg"] or 0),
                score=score,
                hit_reason=(
                    f"连续{n}日收阳且逐日走高, 累计涨幅{total_gain:.1f}%(<{max_gain}%), "
                    f"量能倍数{vol_mult if vol_mult is not None else 0:.2f}"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(ConsecutiveYangStrategy())
