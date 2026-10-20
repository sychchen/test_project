# 每日盘后任务自动化 执行历史

## 2026-09-09（首次执行，成功）
- 执行前 MAX(trade_date)=2026-09-08，执行后=2026-09-09 → 有新股，正常走选股流程。
- 采集（run_collect.py --all，11m28s，全绿）：
  - basic 同步 5216 只（腾讯估值覆盖 5216/5216）
  - listdate 补缺 1 只
  - tags：行业 5216 覆盖，新浪概念 214 节点，指数 5 个
  - emconcepts：493 板块（已过滤动态情绪板块），覆盖 5214/5216
  - quotes 日K增量 rows=10431（ok=5216 failed=0）
  - valsnap 估值快照 2026-09-09 rows=5206
  - index 指数日线 7 指数 rows=280
  - moneyflow 2026-09-09 rows=5216，amount 回填 5206
  - minutes 5分钟分时 rows=250368（ok=5216 failed=0）
- 选股（run_pick.py --all，1m23s）：all done: 7/7 ok
  - 均线多头 923 / 放量突破 89 / 低估值 324 / MACD金叉 184 / 缩量回调 405 / 连续阳线 100 / 一周主线低吸 3
  - 批次号前缀 P20260909-*
- 无 FAILED，无非零退出码。
