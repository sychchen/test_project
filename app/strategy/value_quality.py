# -*- coding: utf-8 -*-
"""低估值+业绩质量 双因子策略(VALUE_QUALITY): VALUE_PCTL ∩ FIN_QUALITY

选股逻辑(两池硬条件取交集, 缺一不可):
  质量侧: FIN_QUALITY 全口径 —— ROE 年化当量达标 + 营收/净利双正增长
          + 毛利率稳定(vs 上一报告期) + 排除 ST/科创板/小市值
  估值侧: PB 处自身近 N 年低分位(<=max_pb_pctl) + PE-TTM>0 + 市值/历史天数下限
          —— 「比自己便宜」, 各行业口径自洽

评分: 两侧原始分各自在池内 rank-pct 归一化到 [0,1], 加权合成
  score = 100 * (quality_weight * q_rank + value_weight * v_rank)
  rank 归一化避免两侧量纲不可比(质量分 ~10-60, 估值分 ~0-25)。

防未来函数:
  质量侧: 复用 FIN_QUALITY, 财报按 announce_date <= t0 重放
  估值侧: 分位按 trade_date <= t0 的估值窗口现算(修正 VALUE_PCTL 在回测中
          读最新落库表引入的未来函数); 仅当 t0 恰为分位表最新日(实盘常态)
          才走落库表快路径
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import (
    EXCLUDED_CODE_PREFIXES, BaseStrategy, PickItem, register)
from app.strategy.fin_quality import FinQualityStrategy


class ValueQualityStrategy(BaseStrategy):
    code = "VALUE_QUALITY"
    name = "低估+质量双因子"
    default_params = {
        # ---- 估值侧(与 VALUE_PCTL 同口径) ----
        "lookback_years": 5,        # 历史窗口(年): PB 分位比较基准
        "max_pb_pctl": 0.20,        # PB 历史分位上限(最低的五分之一)
        "min_mv": 5_000_000_000,    # 总市值下限(元, 50亿, 防小盘壳)
        "min_hist_days": 250,       # 估值历史交易日下限
        "require_positive_pe": True,  # 排除亏损
        # ---- 质量侧(与 FIN_QUALITY 同口径) ----
        "min_roe_annual": 10.0,     # 年报 ROE 下限(%), 按报告期折算
        "max_gm_change_pp": 5.0,    # 毛利率与上期最大变化(pp)
        "min_mv_quality": 3_000_000_000,  # 质量侧市值下限(30亿)
        # ---- 组合 ----
        "quality_weight": 0.5,      # 质量分权重
        "top_n": 30,                # 双因子交集天然小, 取前 30
    }

    def __init__(self):
        self.last_note = None

    # ------------------------------------------------------------------
    def _value_pool_asof(self, t0, p):
        """估值侧硬筛选(as-of t0)。返回 dict: ts_code -> (score, reason)。"""
        days = int(p["lookback_years"] * 365)
        t0 = pd.Timestamp(t0)
        self._v_src = ""

        # 快路径: 分位落库表最新日 == t0(实盘常态), 直接用表
        df = None
        try:
            with engine.connect() as conn:
                td = conn.execute(text(
                    "SELECT MAX(trade_date) FROM stock_valuation_pctl")).scalar()
            if td is not None and pd.Timestamp(td) == t0:
                df = pd.read_sql(text(
                    "SELECT ts_code, trade_date, pb, pb_pctl, pe_ttm, pe_pctl, "
                    "total_mv, close_price, pct_chg, hist_days "
                    "FROM stock_valuation_pctl WHERE trade_date=:td"),
                    engine, params={"td": td})
                self._v_src = "分位落库表"
        except Exception:
            df = None

        if df is None:
            # 回测/落库表过期: 按 t0 截断窗口现算, 口径与 valuation_pctl 一致
            df = pd.read_sql(text(
                "SELECT ts_code, trade_date, pb, pe_ttm, total_mv, "
                "close_price, pct_chg FROM stock_valuation "
                "WHERE trade_date <= :t0 AND "
                "trade_date >= :t0 - INTERVAL %d DAY" % days),
                engine, params={"t0": t0.date()})
            self._v_src = "as-of现算"
            if df.empty:
                self.last_note = "stock_valuation 无数据, 先执行 --valuation"
                return {}
            df = df.dropna(subset=["pb"]).sort_values(["ts_code", "trade_date"])
            df["pb_pctl"] = df.groupby("ts_code")["pb"].rank(pct=True)
            df["pe_pctl"] = df.groupby("ts_code")["pe_ttm"].rank(pct=True)
            df["hist_days"] = df.groupby("ts_code")["pb"].transform("size")
            df = df.groupby("ts_code").tail(1).set_index("ts_code")
        else:
            df = df[~df["ts_code"].str.startswith(EXCLUDED_CODE_PREFIXES)]
            df = df.set_index("ts_code")

        out = {}
        for ts_code, r in df.iterrows():
            pb_pctl = float(r["pb_pctl"])
            if not (float(r["pb"]) > 0) or pb_pctl > p["max_pb_pctl"]:
                continue
            if p["require_positive_pe"] and \
                    (pd.isna(r["pe_ttm"]) or float(r["pe_ttm"]) <= 0):
                continue
            if pd.isna(r["total_mv"]) or float(r["total_mv"]) < p["min_mv"]:
                continue
            if int(r.get("hist_days", 0) or 0) < p["min_hist_days"]:
                continue
            # 估值侧分: 低分位越深越高(与 VALUE_PCTL 同构, 量纲将由 rank 归一化抹平)
            score = (p["max_pb_pctl"] - pb_pctl) * 100
            if pd.notna(r.get("pe_pctl")):
                score += max(0.0, p["max_pb_pctl"] - float(r["pe_pctl"])) * 25
            out[ts_code] = (round(score, 2),
                            f"PB{float(r['pb']):.2f}({pb_pctl:.0%}分位), "
                            f"PE-TTM{float(r['pe_ttm']):.1f}")
        return out

    # ------------------------------------------------------------------
    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame,
               params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        self.last_note = None
        t0 = quotes["trade_date"].max()

        # ---- 质量池: 复用 FIN_QUALITY 全逻辑(as-of 防未来函数, 不截断) ----
        fq = FinQualityStrategy()
        q_items = fq.select(quotes, basics, {
            "min_roe_annual": p["min_roe_annual"],
            "max_gm_change_pp": p["max_gm_change_pp"],
            "min_mv": p["min_mv_quality"],
            "top_n": None,
        })
        q_map = {it.ts_code: it for it in q_items}
        if not q_map:
            self.last_note = f"质量池为空({fq.last_note})"
            return []

        # ---- 估值池 ----
        v_map = self._value_pool_asof(t0, p)
        if not v_map:
            return []   # last_note 已设

        # ---- 交集 + rank 归一化加权 ----
        common = [c for c in q_map if c in v_map]
        if not common:
            self.last_note = (f"双因子交集为空: 质量池{len(q_map)} ∩ 估值池{len(v_map)}")
            return []
        q_ser = pd.Series({c: q_map[c].score for c in common})
        v_ser = pd.Series({c: v_map[c][0] for c in common})
        q_rank = q_ser.rank(pct=True)
        v_rank = v_ser.rank(pct=True)
        wq = float(p["quality_weight"])
        wv = 1.0 - wq

        name_map = dict(zip(basics["ts_code"], basics["name"]))
        last_q = (quotes.sort_values("trade_date", kind="mergesort")
                  .groupby("ts_code").tail(1).set_index("ts_code"))

        items: list[PickItem] = []
        for c in common:
            qi = q_map[c]
            close_px = pct_chg = None
            if c in last_q.index:
                row = last_q.loc[c]
                close_px = (float(row["close"]) if pd.notna(row["close"]) else None)
                pct_chg = (float(row["pct_chg"]) if pd.notna(row["pct_chg"]) else None)
            items.append(PickItem(
                ts_code=c, name=name_map.get(c, ""),
                trade_date=t0, close_price=close_px, pct_chg=pct_chg,
                score=round(100 * (wq * float(q_rank[c]) + wv * float(v_rank[c])), 2),
                hit_reason=f"{qi.hit_reason} | {v_map[c][1]}",
            ))

        items.sort(key=lambda x: x.score, reverse=True)
        total = len(items)
        if p["top_n"] and total > p["top_n"]:
            items = items[:p["top_n"]]
        self.last_note = (f"质量池{len(q_map)} ∩ 估值池{len(v_map)} = {total}只"
                          f"(估值分位{self._v_src})→取前{len(items)}")
        return items


register(ValueQualityStrategy())
