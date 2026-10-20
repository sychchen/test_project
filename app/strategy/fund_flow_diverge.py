# -*- coding: utf-8 -*-
"""价资背离·两源确认策略: 股价未涨(甚至小跌)但主力资金大幅净流入, 博弈主力暗中吸筹

数据源:
  stock_money_flow      新浪资金流(主力净额/占比, 历史自 2025-09 起)
  stock_money_flow_ths  同花顺资金流(总净流入, 即时快照逐日累积, 采集: --thsmoney)
筛选维度(默认口径):
  背离前提: 当日涨跌幅在 [min_pct, max_pct] 区间 —— 价未大涨才有"背离"可言;
            涨超 max_pct 是价资同向(不稀缺), 跌超 min_pct 按利空出货处理。
  资金确认: ① 新浪主力净额 >= min_main_net 且 主力占比 >= min_main_pct
            ② 同花顺净额 > min_ths_net (第二源同向确认, 防单源口径噪音)
            require_both_sources=False 时 THS 缺数据不阻塞(降级单源)。
  质地:   换手率在 [min_turnover, max_turnover] (有活跃度但不过热/对倒)。
  闸门:   上证指数 MA5<MA20 弱势不开新仓(与 zt_pool_follow 同口径),
          指数缺数据时不阻塞、只在命中原因标注。
评分:  0.6×新浪主力占比 + 0.4×THS净额占成交额比, 用于同批命中内排序, 跨日无可比性。
注意:  THS 源自 2026-09-16 起逐日累积, 基准日取两源共同最新日(min of max),
        历史基准日跑批在 THS 积累满一个季度前不可用(新浪单源可补, 但默认双源口径)。
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import BaseStrategy, PickItem, register


class FundFlowDivergeStrategy(BaseStrategy):
    code = "FUND_FLOW_DIVERGE"
    name = "价资背离"
    default_params = {
        "min_pct": -7.0,              # 涨跌幅下限%(跌破按利空处理)
        "max_pct": 2.5,               # 涨跌幅上限%(价未大涨=背离前提)
        "min_main_net": 10_000_000,   # 新浪主力净额下限(元, 默认1000万)
        "min_main_pct": 3.0,          # 新浪主力净流入占比下限%
        "min_ths_net": 0,             # 同花顺净额下限(元, >0 即同向确认)
        "min_turnover": 1.0,          # 换手率下限%(有活跃度)
        "max_turnover": 15.0,         # 换手率上限%(防过热/对倒)
        "require_both_sources": True,  # 双源同向确认(THS 缺失时是否降级单源)
        "use_index_gate": True,       # 大盘环境闸门(上证MA5>=MA20才开仓)
        "top_n": 50,                  # 入选上限(按评分截断)
    }

    def __init__(self):
        self.last_note = None

    @staticmethod
    def _index_env_ok(t0) -> tuple[bool | None, str]:
        """上证指数 MA5>=MA20? (与 zt_pool_follow 同口径)"""
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

        # ---- 基准日: 两源资金流共同最新日(各取 MAX 后取小, 防单源缺采集导致空池) ----
        with engine.connect() as conn:
            t_sina = conn.execute(text(
                "SELECT MAX(trade_date) FROM stock_money_flow")).scalar()
            t_ths = conn.execute(text(
                "SELECT MAX(trade_date) FROM stock_money_flow_ths")).scalar()
        if not t_sina:
            self.last_note = "新浪资金流无数据, 请先采集资金流向"
            return []
        if p["require_both_sources"] and not t_ths:
            self.last_note = "同花顺资金流无数据, 请先执行 run_collect.py --thsmoney"
            return []
        t0 = min(t_sina, t_ths) if t_ths else t_sina

        # ---- 大盘闸门 ----
        env_desc = "闸门关闭"
        if p["use_index_gate"]:
            env_ok, env_desc = self._index_env_ok(t0)
            if env_ok is False:
                self.last_note = f"大盘弱势不开新仓: {env_desc}(上证死叉)"
                return []
            if env_ok is None:
                env_desc = "指数数据缺失(未阻塞)"

        # ---- 当日行情 + 两源资金(单日等值 JOIN, MySQL 5.5 无性能风险) ----
        df = pd.read_sql(text("""
            SELECT q.ts_code, b.name, q.close, q.pct_chg, q.turnover_rate,
                   q.amount, mf.main_net, mf.main_pct,
                   th.net_amt AS ths_net, th.amount AS ths_amount
            FROM stock_daily_quote q
            JOIN stock_money_flow mf
              ON mf.ts_code=q.ts_code AND mf.trade_date=q.trade_date
            LEFT JOIN stock_money_flow_ths th
              ON th.ts_code=q.ts_code AND th.trade_date=q.trade_date
            LEFT JOIN stock_basic b ON b.ts_code=q.ts_code
            WHERE q.trade_date=:t0
        """), engine, params={"t0": t0})
        if df.empty:
            self.last_note = f"{t0} 无资金流交叉数据"
            return []

        cands = []
        n_ths_missing = 0
        for r in df.itertuples():
            name = r.name or ""
            if "ST" in name:
                continue
            if r.pct_chg is None or not (p["min_pct"] <= float(r.pct_chg) <= p["max_pct"]):
                continue
            if r.main_net is None or float(r.main_net) < p["min_main_net"]:
                continue
            if r.main_pct is None or float(r.main_pct) < p["min_main_pct"]:
                continue
            tr = float(r.turnover_rate) if r.turnover_rate is not None else None
            if tr is None or not (p["min_turnover"] <= tr <= p["max_turnover"]):
                continue
            ths_net = float(r.ths_net) if r.ths_net is not None else None
            if p["require_both_sources"]:
                if ths_net is None:
                    n_ths_missing += 1
                    continue
                if ths_net <= p["min_ths_net"]:
                    continue
            # THS 净额占成交额比%(缺 amount 时用日线成交额兜底)
            ths_pct = None
            amt = r.ths_amount if r.ths_amount else r.amount
            if ths_net is not None and amt and float(amt) > 0:
                ths_pct = ths_net / float(amt) * 100
            score = round(0.6 * float(r.main_pct) + 0.4 * (ths_pct or 0.0), 3)
            cands.append({
                "ts_code": r.ts_code, "name": name, "close": r.close,
                "pct_chg": float(r.pct_chg), "turnover": tr,
                "main_net": float(r.main_net), "main_pct": float(r.main_pct),
                "ths_net": ths_net, "ths_pct": ths_pct, "score": score,
            })

        # ---- 评分排序 + top_n 截断 ----
        cands.sort(key=lambda x: x["score"], reverse=True)
        items: list[PickItem] = []
        for c in cands[: int(p["top_n"])]:
            ths_txt = (f"THS+{c['ths_net']/1e8:.2f}亿" if c["ths_net"] is not None
                       else "THS缺数据")
            items.append(PickItem(
                ts_code=c["ts_code"], name=c["name"], trade_date=t0,
                close_price=float(c["close"]) if c["close"] is not None else None,
                pct_chg=c["pct_chg"],
                score=c["score"],
                hit_reason=(
                    f"价{c['pct_chg']:+.2f}%而资进: 新浪主力+{c['main_net']/1e8:.2f}亿"
                    f"({c['main_pct']:.1f}%), {ths_txt}, 换手{c['turnover']:.1f}%, {env_desc}"
                ),
            ))

        miss_txt = f", 其中THS缺数据剔除{n_ths_missing}只" if n_ths_missing else ""
        self.last_note = (f"{env_desc}, 基准日{t0}两源交叉{len(df)}只→条件过滤{len(cands)}只"
                          f"{miss_txt}→取前{len(items)}只")
        return items


register(FundFlowDivergeStrategy())
