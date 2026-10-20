# -*- coding: utf-8 -*-
"""一周主线低吸策略（2026-09-23 按 epic 需求整体调整选股规则）
来源: epic/epic_description.xlsx 行4（2026-09-23）:
  「调整"一周主线低吸"策略为：连续5日收阳线，5日均线大于10日均线，
   10日均线大于20日均线，20日均线向上，近5日涨幅小于15%，非ST，
   流通市值大于30亿 并且流通市值小于1000亿」

实现口径（全部基于 stock_daily_quote / stock_basic 库内数据, 无网络请求）:
  连续5日收阳线   = 最近 5 根 K 线均 close > open（阳线按开收盘价判定）
  均线多头        = ma5 > ma10 > ma20（库内预计算列, 口径与采集链一致）
  20日均线向上    = 当日 ma20 > 昨日 ma20
  近5日涨幅       = close[-1]/close[-6] - 1, 必须 < chg5_max%(默认 15, 不设下限)
  非ST            = stock_basic.name 不含 "ST"
  流通市值        = stock_basic.circ_mv, mcap_min(30亿) < circ_mv < mcap_max(1000亿)
  股票池          = 沿用全局过滤(大A, 剔除科创板 688/689)
评分: ma5 相对 ma20 的偏离度%(多头排列强度), 越强越靠前。
历史说明: 原实现为主线池(AI算力硬件)/轮动池 双池 + 大盘环境闸门 + 主力资金过滤,
  按 epic 需求调整为上述单规则选股; 原参数与辅助函数已随旧逻辑移除。
"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


class WeeklyMainlineStrategy(BaseStrategy):
    code = "WEEKLY_MAINLINE"
    name = "一周主线低吸"
    default_params = {
        "yang_days": 5,        # 连续收阳天数
        "chg5_max": 15.0,      # 近5日涨幅上限%(不设下限)
        "mcap_min": 30.0,      # 流通市值下限(亿)
        "mcap_max": 1000.0,    # 流通市值上限(亿)
    }

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame,
               params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        nd = int(p["yang_days"])
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))
        mcap_map = dict(zip(
            basics["ts_code"],
            pd.to_numeric(basics["circ_mv"], errors="coerce") / 1e8))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:  # 非ST
                continue
            mc = mcap_map.get(ts_code)
            if mc is None or pd.isna(mc) or not (
                    p["mcap_min"] < mc < p["mcap_max"]):  # 流通市值区间
                continue
            if len(g) < nd + 21:  # 5 阳 + ma20 向上至少需 21 根
                continue
            g = g.sort_values("trade_date")
            last = g.iloc[-1]
            # 连续 nd 日收阳线
            tail = g.tail(nd)
            if tail[["open", "close"]].isna().any().any():
                continue
            if not (tail["close"] > tail["open"]).all():
                continue
            # 均线多头: ma5 > ma10 > ma20（预计算列）
            if pd.isna(last["ma5"]) or pd.isna(last["ma10"]) or pd.isna(last["ma20"]):
                continue
            if not (last["ma5"] > last["ma10"] > last["ma20"]):
                continue
            # 20日均线向上
            ma20_prev = g["ma20"].iloc[-2]
            if pd.isna(ma20_prev) or not (last["ma20"] > ma20_prev):
                continue
            # 近5日涨幅 < chg5_max%
            if len(g) < 6 or pd.isna(g["close"].iloc[-6]):
                continue
            base = float(g["close"].iloc[-6])
            if base == 0:
                continue
            chg5 = round((float(last["close"]) / base - 1) * 100, 2)
            if not chg5 < p["chg5_max"]:
                continue
            score = round(float(last["ma5"] - last["ma20"]) / last["ma20"] * 100, 3)
            items.append(PickItem(
                ts_code=ts_code, name=name,
                trade_date=last["trade_date"],
                close_price=float(last["close"]),
                pct_chg=float(last["pct_chg"]) if pd.notna(last["pct_chg"]) else 0.0,
                score=score,
                hit_reason=(
                    f"连续{nd}日收阳, MA5({last['ma5']:.2f})>MA10({last['ma10']:.2f})"
                    f">MA20({last['ma20']:.2f})且MA20上行({ma20_prev:.2f}->"
                    f"{last['ma20']:.2f}), 近5日{chg5}%<{p['chg5_max']}%, "
                    f"市值{mc:.0f}亿({p['mcap_min']:.0f}~{p['mcap_max']:.0f})"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(WeeklyMainlineStrategy())
