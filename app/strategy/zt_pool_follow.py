# -*- coding: utf-8 -*-
"""涨停复盘·连板接力策略: 昨日涨停池中筛「封板质量高 + 梯队地位强」的票, 博弈次日溢价

数据源: stock_zt_pool (东财涨停池, 采集: run_collect.py --ztpool)
筛选维度(默认口径):
  梯队:  连板数在 [min_boards, max_boards] 区间 —— 首板~3板为主,
         更高位接力风险大(炸板率随板数上升), 默认不进。
  质量:  ① 开板次数 <= max_break_cnt (封单坚决, 分歧小)
         ② 首次封板时间不晚于 lock_before (早封=主动资金, 尾盘偷袭板弱)
         ③ 封单额 >= min_lock_amt (有真实承接, 非一笔封板的虚板)
         ④ 换手率 <= max_turnover (防对倒出货/分歧过大)
  分散:  同行业最多 max_per_industry 只, 避免单行业扎堆(行业退潮团灭)。
  闸门:  上证指数 MA5<MA20 弱势不开新仓(与 weekly_mainline 同口径),
         指数缺数据时不阻塞、只在命中原因标注。
评分:  cont_boards*10 + 封单额(亿) - 开板次数*2 + 首封早于09:45加2分,
       用于同批命中内排序, 跨日无可比性。
"""
import pandas as pd
from sqlalchemy import text

from app.db import engine
from app.strategy.base import BaseStrategy, PickItem, register


class ZtPoolFollowStrategy(BaseStrategy):
    code = "ZT_POOL_FOLLOW"
    name = "涨停复盘"
    default_params = {
        "min_boards": 1,          # 最低连板数(1=首板也可入选)
        "max_boards": 3,          # 最高连板数(高位接力风险过滤)
        "max_break_cnt": 2,       # 开板次数上限(封板坚决度)
        "lock_before": "103000",  # 首次封板不晚于该时刻(HHMMSS)
        "min_lock_amt": 20_000_000,   # 封单额下限(元, 默认2000万)
        "max_turnover": 25.0,     # 换手率上限%(防对倒)
        "max_per_industry": 2,    # 同行业最多入选数
        "use_index_gate": True,   # 大盘环境闸门(上证MA5>=MA20才开仓)
    }

    def __init__(self):
        self.last_note = None

    @staticmethod
    def _index_env_ok(t0) -> tuple[bool | None, str]:
        """上证指数 MA5>=MA20? (与 weekly_mainline 同口径)"""
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

        # ---- 涨停池最新交易日 ----
        with engine.connect() as conn:
            t0 = conn.execute(text("SELECT MAX(trade_date) FROM stock_zt_pool")).scalar()
        if not t0:
            self.last_note = "涨停池无数据, 请先执行 run_collect.py --ztpool"
            return []

        # ---- 大盘闸门 ----
        env_desc = "闸门关闭"
        if p["use_index_gate"]:
            env_ok, env_desc = self._index_env_ok(t0)
            if env_ok is False:
                self.last_note = f"大盘弱势不开新仓: {env_desc}(上证死叉)"
                return []
            if env_ok is None:
                env_desc = "指数数据缺失(未阻塞)"

        # ---- 取当日涨停池, 按条件过滤 ----
        df = pd.read_sql(text("""
            SELECT ts_code, name, cont_boards, zt_stat, first_lock_time,
                   break_cnt, lock_amt, turnover_rate, circ_mv, industry, pct_chg
            FROM stock_zt_pool WHERE trade_date=:t0
        """), engine, params={"t0": t0})
        if df.empty:
            self.last_note = f"涨停池 {t0} 无数据"
            return []

        name_map = dict(zip(basics["ts_code"], basics["name"]))
        close_map = {}
        if not quotes.empty:
            last_q = quotes.sort_values("trade_date").groupby("ts_code").tail(1)
            close_map = dict(zip(last_q["ts_code"], last_q["close"]))

        cands = []
        for r in df.itertuples():
            name = name_map.get(r.ts_code, r.name or "")
            if "ST" in name:
                continue
            if not (p["min_boards"] <= int(r.cont_boards) <= p["max_boards"]):
                continue
            if r.break_cnt is not None and int(r.break_cnt) > p["max_break_cnt"]:
                continue
            flt = str(r.first_lock_time or "235959")
            if flt > p["lock_before"]:
                continue
            if r.lock_amt is None or float(r.lock_amt) < p["min_lock_amt"]:
                continue
            if r.turnover_rate is not None and float(r.turnover_rate) > p["max_turnover"]:
                continue
            boards = int(r.cont_boards)
            lock_yi = float(r.lock_amt) / 1e8
            early = 2.0 if flt <= "094500" else 0.0
            score = round(boards * 10 + lock_yi - int(r.break_cnt or 0) * 2 + early, 3)
            cands.append({
                "ts_code": r.ts_code, "name": name, "boards": boards,
                "zt_stat": r.zt_stat, "flt": flt, "break_cnt": int(r.break_cnt or 0),
                "lock_amt": float(r.lock_amt), "turnover": r.turnover_rate,
                "industry": r.industry or "未分类", "pct_chg": r.pct_chg,
                "score": score,
            })

        # ---- 评分排序 + 行业分散 ----
        cands.sort(key=lambda x: x["score"], reverse=True)
        ind_cnt: dict[str, int] = {}
        items: list[PickItem] = []
        for c in cands:
            ind = c["industry"]
            if ind_cnt.get(ind, 0) >= p["max_per_industry"]:
                continue
            ind_cnt[ind] = ind_cnt.get(ind, 0) + 1
            items.append(PickItem(
                ts_code=c["ts_code"], name=c["name"], trade_date=t0,
                close_price=(float(close_map[c["ts_code"]])
                             if c["ts_code"] in close_map
                             and pd.notna(close_map[c["ts_code"]]) else None),
                pct_chg=(float(c["pct_chg"]) if c["pct_chg"] is not None else None),
                score=c["score"],
                hit_reason=(
                    f"{c['boards']}连板({c['zt_stat']}), 首封{c['flt'][:2]}:{c['flt'][2:4]}, "
                    f"开板{c['break_cnt']}次, 封单{c['lock_amt']/1e8:.2f}亿, "
                    f"换手{float(c['turnover']):.1f}%, 行业[{ind}], {env_desc}"
                ),
            ))

        self.last_note = (f"{env_desc}, 涨停池{len(df)}只→条件过滤{len(cands)}只"
                          f"→行业分散后{len(items)}只")
        return items


register(ZtPoolFollowStrategy())
