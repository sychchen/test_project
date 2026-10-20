# 选股系统 项目长期约定

## 数据源可用性（2026-09-08 实测，本网络环境）
- 可用：新浪 getHQNodeData（股票列表+名称；**估值 per 字段为静态口径已弃用**——平安银行 5.69 vs TTM 5.26 偏差8%）/ getHQNodes（板块标签，概念体系陈旧）/ getKLineData（个股+指数K线；**分钟线 datalen 上限 1023 根≈21 个交易日，更早历史无法回溯**）/ F10 vCI_CorpInfo（上市日期，个别股票该字段为空）；腾讯 qt.gtimg.cn 88字段快照（**f39 PE-TTM/f46 PB/f45 总市值亿/f44 流通市值亿，60只/批，估值四字段主源**）；**Baostock query_history_k_data_plus（历史 peTTM/pbMRQ/amount 日频回填 + frequency=5 分钟线区间回填，免费免 token，与腾讯口径偏差<1%，支持多进程各自 login 并发；⚠️服务端会无预警断连 WinError 10053 且**静默返回空结果不抛异常**——长跑任务必须用重试重登录+连续空看门狗+跑完 NULL 复核，valuation_backfill/minute_backfill 已加固）**；东财 **push2delay** clist 延时快照（资金流 f62/f66/f72/f78/f84/f184、成交额 f6、上市日期 f26 兜底补缺、**概念板块列表 fs=m:90+t:3+f:!50 + 成分 fs=b:BKxxxx 含 PCB/CPO 等热点**，**单页上限100**）
- 不可用：东财 push2（断连）、push2his（并发即封IP）、新浪 MoneyFlow（服务下线）、腾讯 zjlx/ff_（控制器下线）、腾讯 fqkline 批量（反爬）
- 采集一律直连：`requests.Session(trust_env=False)`，8 线程 + thread-local Session（东财例外，需低并发）

## 库表与采集
- 表：stock_basic（含 industry/concepts/index_tags/list_date；PE/PB/市值=**最新快照**）、stock_daily_quote（amount 历史 NULL 已由 valbackfill 扩展 baostock amount 字段回填 + 2026-09-08 起前滚；**pe_ttm/pb/total_mv/circ_mv 日频估值快照**自 2026-09-08 前滚积累）、stock_index_daily（7指数）、stock_money_flow（前滚积累）、stock_minute_quote（**2026-09-09 起前滚积累，已取消"仅保留3日"自动清理**——否则每日增量会删掉 baostock 回填的历史分时）、stock_pick_batch/result
- 历史区间回填：`--quotes --start-date --end-date`（新浪日K区间模式，幂等）；`--minbackfill --start-date --end-date`（baostock 分钟线，minute_backfill.py，全市场约2-4小时，**重跑务必带 skip_filled=True 断点续跑**——会话重启会杀后台任务）；`--valbackfill`（baostock 日频 pe/pb/市值/amount 仅补NULL）；顺序须 quotes 先行，三者幂等可重跑
- `run_collect.py --all` 顺序：basic→listdate→tags→**emconcepts**→quotes→**valsnap**→index→moneyflow→minutes（valsnap/moneyflow 依赖 quotes 的最近交易日，顺序不可乱；valsnap=stock_basic 估值同源拷入当日日线行；**emconcepts 东财概念必须在 tags 后，否则被新浪概念覆盖**）
- 东财概念采集（em_concept_collector.py）：合并入 stock_basic.concepts（东财在前，上限40个）；**动态情绪板块（"昨日*"/"*热股*"/"*表现"）必须过滤**——会过期且挤占名额致热点概念被截断；旧 concepts 合并时同样过滤防残留
- 回测口径：backtest 用 `_overlay_valuation_asof(basics, t0)` 以日线表 t0 估值快照覆盖 basic 最新值（防未来函数），快照 NULL 回退最新值；实盘选股仍用 basic 最新快照（当日即最新）
- 新浪概念标签覆盖差（中际旭创仅"黄河三角"），板块匹配必须「概念 OR 申万行业」双通道取并集

## 策略框架
- 策略注册：BaseStrategy + STRATEGY_REGISTRY，新策略须在 pick_service.py import 触发注册
- 股票池：`filter_universe` 统一剔除科创板（688/689）；ST 由各策略按 name 剔除
- WEEKLY_MAINLINE：大盘环境用真实上证指数 MA5/MA20（回退市场宽度）；新股用真实 list_date≥60天；资金流 use_money_flow="auto"（积累满21交易日自动启用硬过滤）；策略内直读 stock_index_daily/stock_money_flow 必须按分析日 t0 截断防未来数据
- 小样本不调参（<30 只次调参=过拟合）；策略文档原文缺数据字段时优先采集入库，不接受长期代理
- 回测空窗两类**正确**根因（勿当 bug 修）：① 指数门死叉拦截（如 08-25~08-27）；② K 线充足性 min_hist_days=21（t0 窗内 <21 根→空池）；诊断工具 diag_env_gate.py / diag_stage_survivors.py，判定矩阵见 docs/07

## 服务与部署
- 8600 端口常驻 uvicorn（app.api.main:app）；StaticFiles 改 HTML 即生效，**策略/采集器/API 代码变更必须重启进程**
- 前端图表：ECharts 5.5.1 本地依赖（app/web/lib/echarts.min.js），统一引擎 buildChartOption（日K蜡烛+MA/分时折线+副图指标，tooltip十字光标/dataZoom缩放+滚动条）；旧 canvas 引擎已于 2026-09-09 删除
- 重启方式：PowerShell `Get-NetTCPConnection -LocalPort 8600 | Stop-Process` 后后台重启；Git Bash 的 taskkill //PID 转义有坑，用 PowerShell
- 每日盘后调度器（WorkBuddy 自动化 2cd30657）：工作日 15:30 随机化创建一次性实例（15:30~18:30）执行 --all + 7 策略选股；**实例执行完（含非交易日跳过/失败分支）自删**，调度器每日创建前兜底清理同名实例——排查"进行中"残留时先查这点

## 用户协作约定
- **日志沉淀位置（2026-10-20 用户指定）**：项目相关工作日志一律写入项目主路径 `stock-system\WORKLOG.md`（按日追加、勿改写历史段落），**不再写 .workbuddy/memory/YYYY-MM-DD.md**；.workbuddy/memory/ 仅保留本 MEMORY.md（长期约定）。原 7 份日志已合并迁入 WORKLOG.md（2026-09-08~10-20）
- 各端口径严格同步：SQL/md/HTML/README/内存 任一变更全部同步；文档落 docs/ 编号 md
- 结论须诚实：回测负收益等坏结果如实呈现并给基准对照；数据来源必须具体接口名/物理表名
- 汇报偏好：结构化表格 + checkmark 验证清单
- Epic 机制（2026-10-14 确认）：任务清单=stock-system\epic\epic_description.xlsx（五列：状态/任务类型/任务详情/日期/回答）；执行流程=read_epic.py 读 → 代码实证 → oneoff 脚本回答回填回答列 → update_epic_status.py 置「已完成」（行号+关键字双校验，ROWS 须同步为本次行）；用户报的 akshare\epic\... 路径不存在，以 stock-system\epic\epic_description.xlsx 为准；冗余的 akshare_read_epic.py / akshare_update_epic_status.py 两脚本已于 2026-10-14 删除，epic/ 仅存 read_epic.py / update_epic_status.py 工具对
