# -*- coding: utf-8 -*-
"""两融趋势策略: 融资余额连升 + 融资买入放量 + 股价未过热, 博弈杠杆资金持续进场

数据源:
  stock_margin_detail  沪深交易所两融明细(rzye 融资余额 / rzmre 融资买入额,
                       历史已回填 250 个交易日, 采集: --margin)
  stock_daily_quote    日线( pct_chg / amount / vol_ratio 预计算列 )
筛选维度(默认口径):
  杠杆趋势: 基准日前 ramp_days 个交易日中, 融资余额上升天数 >= min_rise_days
            (默认 5 日中至少 4 日升, 允许 1 日小回落), 且 5 日累计升幅 >= min_rzye_pct。
  买入强度: 基准日融资买入额 >= 前 ramp_days 日均值(融资买入放量;
            注: 不用"买入额/成交额占比"口径 —— 日线 amount 列 9/10-9/16 有采集缺口,
            占比分母不可靠, 改用买入额自身动量, 完全不依赖成交额历史)。
  价格纪律: 基准日涨跌幅在 [min_pct, max_pct](未大涨追高), 量比 <= max_vol_ratio
            (未放量滞涨; vol_ratio 为表内预计算列), 成交额 >= min_amount(流动性)。
  闸门:    上证指数 MA5>=MA20 才开仓(与 zt_pool_follow/fund_flow_diverge 同口径)。
评分: 0.6×融资余额5日升幅(封顶8%) + 0.4×融资买入放量倍数(封顶2倍, ×2 缩放),
      用于同批命中内排序, 跨日无可比性。
注意: 两融数据 T+1 披露, 基准日取两融表 MAX(trade_date), 行情因子同日对齐;
      两融余额上升但买入不放量 = 余额惯性(含还券下降等), 不算真趋势。
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import BaseStrategy, PickItem, register


class MarginTrendStrategy(BaseStrategy):
    code = "MARGIN_TREND"
    name = "两融趋势"
    default_params = {
        "ramp_days": 5,            # 融资余额观察窗口(交易日)
        "min_rise_days": 4,        # 窗口内余额上升天数下限
        "min_rzye_pct": 2.0,       # 窗口累计升幅下限%
        "min_buy_ratio": 1.0,      # 融资买入放量下限(当日/前窗口均值倍数)
        "min_pct": -3.0,           # 基准日涨跌幅下限%(价格纪律)
        "max_pct": 5.0,            # 基准日涨跌幅上限%(未大涨追高)
        "max_vol_ratio": 3.0,      # 量比上限(未放量滞涨)
        "min_amount": 200_000_000, # 基准日成交额下限(元, 流动性)
        "use_index_gate": True,    # 大盘环境闸门(上证MA5>=MA20才开仓)
        "top_n": 50,               # 入选上限(按评分截断)
    }

    def __init__(self):
        self.last_note = None

    @staticmethod
    def _index_env_ok(t0) -> tuple[bool | None, str]:
        """上证指数 MA5>=MA20? (与 zt_pool_follow/fund_flow_diverge 同口径)"""
        try:
            with engine.connect() as conn:
                row = conn.execute(text(
                    "SELECT ma5, ma20 FROM stock_index_daily "
                    "WHERE index_code='sh000001' AND trade_date<=:t0 "
                    "ORDER BY trade_date DESC LIMIT 1"), {"t0": t0}).fetchone()
        except Exception:
            return None, ""
        if not row or row[0] is None or row[1] is None:
            return None, ""
        ma5, ma20 = float(row[0]), float(row[1])
        ok = ma5 >= ma20
        return ok, f"上证MA5{'≥' if ok else '<'}MA20({ma5:.0f}/{ma20:.0f})"

    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        p = self.merged_params(params)
        self.last_note = None

        # ---- 基准日: 两融表最新日(数据 T+1 披露), 行情因子同日对齐 ----
        with engine.connect() as conn:
            t0 = conn.execute(text(
                "SELECT MAX(trade_date) FROM stock_margin_detail")).scalar()
            n_dates = conn.execute(text(
                "SELECT COUNT(DISTINCT trade_date) FROM stock_margin_detail")).scalar()
        if not t0:
            self.last_note = "两融明细无数据, 请先执行 run_collect.py --margin"
            return []
        need = int(p["ramp_days"]) + 5          # 窗口+前4日均值的冗余深度
        if (n_dates or 0) < need:
            self.last_note = f"两融历史仅{n_dates}日, 不足{need}日(先 --margin 回填)"
            return []

        # ---- 两融序列(仅取窗口深度, ~4万行) ----
        with engine.connect() as conn:
            dates = [r[0] for r in conn.execute(text(
                "SELECT DISTINCT trade_date FROM stock_margin_detail "
                "ORDER BY trade_date DESC LIMIT :nd"), {"nd": need}).fetchall()]
        if not dates:
            self.last_note = "两融明细无日期数据"
            return []
        t_start = min(dates)
        md = pd.read_sql(text("""
            SELECT ts_code, trade_date, rzye, rzmre
            FROM stock_margin_detail
            WHERE trade_date >= :ts
        """), engine, params={"ts": t_start})
        if md.empty:
            self.last_note = f"{t0} 无两融数据"
            return []

        # ---- 行情同日截面(来自 universe 缓存, 不再查库; vol_ratio 为表内预计算列) ----
        q_t0 = quotes[quotes["trade_date"] == t0][
            ["ts_code", "close", "pct_chg", "amount", "vol_ratio"]]

        name_map = dict(zip(basics["ts_code"], basics["name"])) if "name" in basics else {}

        md = md.sort_values(["ts_code", "trade_date"])
        cands = []
        for tc, g in md.groupby("ts_code"):
            g = g.tail(int(p["ramp_days"]) + 1)
            if len(g) < int(p["ramp_days"]) + 1 or g["rzye"].isna().any():
                continue
            rzye = g["rzye"].astype(float).tolist()
            rises = sum(1 for i in range(1, len(rzye)) if rzye[i] > rzye[i - 1])
            gain = (rzye[-1] / rzye[0] - 1) * 100
            if rises < int(p["min_rise_days"]) or gain < float(p["min_rzye_pct"]):
                continue

            # 融资买入放量: 当日 rzmre / 前 ramp_days 日均值
            # (不用"买入额/成交额占比"口径: 日线 amount 列 9/10-9/16 有采集缺口)
            rzmre = g["rzmre"].astype(float)
            if rzmre.iloc[:-1].isna().any() or rzmre.iloc[-1] is None:
                continue
            buy_avg_prev = rzmre.iloc[:-1].mean()
            if not buy_avg_prev:
                continue
            buy_mult = float(rzmre.iloc[-1]) / buy_avg_prev
            if buy_mult < float(p["min_buy_ratio"]):
                continue

            # ---- 基准日行情纪律 ----
            qr = q_t0[q_t0["ts_code"] == tc]
            if qr.empty:
                continue
            r = qr.iloc[0]
            if r["pct_chg"] is None or not (p["min_pct"] <= float(r["pct_chg"]) <= p["max_pct"]):
                continue
            if r["amount"] is None or float(r["amount"]) < float(p["min_amount"]):
                continue
            name = name_map.get(tc, "")
            if "ST" in name:
                continue
            # 量比: 表内预计算列(当日量/前5日均量, 全站口径)
            if r["vol_ratio"] is None:
                continue
            vol_ratio = float(r["vol_ratio"])
            if vol_ratio > float(p["max_vol_ratio"]):
                continue

            gain_score = min(gain, 8.0)                       # 封顶8%
            mult_score = min(buy_mult, 2.0)                   # 封顶2倍
            score = round(0.6 * gain_score + 0.4 * mult_score * 2, 3)
            cands.append({
                "ts_code": tc, "name": name, "close": r["close"],
                "pct_chg": float(r["pct_chg"]), "gain": gain, "rises": rises,
                "buy_mult": buy_mult,
                "vol_ratio": vol_ratio, "score": score,
            })

        # ---- 大盘闸门 ----
        env_desc = "闸门关闭"
        if p["use_index_gate"]:
            env_ok, env_desc = self._index_env_ok(t0)
            if env_ok is False:
                self.last_note = f"大盘弱势不开新仓: {env_desc}(上证死叉)"
                return []
            if env_ok is None:
                env_desc = "指数数据缺失(未阻塞)"

        cands.sort(key=lambda x: x["score"], reverse=True)
        items: list[PickItem] = []
        for c in cands[: int(p["top_n"])]:
            items.append(PickItem(
                ts_code=c["ts_code"], name=c["name"], trade_date=t0,
                close_price=float(c["close"]) if c["close"] is not None else None,
                pct_chg=c["pct_chg"],
                score=c["score"],
                hit_reason=(
                    f"融资余额{int(p['ramp_days'])}日+{c['gain']:.2f}%"
                    f"(连升{c['rises']}日), 融资买入{c['buy_mult']:.2f}倍于前均, "
                    f"价{c['pct_chg']:+.2f}%, 量比{c['vol_ratio']:.1f}, {env_desc}"
                ),
            ))

        self.last_note = (f"基准日{t0}, 两融余额趋势合格{len(cands)}只→取前{len(items)}只"
                          f"({env_desc})")
        return items


register(MarginTrendStrategy())
