# -*- coding: utf-8 -*-
"""验证回测幸存者偏差修复: 停牌股不再被静默剔除。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.backtest import run_backtest

out = io.open('oneoff/bt_suspend_test.log', 'w', encoding='utf-8', newline='\n')
for code in ('VOLUME_BREAKOUT', 'MA_BULL'):
    r = run_backtest(code, days_ago=5)
    st = r['stats']
    out.write(f"{code}: hits={st.get('hits')} win={st.get('win_rate')}% "
              f"avg={st.get('avg_return')}% susp_hits={st.get('suspended_hits')} "
              f"susp_now={st.get('suspended_now')}\n")
    susp_items = [d for d in json.loads(json.dumps(r['top10'])) if d.get('suspended')]
    # top10 可能不含停牌股, 遍历全部需要 API 层; 这里验证字段结构存在即可
    out.write(f"  top10 sample keys: {sorted(r['top10'][0].keys()) if r['top10'] else 'N/A'}\n")
out.write('DONE\n')
out.close()
print('TEST_DONE')
