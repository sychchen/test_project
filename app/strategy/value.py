# -*- coding: utf-8 -*-
"""低估值策略: 0<PE<max_pe 且 0<PB<max_pb 且 总市值>=min_mv(元)"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


class ValueStrategy(BaseStrategy):
    code = "VALUE"
    name = "低估值"
    default_params = {"max_pe": 20.0, "max_pb": 2.0, "min_mv": 10_000_000_000}  # 100亿

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        df = basics.dropna(subset=["pe_ttm", "pb", "total_mv"]).copy()
        df = df[(df["pe_ttm"] > 0) & (df["pe_ttm"] < p["max_pe"])
                & (df["pb"] > 0) & (df["pb"] < p["max_pb"])
                & (df["total_mv"] >= p["min_mv"])]
        # 剔除 ST
        df = df[~df["name"].str.contains("ST", na=False)]

        # 最新收盘价/交易日（若有行情则带出）
        latest = (
            quotes.sort_values("trade_date").groupby("ts_code").tail(1)
            .set_index("ts_code") if not quotes.empty else pd.DataFrame()
        )
        global_max_date = quotes["trade_date"].max() if not quotes.empty else None
        items: list[PickItem] = []
        for _, r in df.iterrows():
            close_price = pct_chg = None
            tdate = global_max_date
            if not latest.empty and r["ts_code"] in latest.index:
                q = latest.loc[r["ts_code"]]
                close_price = float(q["close"]) if pd.notna(q["close"]) else None
                pct_chg = float(q["pct_chg"]) if pd.notna(q["pct_chg"]) else None
                tdate = q["trade_date"]
            items.append(PickItem(
                ts_code=r["ts_code"], name=r["name"],
                trade_date=tdate, close_price=close_price, pct_chg=pct_chg,
                score=round(float(p["max_pe"] - r["pe_ttm"]), 3),
                hit_reason=(
                    f"PE-TTM {r['pe_ttm']:.2f}(<{p['max_pe']}), PB {r['pb']:.2f}(<{p['max_pb']}), "
                    f"总市值 {r['total_mv']/1e8:.0f}亿"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(ValueStrategy())
