# -*- coding: utf-8 -*-
"""放量突破策略: 收盘创近N日新高 且 成交量>=M倍前N日均量 且 涨幅>min_pct%

2026-09-15 口径修正（两处）:
  ① 量能基线改用「前 window 日均量(不含当日)」。
     库内 vol_ma20 = vol.rolling(20).mean() 含当日，把当日放量算进基线，
     导致名义"2 倍量"实际约为 2.02 倍（实测中位数偏差 +0.9%，方向更严）。
     现与市面「倍量」口径对齐，并把命中原因里的措辞从"量比"改为"放量倍数"
     （原措辞与系统"量比"列 = 当日量/前5日均量 同页不同义，易误读）。
  ② 新增 exclude_limit_up(默认 True): 排除当日封涨停的标的。
     实测原 61 只命中中 24 只(39%) 涨幅 >= 9.5%（含 10%/20% 涨停板），
     这些个股次日无法按当日收盘价买入，会造成回测/实盘口径失真。
     涨停幅度按板块判定: 创业板(30*) 20%，主板(60*/00*) 10%，留 0.3% 余量。
"""
import pandas as pd

from app.strategy.base import BaseStrategy, PickItem, register


def _limit_up_pct(ts_code: str) -> float:
    """按板块返回涨停幅度(%): 创业板(300/301) 20%，其余主板 10%"""
    return 20.0 if ts_code[:2] == "30" else 10.0


class VolumeBreakoutStrategy(BaseStrategy):
    code = "VOLUME_BREAKOUT"
    name = "放量突破"
    default_params = {"window": 20, "vol_multiple": 2.0, "min_pct_chg": 3.0,
                      "exclude_limit_up": True}

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        w, vm, min_pct = p["window"], p["vol_multiple"], p["min_pct_chg"]
        excl_lu = p.get("exclude_limit_up", True)
        items: list[PickItem] = []
        name_map = dict(zip(basics["ts_code"], basics["name"]))

        for ts_code, g in quotes.groupby("ts_code"):
            name = name_map.get(ts_code, "")
            if "ST" in name:  # 剔除 ST 股
                continue
            if len(g) < w + 1:
                continue
            g = g.sort_values("trade_date")
            last = g.iloc[-1]
            # 量能基线: 前 w 日均量(不含当日)，避免把当日放量算进基线
            vol_base = g["vol"].shift(1).rolling(w).mean().iloc[-1]
            if pd.isna(vol_base) or not vol_base:
                continue
            if pd.isna(last["close"]) or pd.isna(last["pct_chg"]):
                continue
            # 涨停/封板当日无法按收盘价买入 -> 排除（回测可执行性）
            if excl_lu and float(last["pct_chg"]) >= _limit_up_pct(ts_code) - 0.3:
                continue
            prev_max_close = g["close"].iloc[-(w + 1):-1].max()
            cond_break = last["close"] >= prev_max_close
            cond_vol = last["vol"] >= vm * vol_base
            cond_pct = float(last["pct_chg"]) > min_pct
            if not (cond_break and cond_vol and cond_pct):
                continue
            ratio = float(last["vol"] / vol_base)
            items.append(PickItem(
                ts_code=ts_code, name=name_map.get(ts_code, ""),
                trade_date=last["trade_date"],
                close_price=float(last["close"]), pct_chg=float(last["pct_chg"] or 0),
                score=round(ratio, 3),
                hit_reason=(
                    f"收盘创近{w}日新高({last['close']:.2f}>={prev_max_close:.2f}), "
                    f"放量倍数{ratio:.2f}(>={vm}, 前{w}日均量), 涨幅{float(last['pct_chg']):.2f}%"
                ),
            ))
        items.sort(key=lambda x: x.score, reverse=True)
        return items


register(VolumeBreakoutStrategy())
