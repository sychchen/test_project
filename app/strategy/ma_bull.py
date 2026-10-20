# -*- coding: utf-8 -*-
"""均线多头策略: close>ma20 且 ma5>ma10>ma20 且 ma5 连续 N 日上行

2026-09-15 修正: 补 MA20 中期趋势确认（ma20_rising_days，默认 5 日）。
  市面「均线多头排列」标准写法为 MA5>MA10 AND MA10>MA20 AND MA20>REF(MA20,1)，
  原实现缺最后一项，会在均线纠缠/中期走平甚至下行时给出信号。
  实测原 436 只命中中 59 只(14%) 近 5 日 MA20 并未上行。
"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


class MaBullStrategy(BaseStrategy):
    code = "MA_BULL"
    name = "均线多头"
    default_params = {"ma_rising_days": 3, "ma20_rising_days": 5}

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        n = p["ma_rising_days"]
        m20d = int(p.get("ma20_rising_days", 5))
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:  # 剔除 ST 股
                continue
            if len(g) < 21 + n:
                continue
            g = g.sort_values("trade_date")
            last = g.iloc[-1]
            if pd.isna(last["ma20"]) or pd.isna(last["ma10"]) or pd.isna(last["ma5"]):
                continue
            # 多头排列 + 站上 ma20
            if not (last["ma5"] > last["ma10"] > last["ma20"] and last["close"] > last["ma20"]):
                continue
            # MA20 中期趋势向上（标准必要条件，防"下跌反弹中的短暂多头排列"）
            if len(g) < m20d + 1:
                continue
            ma20_prev = g["ma20"].iloc[-(m20d + 1)]
            if pd.isna(ma20_prev) or not (last["ma20"] > ma20_prev):
                continue
            # ma5 连续 n 日上行
            ma5_tail = g["ma5"].tail(n + 1).dropna()
            if len(ma5_tail) < n + 1 or not (ma5_tail.diff().dropna() > 0).all():
                continue
            score = round(float((last["ma5"] - last["ma20"]) / last["ma20"] * 100), 3)
            items.append(PickItem(
                ts_code=ts_code, name=name_map.get(ts_code, ""),
                trade_date=last["trade_date"],
                close_price=float(last["close"]), pct_chg=float(last["pct_chg"] or 0),
                score=score,
                hit_reason=(
                    f"MA5({last['ma5']:.2f})>MA10({last['ma10']:.2f})>MA20({last['ma20']:.2f}), "
                    f"收盘站上MA20, MA5连续{n}日上行, MA20{m20d}日上行"
                    f"({ma20_prev:.2f}->{last['ma20']:.2f})"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(MaBullStrategy())
