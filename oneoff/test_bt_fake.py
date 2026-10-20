# -*- coding: utf-8 -*-
"""定向验证: t0 后停牌股被保留并以停牌前收盘计浮盈(而非静默剔除)。"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import app.backtest as bt
import app.pick_service  # noqa: F401  触发策略注册
from app.strategy.base import BaseStrategy, PickItem, STRATEGY_REGISTRY


class FakeStrategy(BaseStrategy):
    code = "FAKE_SUSPEND"
    name = "假策略-恒选停牌股"
    default_params = {}

    def select(self, quotes, basics, params=None):
        codes = ["300585.SZ", "601995.SH", "600519.SH"]
        # 300585: t0=9/16 有行, 9/21 起停牌至 t_end -> 应保留, suspended_now=True
        # 601995: 期间停牌(9/19-9/22), t_end 已复牌 -> suspended=True, suspended_now=False
        # 600519: 全程正常 -> 双 False
        rows = basics[basics["ts_code"].isin(codes)]
        return [PickItem(ts_code=r.ts_code, name=r.name, score=1,
                         hit_reason="fake", trade_date=None,
                         close_price=None, pct_chg=None)
                for r in rows.itertuples()]


STRATEGY_REGISTRY["FAKE_SUSPEND"] = FakeStrategy()

r = bt.run_backtest("FAKE_SUSPEND", days_ago=5)
out = io.open('oneoff/bt_fake_test.log', 'w', encoding='utf-8', newline='\n')
out.write(f"stats: {r['stats']}\n")
for d in r['top10']:
    out.write(f"{d['ts_code']} {d['name']} buy={d['buy_price']} now={d['now_price']} "
              f"ret={d['return_pct']}% suspended={d['suspended']} "
              f"suspended_now={d['suspended_now']}\n")
out.write('DONE\n')
out.close()
print('TEST_DONE')
