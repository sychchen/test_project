# 选股系统-调度机制验证（临时）执行记录

## 2026-09-08 12:19 一次性验证
- 通过 automation_update(list) 确认「选股系统-每日盘后随机调度器」(id: 2cd30657-f49f-498f-bc4f-46b9d83aca86) 存在且 status=ACTIVE。
- 调度规则：工作日（周一至周五）15:30 触发（FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=30）。
- 未执行任何采集或选股操作，验证通过。本任务按约定将由创建者删除。

## 2026-09-08 22:35 续跑（继续上轮收尾）
- 上轮遗留三件事全部完成：① 两周复盘一次性自动化已建（id 1fd0e953，2026-09-23 19:00）；② `run_collect.py --all` 全链路 exit 0（quotes/index/moneyflow/minutes 全阶段 0 失败，唯 listdate 剩 301286.SZ 一只 parse_miss）；③ agent-browser 核验回测页双口径与采集页 3 新按钮渲染正常，截图 docs/verify/backtest-dual-20260908.png、collect-page-20260908.png。
- 结果：系统可无人值守运行；下一观察点 2026-09-23 复盘自动化、2026-10-15 前后资金流 auto 硬过滤激活。

## 2026-09-08 12:19 续跑（回测空窗根因诊断 + 估值回填收尾）
- 完成 WEEKLY_MAINLINE 10/15 日回测空窗诊断：10 日窗（t0=08-25）指数门死叉正确拦截；15 日窗（t0=08-18）日线库深度（全市场自 2026-07-28 起）不足，K 线 16 根 < min_hist_days=21 全员拦截。两空窗均为正确行为，无 bug，与估值覆盖无因果（逐级存活统计实证）。
- 产出 docs/07-回测窗口可测性诊断.md；docs/04 多窗口表加旧口径注记；回测 panel-tip 更新；diag_env_gate.py / diag_stage_survivors.py 留存复现。
- 估值回填收尾四步全部完成。待决策项：>10 日深窗回测需 range 日线回补+Baostock 估值回填（约 1.5h），默认不执行。
