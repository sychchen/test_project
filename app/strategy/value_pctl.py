# -*- coding: utf-8 -*-
"""低估值分位策略: 当前 PB 处于自身近 N 年历史低分位 —— 「比自己便宜」而非绝对阈值

与 VALUE(绝对阈值: PE<20/PB<2) 互补:
  绝对阈值对银行/地产等天然低 PB 行业无区分度, 对成长股又永远选不中;
  历史分位是「自身纵向比较」, 各行业口径自洽。

数据源: stock_valuation (东财估值全历史, 采集: run_collect.py --valuation)
口径:
  pb_pctl = 近 lookback_years 年内 PB <= 当前 PB 的交易日占比(含当日, rank pct)
  条件: pb_pctl <= max_pb_pctl(默认0.20) 且 PB>0 且 PE-TTM>0(排除亏损)
        且 总市值>=min_mv 且 历史交易日>=min_hist_days(分位统计意义下限)
  评分: (max_pb_pctl - pb_pctl)*100 + PE 分位加成, 越高越低估
性能: 只读近 N 年 7 列(~600万行), pandas groupby rank, 全程 < 1 分钟。
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import (
    EXCLUDED_CODE_PREFIXES, BaseStrategy, PickItem, register)


class ValuePctlStrategy(BaseStrategy):
    code = "VALUE_PCTL"
    name = "低估值分位"
    default_params = {
        "lookback_years": 5,        # 历史窗口(年): PB 分位的比较基准
        "max_pb_pctl": 0.20,        # PB 历史分位上限(0.2=最低的五分之一)
        "min_mv": 5_000_000_000,    # 总市值下限(元, 默认50亿, 防小盘壳)
        "min_hist_days": 250,       # 历史交易日下限(分位统计意义)
        "require_positive_pe": True,  # 排除亏损股(PE-TTM<=0)
        "use_pe_pctl_bonus": True,  # 评分中纳入 PE 分位加成
        "top_n": 50,                # 输出数量上限: 分位是池级筛选(全市场约20%会过线),
                                    # 按低估程度(score)截断到可跟踪规模
    }

    def __init__(self):
        self.last_note = None
        self._src_desc = ""

    def _load_pctl_table(self):
        """读 stock_valuation_pctl 最新交易日(daily_update 日度落库)。
        返回 (last_df索引=ts_code, hist_series) 或 (None, None) 触发回退。"""
        try:
            with engine.connect() as conn:
                td = conn.execute(text(
                    "SELECT MAX(trade_date) FROM stock_valuation_pctl")).scalar()
                if not td:
                    return None, None
                df = pd.read_sql(text("""
                    SELECT ts_code, trade_date, pb, pb_pctl, pe_ttm, pe_pctl,
                           total_mv, close_price, pct_chg, hist_days
                    FROM stock_valuation_pctl WHERE trade_date=:td
                """), conn, params={"td": td})
        except Exception:
            return None, None   # 表不存在(未跑 --init-db)等, 回退全量
        if df.empty:
            return None, None
        # 分位表绕过框架 universe 过滤(自查数据), 自行剔除科创板(688/689)
        df = df[~df["ts_code"].str.startswith(EXCLUDED_CODE_PREFIXES)]
        self._src_desc = f"分位表@{td}"
        return df.set_index("ts_code"), df.set_index("ts_code")["hist_days"]

    def _compute_pctl_full(self, p):
        """回退: 全量计算分位(与 app/collector/valuation_pctl.py 落库口径一致)"""
        days = int(p["lookback_years"] * 365)
        df = pd.read_sql(text("""
            SELECT ts_code, trade_date, pb, pe_ttm, total_mv, close_price, pct_chg
            FROM stock_valuation
            WHERE trade_date >= (SELECT d FROM (SELECT MAX(trade_date) d
                                 FROM stock_valuation) x) - INTERVAL %d DAY
        """ % days), engine)
        if df.empty:
            self.last_note = "stock_valuation 无数据, 请先执行 run_collect.py --valuation"
            return None, None
        df = df[~df["ts_code"].str.startswith(EXCLUDED_CODE_PREFIXES)]
        df = df.dropna(subset=["pb"]).sort_values(["ts_code", "trade_date"])
        df["pb_pctl"] = df.groupby("ts_code")["pb"].rank(pct=True)
        if p["use_pe_pctl_bonus"]:
            df["pe_pctl"] = df.groupby("ts_code")["pe_ttm"].rank(pct=True)
        hist = df.groupby("ts_code").size()
        last = df.groupby("ts_code").tail(1).set_index("ts_code")
        self._src_desc = "全量计算"
        return last, hist

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        self.last_note = None

        # ---- 优先读分位落库表(秒级, daily_update 链路日度维护);
        #      表空/未建时回退全量计算(~6分钟), 口径与 valuation_pctl 落库一致 ----
        last, hist = self._load_pctl_table()
        if last is None:
            last, hist = self._compute_pctl_full(p)
        if last is None:
            return []   # last_note 已在子函数中设置

        name_map = dict(zip(basics["ts_code"], basics["name"]))
        items: list[PickItem] = []
        skipped_hist = 0
        for ts_code, r in last.iterrows():
            name = name_map.get(ts_code, "")
            if "ST" in name:
                continue
            n_hist = int(hist.get(ts_code, 0))
            if n_hist < p["min_hist_days"]:
                skipped_hist += 1
                continue
            if not (r["pb"] > 0) or r["pb_pctl"] > p["max_pb_pctl"]:
                continue
            if p["require_positive_pe"] and (pd.isna(r["pe_ttm"]) or r["pe_ttm"] <= 0):
                continue
            if pd.isna(r["total_mv"]) or r["total_mv"] < p["min_mv"]:
                continue
            pb_pctl = float(r["pb_pctl"])
            score = round((p["max_pb_pctl"] - pb_pctl) * 100, 2)
            pe_desc = ""
            if p["use_pe_pctl_bonus"] and pd.notna(r.get("pe_pctl")):
                pe_pctl = float(r["pe_pctl"])
                # PE 同处低分位时加成(最多 +5), 不处低分位不扣分
                bonus = max(0.0, (p["max_pb_pctl"] - pe_pctl)) * 25
                score = round(score + bonus, 2)
                pe_desc = f", PE-TTM {float(r['pe_ttm']):.1f}({pe_pctl:.0%}分位)"
            elif pd.notna(r["pe_ttm"]):
                pe_desc = f", PE-TTM {float(r['pe_ttm']):.1f}"
            items.append(PickItem(
                ts_code=ts_code, name=name,
                trade_date=r["trade_date"],
                close_price=(float(r["close_price"]) if pd.notna(r["close_price"]) else None),
                pct_chg=(float(r["pct_chg"]) if pd.notna(r["pct_chg"]) else None),
                score=score,
                hit_reason=(
                    f"PB {float(r['pb']):.2f} 处近{p['lookback_years']}年 "
                    f"{pb_pctl:.1%} 分位(<={p['max_pb_pctl']:.0%}){pe_desc}, "
                    f"总市值 {float(r['total_mv'])/1e8:.0f}亿, 历史{n_hist}天"
                ),
            ))

        items.sort(key=lambda x: x.score, reverse=True)
        total_hit = len(items)
        if p["top_n"] and total_hit > p["top_n"]:
            items = items[:p["top_n"]]
        self.last_note = (f"[{self._src_desc}] 估值池{len(last)}只"
                          f"(历史不足{skipped_hist}只跳过)"
                          f"→过线{total_hit}只→取前{len(items)}只")
        return items


register(ValuePctlStrategy())
