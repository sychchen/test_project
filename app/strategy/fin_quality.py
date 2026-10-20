# -*- coding: utf-8 -*-
"""业绩质量策略: 财报硬指标筛选 —— 「业绩硬」池, 与 VALUE_PCTL(「便宜」池)互补

数据源: stock_fin_report(东财业绩报表, 采集: run_collect.py --finreport,
8 报告期 2024-09-30 → 2026-06-30, ~7 万行)。字段自带累计同比
(revenue_yoy / net_profit_yoy), 免自行拼同比。

口径(2026-09-21 与方案文档对齐):
  1) ROE 达标(按报告期长度折算年化当量): roe >= min_roe_annual * months/12
     (Q1: 2.5 / H1: 5 / Q3: 7.5 / 年报: 10, 默认 min_roe_annual=10)
     —— 东财 ROE 为报告期累计值, 单季度直接卡 10 会误杀 Q1/H1 优质股
  2) 增长双正: revenue_yoy > 0 且 net_profit_yoy > 0
  3) 毛利率稳定: |gross_margin - 上一报告期| <= max_gm_change_pp(默认 5pp)
     —— 防周期顶毛利率假象; 无上一期(次新)不参与
  4) 排除 ST / 科创板(688/689) / 总市值 < min_mv(默认 30 亿)
防未来函数: 只用 announce_date <= t0 的报告(回测截断日选股视角),
  与 VALUE_PCTL 读"最新落库表"不同, 本策略从原始财报表按公告日重放。
评分: ROE 年化当量 + 净利同比加成(上限 50 封顶*0.4) + 营收同比加成(*0.2)。
组合思路: 与 VALUE_PCTL 交集 = 「低估值+业绩硬」双因子(后续可做合一策略)。
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import (
    EXCLUDED_CODE_PREFIXES, BaseStrategy, PickItem, register)

_REPORT_MONTHS = {3: 3, 6: 6, 9: 9, 12: 12}   # 报告期月份 → 累计月数


class FinQualityStrategy(BaseStrategy):
    code = "FIN_QUALITY"
    name = "业绩质量"
    default_params = {
        "min_roe_annual": 10.0,     # 年报 ROE 下限(%), 按报告期折算
        "max_gm_change_pp": 5.0,    # 毛利率与上期最大变化(pp)
        "min_mv": 3_000_000_000,    # 总市值下限(元, 默认30亿)
        "top_n": 50,                # 质量池天然较宽, 按 score 截断
    }

    def __init__(self):
        self.last_note = None

    def _load_reports_asof(self, t0) -> pd.DataFrame:
        """加载 announce_date <= t0 的全部报告(7万行全表, 亚秒级)。"""
        df = pd.read_sql(text(
            "SELECT ts_code, report_date, roe, gross_margin, "
            "revenue_yoy, net_profit_yoy, announce_date, industry "
            "FROM stock_fin_report WHERE announce_date IS NOT NULL"), engine)
        if df.empty:
            return df
        df = df[pd.to_datetime(df["announce_date"]) <= pd.to_datetime(t0)]
        df = df[~df["ts_code"].str.startswith(EXCLUDED_CODE_PREFIXES)]
        return df.sort_values(["ts_code", "report_date"], kind="mergesort")

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame,
               params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        self.last_note = None

        t0 = quotes["trade_date"].max()   # 回测=截断日; 实盘=最新交易日
        reports = self._load_reports_asof(t0)
        if reports.empty:
            self.last_note = "stock_fin_report 无数据, 请先执行 run_collect.py --finreport"
            return []

        # 每股取 as-of 视角下最新一期 + 上一期(毛利率稳定性的比较基准)
        reports["rk"] = reports.groupby("ts_code").cumcount(ascending=False)
        latest = reports[reports["rk"] == 0].set_index("ts_code")
        prev = reports[reports["rk"] == 1].set_index("ts_code")

        name_map = dict(zip(basics["ts_code"], basics["name"]))
        mv_map = (pd.to_numeric(basics.set_index("ts_code").get("total_mv"),
                                errors="coerce")
                  if "total_mv" in basics.columns else pd.Series(dtype=float))
        # 行情快照映射(t0 收盘/涨跌幅), 避免循环内全帧过滤 O(n²)
        last_q = (quotes.sort_values("trade_date", kind="mergesort")
                  .groupby("ts_code").tail(1).set_index("ts_code"))

        items: list[PickItem] = []
        n_skip_st = n_skip_mv = n_skip_roe = n_skip_grow = n_skip_gm = 0
        for ts_code, r in latest.iterrows():
            name = name_map.get(ts_code, "")
            if "ST" in name:
                n_skip_st += 1
                continue
            mv = mv_map.get(ts_code)
            if pd.isna(mv) or float(mv) < p["min_mv"]:
                n_skip_mv += 1
                continue
            months = _REPORT_MONTHS.get(pd.Timestamp(r["report_date"]).month)
            roe = pd.to_numeric(r["roe"], errors="coerce")
            if months is None or pd.isna(roe):
                n_skip_roe += 1
                continue
            roee = float(roe) * 12 / months     # 年化当量
            if roee < p["min_roe_annual"]:
                n_skip_roe += 1
                continue
            rev_yoy = pd.to_numeric(r["revenue_yoy"], errors="coerce")
            np_yoy = pd.to_numeric(r["net_profit_yoy"], errors="coerce")
            if pd.isna(rev_yoy) or pd.isna(np_yoy) or rev_yoy <= 0 or np_yoy <= 0:
                n_skip_grow += 1
                continue
            gm = pd.to_numeric(r["gross_margin"], errors="coerce")
            if ts_code not in prev.index:
                n_skip_gm += 1
                continue
            gm_prev = pd.to_numeric(prev.loc[ts_code, "gross_margin"],
                                    errors="coerce")
            if pd.isna(gm) or pd.isna(gm_prev) or \
                    abs(float(gm) - float(gm_prev)) > p["max_gm_change_pp"]:
                n_skip_gm += 1
                continue

            score = round(
                roee
                + max(0.0, min(float(np_yoy), 50.0)) * 0.4
                + max(0.0, min(float(rev_yoy), 50.0)) * 0.2, 2)
            gm_chg = float(gm) - float(gm_prev)
            close_px = pct_chg = None
            if ts_code in last_q.index:
                row = last_q.loc[ts_code]
                close_px = (float(row["close"]) if pd.notna(row["close"]) else None)
                pct_chg = (float(row["pct_chg"]) if pd.notna(row["pct_chg"]) else None)
            items.append(PickItem(
                ts_code=ts_code, name=name,
                trade_date=t0,
                close_price=close_px,
                pct_chg=pct_chg,
                score=score,
                hit_reason=(
                    f"{pd.Timestamp(r['report_date']):%y}Q{(months + 2) // 3} "
                    f"ROE{float(roe):.1f}%(年化当量{roee:.1f}), "
                    f"营收{rev_yoy:+.1f}%/净利{np_yoy:+.1f}%, "
                    f"毛利率{float(gm):.1f}%({gm_chg:+.1f}pp vs 上期)"
                ),
            ))

        items.sort(key=lambda x: x.score, reverse=True)
        total_hit = len(items)
        if p["top_n"] and total_hit > p["top_n"]:
            items = items[:p["top_n"]]
        self.last_note = (
            f"财报as-of@{t0} 池{len(latest)}只 "
            f"(ST{ n_skip_st}/市值{n_skip_mv}/ROE{n_skip_roe}"
            f"/增长{n_skip_grow}/毛利率{n_skip_gm} 跳过)"
            f"→过线{total_hit}只→取前{len(items)}只")
        return items


register(FinQualityStrategy())
