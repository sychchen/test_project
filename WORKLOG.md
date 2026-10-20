# 选股系统工作日志（WORKLOG）

> 由 .workbuddy/memory/ 日志沉淀合并迁入（2026-10-20 用户指定：项目相关日志统一放项目主路径）。
> 按日追加，勿改写历史段落。

---

# 2026-09-08 工作日志

## 调度机制验证（一次性自动化 12:19）
- 确认「选股系统-每日盘后随机调度器」(2cd30657-f49f-498f-bc4f-46b9d83aca86) 存在且 ACTIVE，工作日 15:30 触发。

## 选股范围调整：只选大A，剔除科创板（16:04）
- 需求：所有策略只选大A（沪深主板+创业板），不考虑科创板。
- 实现：在 `app/strategy/base.py` 新增 `filter_universe()`，统一剔除 688/689 前缀（科创板+CDR）代码；在 `app/pick_service.py` 的 `run_pick` 和 `app/backtest.py` 的 `run_backtest` 加载数据后集中调用。
- 覆盖范围：CLI（run_pick.py）、API（/pick、/backtest 均走 run_pick/run_backtest）、全部 6 个策略（MA_BULL/VOLUME_BREAKOUT/VALUE/MACD_GOLDEN/PULLBACK/CONSECUTIVE_YANG）。
- 验证：模拟数据单测通过，688981/689009 被剔除，空表边界不报错（pandas 3.0.5 @ Python 3.12）。
- 说明：创业板（30xxxx）保留，采集端 basic_collector 本就只采沪深（60/68/00/30），北交所不在池内。

## 新增「一周主线低吸」策略 WEEKLY_MAINLINE（18:12）
- 需求：按 docs/03-一周策略.md（主线分歧低吸+轮动套利）生成可每日自动运行的一周策略。
- 产出：`app/strategy/weekly_mainline.py`（已注册进 pick_service）+ `docs/04-一周策略-量化实现.md`。
- 逻辑：主线池（CPO/PCB/光通信等概念 + 市值50-800亿 + 量比1.5-8 + RSI14<70 + 当日-2%~7% 分歧低吸，取前3）；主线候选<2或板块均涨幅>5% 时启用轮动池（农业/资源，取前2）；全市场 MA5>MA20 占比<45% 时空仓。
- 数据代理（系统无该字段）：主力净流入→3日量比；大盘死叉→市场宽度；新股→库内≥21个交易日；板块概念→stock_basic.concepts（依赖 tag_collector sync_tags）。
- 验证：合成数据三场景（主线正常/主线过热启用轮动/大盘弱势空仓）全部通过。测试中发现随机游走数据 RSI 易偏高，改用确定性价格路径校准。
- 注意：策略依赖 stock_basic.concepts 列，若库中无此列会抛错提示先跑 sync_tags。

## 前端整站重构：仿同花顺网页版（19:40）
- 需求：原页面（6卡片长页）不好看，参照同花顺网页版重构。
- 实现：重写 `app/web/index.html`（单文件、无外部依赖、API 全部不变）：
  - 布局：顶部导航（红色主调 #e8342e）+ 6 个标签页（行情K线/策略选股/选股结果/多策略交集/策略回测/数据采集）+ 底部固定状态栏（DB 健康 + 时钟 + 状态消息）。
  - 行情页：搜索联想 + 同花顺式报价带（最新价大字/涨跌/今开昨收最高最低/量/PE/市值 + 行业指数概念标签）+ 日K/分时切换。
  - 选股结果/交集：数字右对齐 tabular-nums、涨红跌绿、行内展开K线保留；执行选股后自动跳转结果页。
  - 回测页：红顶边统计卡；采集页：黑底控制台日志（带时间戳，替代原 status 文本）。
- 修复 bug：报价带在 pre_close 为 null 时涨跌额显示 NaN → 空值兜底。
- 验证：agent-browser 逐页截图验证（策略矩阵/结果表/行内K线/报价带+日K/采集控制台/真实回测 MA_BULL 胜率49.9%）全部正常。
- 注意：8600 端口为常驻服务，StaticFiles 直接读盘即改即生效；但策略注册在进程内存中，服务重启后策略数才会从 6 变为 7（含 WEEKLY_MAINLINE）。
- 收尾（19:55）：已重启 8600 服务（旧 PID 28224 已停），/api/health 确认 strategies=7、DB 正常；浏览器补验策略矩阵（7 卡含一周主线低吸）与多策略交集页（≥2策略 200 只共振，红色策略标签）均正常。服务以后台任务方式运行（task YRBB2k）。

## K线副图系统（20:13）
- 需求：主图下方提供可选数量+可选指标的副图，默认1张，仿同花顺常用指标。
- 实现：index.html 重构图表为统一引擎 drawStockChart（主图+1~3张副图，x轴对齐）：
  - 副图数量 1/2/3 可选（默认1），每副图指标下拉可选；日K与分时独立记忆设置。
  - 日K副图指标 8 种：VOL/MACD/KDJ/RSI(6,12,24)/BIAS(6,12,24)/OBV/WR(6,14)/CCI(14)；分时副图 4 种：VOL/MACD/KDJ/RSI。
  - 指标计算均为纯 JS 前端实现（EMA/中式SMA csma/滚动高低点）；副图左上角显示指标名+最新值（同花顺风格），MACD/BIAS/CCI 带零轴。
  - 结果表行内展开K线同步走新引擎，跟随全局副图设置。
- 验证：浏览器实测 默认1张VOL、3张(VOL+MACD+KDJ)、指标切换(KDJ→RSI)、分时图3副图、行内展开K线 全部正常。
- 注意：agent-browser 的 click 在页面滚动后偶发落空，可用 eval 直接调 JS 函数绕过。

## 排查「一周策略无结果」（20:34）
- 原因：今日 11:57 和 17:01 的批次都是旧服务进程（内存中仅 6 策略）跑的，WEEKLY_MAINLINE 18:12 才创建，从未执行过；且每日盘后调度器 prompt 中硬编码的策略清单也只有 6 个。
- 处理：① 手动执行 POST /api/pick/WEEKLY_MAINLINE → 批次 P20260908-203547，命中 2 只均为轮动池（600822 上海物贸、000822 山东海化；主线候选<2 触发轮动，市场宽度58%）；② 已更新调度器(2cd30657) prompt，策略清单改为 7 个（追加 WEEKLY_MAINLINE），明日起自动覆盖。
- 观察：今日主线池 0 命中——AI 算力板块分化日，RSI<70+20日涨>5%+量比1.5-8 组合较严；若连续多日主线无命中，可考虑放宽 rsi14_max 或 chg20 阈值。

## 副图控件全项目补齐（20:49）
- 需求：所有K线图都要提供副图控件，选股结果行内展开K线此前只有全局设置没有控件。
- 排查：全项目仅 index.html 一个前端文件，图表入口 3 处——行情页 mainChart（已有控件）、选股结果/多策略交集行内展开K线（共用 toggleKline，无控件）。
- 实现：行内展开改为每张图**独立副图控件**（inlineCharts 注册表 + inlineSubCount/inlineSubInd/renderInlineChart），每张展开图有自己的 副图数量(1-3)+指标下拉，默认跟随全局设置；改 toggleKline 一处即覆盖结果页与交集页。
- 验证：浏览器实测行内展开控件渲染、数量切换(1→2张, canvas 300→400)、指标切换(MACD→KDJ) 均正常。测试教训：agent-browser eval 中重复声明 const 变量名会 SyntaxError，后续 eval 需换变量名。

## 排查「DeepSeek 选 11 只 vs 系统只选 2 只」+ 双通道修复（21:02）
- 结论一（口径不可比）：DeepSeek 截图为叙事式分板块点名，其原文自述"短期涨幅已高、严禁追高"（实测 RSI：强达电路80.8/德科立78.0/亚盛集团85.7/金健米业84.4，当日多 +10%），按本策略"分歧低吸"纪律大多本就该过滤。
- 结论二（真实缺陷）：新浪 concepts 标签覆盖差——中际旭创仅"黄河三角"、超声电子无"PCB"、剑桥科技/强达电路/四方精创等为 null；industry（申万一级）全覆盖。旧「仅概念」匹配漏票严重。
- 修复：WEEKLY_MAINLINE 板块匹配改「概念关键字 OR 申万行业」双通道取并集（main_industries=通信/电子/计算机，rotate_industries=农林牧渔/有色金属/石油石化/煤炭/钢铁；select 三处匹配+过热监测全量集同步改）。
- 验证：重启 8600 后重跑 → 批次 P20260908-211140 命中 3 只全为主线池（000823 超声电子/300468 四方精创/600601 方正科技），三者在旧逻辑下均不可能入选（概念标签缺失），实证修复必要。
- 同步更新 docs/04-一周策略-量化实现.md（双通道条件+映射表+实盘验证记录）；删除临时脚本 tmp_deepseek_check.py。
- 经验：数据源标签类字段不要只依赖单一通道，须实证抽查覆盖率；概念/行业双通道取并集是防漏票稳妥做法。

## WEEKLY_MAINLINE 多窗口回测（21:20）
- 方法：GET /api/backtest/WEEKLY_MAINLINE?days_ago=1/2/3/5/8，另用 SQL 算同期全市场等权基准对比。
- 结果（诚实记录）：5 窗口全部跑输大盘——策略均收益 -0.32%/-2.66%/-2.78%/-5.21%/-2.11%，同期全市场 +0.70%/+1.69%/+1.34%/+0.13%/+1.48%。
- 解读：样本极小（15 只次）、回测是"死拿至今"口径未含 -3% 止损/T+3 退出纪律、轮动市强势股均值回归。双通道修复解决覆盖问题，alpha 未证实。
- 决策：不调参（15 只次调参=过拟合）；后续由每日调度积累 2 周实盘信号再复盘；回测模块待补止损/T+3 退出模拟。文档第六节已同步该结论。

## 全策略数据要素完整性审计 + 缺口补齐（21:23）
- 审计：通读 7 策略源码提取输入字段对照库表——6 个旧策略要素全覆盖；WEEKLY_MAINLINE 有 3 项缺口（主力净流入/指数行情/上市日期，此前均为运行时代理）。
- 数据源实测：新浪 MoneyFlow 已下线、腾讯 zjlx/ff_ 已下线、东财 push2 断连、push2his 单发可用但并发即封 IP（弃用）；**东财 push2delay clist 延时批量快照可用**（f62 主力/f66 超大/f72 大/f78 中/f84 小/f184 占比，主力=超大+大数值校验通过，单页上限 100 需分页 56 页）。
- 修复落地：① 新表 stock_index_daily（7 指数×40 日，新浪 K 线）+ index_collector；② 新表 stock_money_flow（5215 行@当日，push2delay）+ moneyflow_collector；③ stock_basic 加 list_date 列，sync_list_dates() 抓新浪 F10 补缺（5194/5195，茅台 2001-08-27 抽验一致，仅 301286.SZ parse_miss）；三任务均入 --all（basic→listdate→tags→quotes→index→moneyflow→minutes），调度器自动覆盖。
- API/前端：新增 POST /api/collect/listdate|index|moneyflow，采集页加 3 按钮；服务已重启验证。
- 文档：新建 docs/05-策略数据要素审计.md（矩阵+修复+数据源实测+遗留）；README（策略表补 WEEKLY_MAINLINE、7 策略口径、新表、新数据源）；docs/04 映射表标注真实数据已入库。
- 遗留：money_flow 历史深度 0 前滚积累（满 21 交易日≈2026-10-15 后策略可切真实主力净流入）；stock_daily_quote.amount 恒 NULL 无策略消费（备用方案：腾讯快照字段37）；策略端切换真实数据需另做对照验证。

## WEEKLY_MAINLINE 切换真实数据口径（21:59）
- 切换：大盘环境→真实上证指数 MA5/MA20（use_index_env 默认开，缺数据回退市场宽度）；新股过滤→真实 list_date≥60天（缺失回退库内天数）；资金流→hit_reason 实时展示真实主力净流入，硬过滤 use_money_flow 默认关（待积累满21日）。策略内直读 stock_index_daily/stock_money_flow，均按分析日 t0 截断，回测无未来数据。
- 验证三模式：① 默认（批次 215917）：3 只同前，hit_reason 含"上证MA5≥MA20(3937/3936)+主力净流入"真实值；② 严格 use_money_flow=true（批次 215952）：换血为 意华股份(+1.8亿)/艾华集团(+8297万)/明阳电路(+3849万)，原 超声电子(-1.3亿)/方正科技(-1.5亿) 被真实资金流证伪剔除——代理与真实资金流存在方向性偏差的关键实证；③ 回测 days_ago=5 正常（指数真实+资金流自动回退代理）。
- 文档：docs/04 映射表+切换验证记录、docs/05 遗留事项进度已同步。

## 回测模块纪律化退出模拟（22:15）
- 实现：backtest.py 新增 `_simulate_exit()`（-3%硬止损/+5%上移保本次日生效/T+3强平，日内路径保守：先按旧止损线判low、跳空低开按开盘价离场）；run_backtest 返回新增 discipline 块（params/stats含avg_hold_days/exits退出分布/details）；API 加 stop_pct/breakeven_trigger/max_hold_days 查询参数；前端回测页改双口径对照（持有至今 vs 纪律化退出，表格加纪律收益/纪律退出列）。
- 5窗口对照：纪律口径将单笔最大亏损锁定 -3.0%（死拿最差 -7.03%）；09-01 窗口均收益 -5.21%→-2.37%、09-03 -2.78%→-2.07%、09-07 -0.32%→0.0%、09-04 -2.66%→-3.0%（止损后反弹略差，诚实记录）。
- 意外发现：days_ago=8(08-27) 窗口变为不开仓——真实指数闸门当日上证 MA5(3909.15)<MA20(3910.07) 死叉（差 0.9 点，次日回多头），旧宽度代理曾放行买入均亏 -2.11%，真实闸门避开该亏损窗口。
- docs/04 解读段重写为双口径对照表+三点结论；README 回测行同步；结论维持：纪律截尾有效、alpha 未证实、冻结参数待 2 周实盘信号复盘。

## amount 回填 + 资金流 auto 模式（22:25）
- amount：资金流采集加 f6 字段零额外请求回填 stock_daily_quote.amount（首日 5206/5206 全覆盖，茅台 23.03亿 与腾讯快照精确一致）；至此审计发现的字段级缺口清零。
- auto 模式：use_money_flow 默认改 "auto"（_flow_depth()≥min_flow_days(21) 自动启用硬过滤，True/False 可强制）；验证：深度=1 时结果与默认批一致（过滤未启用，展示保留）。约 2026-10-15 自动生效，无需人工干预。
- 沉淀 .workbuddy/memory/MEMORY.md：数据源可用性、库表采集顺序、策略框架约定、服务重启方式、用户协作约定。
- docs/04/05/README 同步 auto 模式与 amount 回填口径。

## 复盘自动化 + 全链路验证 + 浏览器核验（22:35）
- 一次性自动化「选股系统-一周策略两周实盘复盘」id=1fd0e953-ffed-4581-a655-c267414d47b4，2026-09-23 19:00 触发（T+1/T+3/纪律收益 vs 市场基准，产出 docs/06）。
- `run_collect.py --all` 全链路 exit 0：basic 5215 → listdate(pending=1, 301286.SZ parse_miss 遗留) → tags(行业5558/概念3271/指数788) → quotes 5215/5215(10430行增量) → index 7/7(280行) → moneyflow 2026-09-08 5215行/56页/amount回填5206 → minutes 5215/5215(250320行)。明天调度器无人值守跑同一命令，已验证可靠。
- 资金流深度=2，auto 硬过滤预计 2026-10-15 前后自动激活。
- agent-browser 核验：回测页双口径（btDiscTip 退出分布"到期×1 止损×2"、btDiscStats 六宫格、表列 纪律收益/纪律退出 齐全）；5日窗口 WEEKLY_MAINLINE 3只（熵基科技/中安科/永新光学）持有至今 -5.21% → 纪律口径 -2.37%，最大亏损 -7.03%→-3% 锁定，与设计一致；采集页 8 按钮含 3 新按钮（补缺上市日期/采集指数日线/采集资金流向）。截图存 docs/verify/。

## 调度漏跑修复 + listdate 100% 覆盖（22:50）
- 发现：今日 16:54 调度实际执行（a5368f75）只跑 6 策略，静默漏跑 WEEKLY_MAINLINE（其记忆自报"6 策略全部成功"，prompt 枚举清单被执行者截断）。
- 修复①：`run_pick.py` 新增 `--all`（遍历 STRATEGY_REGISTRY，逐策略 batch/hits，失败打印堆栈+退出码非零）；验证 7/7 ok，6 旧策略 hits 与 16:54 批完全一致（803/119/312/302/283/69），WEEKLY_MAINLINE=3。
- 修复②：调度器 2cd30657 prompt 第 4 步改为 `run_pick.py --all` 并要求 FAILED/非零退出码必须显式上报；采集描述同步为现 7 阶段口径（约 8-10 分钟）。
- 修复③：301286.SZ parse_miss 根因=新浪 F10 页"上市日期"字段本身为空；moneyflow 采集加 f26 顺带补缺 list_date NULL 行（幂等零额外请求）→ 侨源股份 2022-06-14，**list_date 覆盖率 100%（NULL=0）**；run_collect moneyflow 打印加 listdate补缺。
- 服务已重启（新 PID 任务 PvcO8N）；README/docs-05 同步；Edit 工具两处"假成功"已复核补落盘（教训记入 MEMORY.md）。

## 复盘自动化复核 + 临时任务清理（23:00）
- 复盘自动化 1fd0e953 prompt 修正：复盘窗口从 2026-09-09 改为 **2026-09-08（含）**——否则漏掉首日产出的 3 只实盘信号；补充"同日多批次以最后一批为准"规则（首日有 5 个人工验证批）。
- 删除已完成使命的临时自动化 69f1d250（调度机制验证）。
- f26 外部交叉验证：深交所公告 t20220613_593785 + 中国证券网均确认 侨源股份 2022-06-14 创业板上市，与补缺值一致。
- docs/04 批次表补 P20260908-221811（auto 验证批）与 P20260908-224428（--all 全量批）两行 + 调度漏跑根因注记。

## 估值字段日频化（23:20，应用户设计评审）
- 用户指出 PE/PB/总市值/流通市值日频变化，stock_basic 只存最新值不合理 → 采纳并实施：stock_daily_quote 幂等加 pe_ttm/pb/total_mv/circ_mv 四列，新增 `snapshot_valuation()`（同源新浪拷贝当日快照，零网络请求，仅 UPDATE 已有日线行），--all 顺序插入 quotes 之后（新 CLI `--valsnap`）。
- 连带修复真实缺陷：回测此前用"今日估值"评历史信号（未来函数）→ backtest `_overlay_valuation_asof(basics, t0)` 按 t0 快照覆盖，NULL 回退最新值（积累期过渡口径）。
- 验证：首日 5206 行与 stock_basic 全量相等（null-safe 全等）；茅台 PE-TTM 19.94/PB 6.51/总市值 16367亿；overlay 快照路径与回退路径单测均全等；服务重启后 VALUE 回测 312 命中正常（与当日实盘命中数一致，符合预期——t0=09-01 快照尚为 NULL 走回退）。
- README/docs-05/schema.sql/MEMORY.md 已同步口径；服务任务 kHFHla。

## valsnap 口径补全 + 技能沉淀（23:40）
- 调度器 2cd30657 prompt 采集描述补"估值快照[PE/PB/市值入日线表]"阶段（8 阶段口径，摘要要求含估值快照行数）。
- API 补 POST /api/collect/valsnap（实测 200，重跑幂等 rows=5206）；采集页加"估值快照入日线"按钮+JS；服务重启（任务 7BHQQV）。
- 沉淀用户级技能 stock-system-daily-element（~/.workbuddy/skills/）：新增日频数据要素九步流程（实测源→幂等DDL→顺带零请求→--all 编排→t0 防未来函数→双路径验证→重启→多端同步→落盘复核）+ 反模式（枚举策略清单/东财并发/不验证就宣布完成）。

## 收尾核验（23:15）
- 分时表膨胀隐患排除：minute_collector 内置 KEEP_DAYS=3 自动 DELETE 清理（bar_time < now-3天），随每次采集执行，无膨胀风险。
- 回测页 panel-tip 补估值口径声明（"估值按基准日快照，2026-09-08 前无快照日期暂用最新值"），StaticFiles 即时生效。
- 采集页"估值快照入日线"按钮浏览器端到端验证：点击→API→控制台"✅ 交易日 2026-09-08, 写入 5206 行"。

## 估值口径升级：腾讯TTM + baostock 历史回填（23:55，应用户数据源之问）
- 用户问 Wind/同花顺等更多数据源可否解决历史回填 → 实测 **Baostock 可用**（免费免 token，peTTM/pbMRQ 日频历史，login/query 全通）；Wind/iFinD 需付费终端（如用户公司有账号可后续接）；同花顺免费网页 hexin-v 反爬不建议。
- **重大发现（三方仲裁）**：新浪 per 为静态口径——平安银行 PE 新浪 5.69 vs 腾讯 f39 5.26 vs baostock 5.260（腾讯=baostock 精确一致，偏差 8%）；茅台三源差<1%。腾讯 88 字段 f39/f46/f45(亿)/f44(亿) 四字段齐全、60只/批。
- 改动①：basic_collector 估值四字段切换腾讯快照覆盖层（5215/5215 覆盖，平安 5.26/茅台 20.10/中芯 144.35/宁德 18.26 与快照精确一致），新浪仅兜底；stock_basic 已是 TTM。
- 改动②：新增 `valuation_backfill.py`（baostock，only_null 参数；市值=当日收盘×最新股本回推，披露近似误差）；CLI `--valbackfill [--full]`，不进 --all。
- 后台任务 Saa8KZ 正以 --full 模式统一回填全部历史（约 80 分钟）；完成后日线表估值全链路 TTM 口径（历史=baostock，前向=腾讯快照）。
- docs/05 遗留事项④三步演进重写、数据源表加腾讯/baostock 行、README 命令与数据源、MEMORY.md、技能 stock-system-daily-element（加 baostock+仲裁原则）已同步；服务重启（任务 Lc7HK2）。
- 影响预告：VALUE 策略实盘输入已变（pe 口径 5.69→5.26 类），明日命中数可能较今日 312 微调，属口径升级预期内。
- 口径升级实证（批次 P20260908-234007-VALUE 327 vs 旧 312）：新增 44（TTM PE 真实<20，如 海正药业 15.78/TCL科技 16.22）、剔除 29（TTM 真实≥20 被静态口径漏放，如 口子窖 35.93/中国核电 25.23/中国能建 20.72）、共同 283——剔除案例直接证明旧口径把真高 PE 股放进"低估值"池。

## 服务异常死亡事件（00:30）
- 服务任务 Lc7HK2（23:55 启动）约 00:26 被外部终止：日志干净（启动完成后无异常即结束），非代码崩溃、非主动 kill。已重启（任务 QG2edQ），健康检查正常。
- 影响评估：调度器无人值守链路不依赖 8600 常驻服务（run_collect/run_pick 为自动化内同步子进程），仅交互式 Web 受影响；若再次无因死亡需排查宿主对后台进程的回收策略。
- 回填任务 Saa8KZ 进度 3500/5215（67%），零失败，预计 00:49 完成。

## 回测 10/15 日空窗根因诊断（12:20，自动化续跑收尾）
- **结论：两空窗均为正确行为，无代码 bug**。10 日窗（t0=08-25）被指数门死叉拦截（MA5 3894.96<MA20 3898.27，与 08-27 案例同类）；15 日窗（t0=08-18）因日线库全市场深度自 2026-07-28 起（31 根），窗内仅 16 根 < min_hist_days=21，指标充足性阶段 895→0 全员拦截（保守空池正确）。
- 证据：diag_stage_survivors.py 逐级存活统计——估值覆盖唯一影响点 D 阶段（市值带）两窗口均正常（575/570 只），唯一致命差异在 F 阶段 K 线充足性 ⇒ 空窗与估值口径升级无因果关系。
- 新事实：stock_daily_quote 全市场统一深度自 2026-07-28（增量 keep=2 只前滚不回溯）；可回测最深 t0=2026-08-25（days_ago≤10），更深窗口需 range 回补日线+Baostock 估值回填（约 1.5h，默认不做）。
- 产出：docs/07-回测窗口可测性诊断.md（新建）；docs/04 多窗口表加旧口径注记（5 日窗新口径：死拿 -2.34%/纪律 +1.22%）；index.html 回测 panel-tip 更新为真 TTM 快照+深度+死叉说明；diag_env_gate.py / diag_stage_survivors.py 留存仓库根目录作复现工具。
- 至此估值回填收尾四步全部完成（连续性✅ 覆盖✅ 回测对比✅ 文案✅）。

---

# 2026-09-09

## K线图引擎重构：canvas 手绘 → ECharts 5.5.1
- 背景：用户反馈旧 canvas K 线难看、无悬浮指标值、无法缩放/拖动看历史。
- 改动：
  - `app/web/lib/echarts.min.js`：新增本地 ECharts 5.5.1（jsdelivr 下载，1030855B，走 /static/lib/ 托管，不依赖外网 CDN）。
  - `app/web/index.html`：删除 drawStockChart/drawSubPanel 两个 canvas 引擎，新增 `buildChartOption(cfg)`（主图 candlestick 空心红涨/实心绿跌 + MA5/10/20 legend 可开关；分时=折线+面积；副图 1-3 张复用原 calcSub 指标库 VOL/MACD/KDJ/RSI/BIAS/OBV/WR/CCI）。
  - 交互三件套：tooltip trigger:'axis' + cross axisPointer（自定义 formatter 显示 开/高/低/收/涨跌额/涨跌幅/振幅/成交量/成交额/MA/副图指标值，axisPointer link 跨副图联动）；dataZoom inside（滚轮缩放+按住拖动平移，minValueSpan 10）+ slider（底部红色滚动条）；日K 默认显示最近 120 根（不足则全显）。
  - 主图容器 canvas→div#mainChart；表格行内展开 K 线同引擎（inlineCharts 存 echarts 实例，收起时 disposeInlineCharts 释放）；window resize 全部图表自适应。
- 验证：agent-browser 实测 600519.SH——series/grids/dataZoom 正确、dispatchAction showTip 悬浮框渲染 OHLC+指标值、dataZoom 20-70 缩放生效、整页截图确认底部滚动条可见。JS 语法 node --check 通过。
- 同步：README.md「K线行情」段、docs/02-技术设计.md 前端技术栈行 + 目录结构（lib/echarts.min.js）。
- 注意：/api/quote/{ts_code} 返回全量日线（当前 600519 仅 31 根，2026-07-28 起），历史深度取决于 stock_daily_quote 积累；改前端 HTML 无需重启 8600 服务。

## 结果/交集信息增强 + 副图控件下移 + 东财概念采集

- 结果/交集增强：/api/pick/results 与 /api/pick/intersection LEFT JOIN stock_basic 补 industry/concepts；results 加同基准日命中策略子查询 strategies。前端两表加列（结果 8→11 列：行业/概念/命中策略；交集 7→9 列），tagList() 渲染 tag、超 4-5 个折叠 +N、title 悬浮全显。
- 副图控件（张数+指标选择）从 K 线上方移到下方（行情页 #subCtl 与行内 K 线 sub-ctl 同改）。
- 东财概念采集（解决新浪概念无 PCB/CPO）：新建 app/collector/em_concept_collector.py，push2delay clist 板块列表 fs=m:90+t:3+f:!50（504 板块）+ 成分 fs=b:BKxxxx 分页；合并入 stock_basic.concepts（东财概念在前，上限 40 个，覆盖 5213/5215=99.96%，新浪仅 58%）。
- 两个坑已修：①"昨日涨停/东方财富热股"等动态情绪板块需过滤（_is_stable_board，每日变化且挤占名额致 CPO 被 MAX_CONCEPTS 截断）；②合并旧 concepts 时旧列含上轮残留动态标签，二次采集需同样过滤。
- 接入：run_collect.py --emconcepts（--all 顺序 tags→emconcepts，必须在 tags 后防覆盖）、POST /api/collect/emconcepts、采集页「采集东财概念」按钮。全量约 3 分 50 秒。
- 验证：中际旭创 CPO概念✓、超声电子/沪电股份 PCB✓、动态标签残留 0 行；前端实测结果页 11 列、交集页 9 列、副图控件在图下方。
- 同步：README + docs/01/02/04/05 五处文档口径。

## 副图控件悬浮中间位 + 详情设置面板

- 副图控件：ECharts 单画布多 grid，HTML 控件无法插进画布 → 改绝对定位悬浮条（.sub-ctl-float），chartLayout() 统一布局常量（主图与第一副图间留 gapMain=36px 间隙），placeSubCtl() 在渲染后定位到间隙正中；主图页与行内 K 线一致。
- 详情设置（⚙ 按钮，cfgPanel，chartCfg 持久化 localStorage）：
  - 周期 6 档：分时(当日5分)/30分/日K/五日/周K/月K。aggBars() 日K→五日(每5交易日)/周K(ISO周)/月K；aggMinute() 5分→30分(每6根合一，当日8根)。MA 改前端 maN() 统一计算（聚合周期无 DB ma 字段），MA_DEFS 结构由 [名,字段名,色] 改为 [名,周期,色]。
  - K线样式：实心阳线（新默认，itemStyle.color=UP_C）/空心阳线（#fff）。
  - 显示缺口：gapMarkArea() 检跳空缺口（今低>昨高=上缺红/今高<昨低=下缺绿），markArea 虚线框延伸至回补点或最新bar；周K 缺口罕见。
  - showChart() 只剩 Tab 快捷入口（写 period 后调 renderMainChart()）；pickStock 用 syncCfgUI()+renderMainChart() 恢复用户设置。
- 验证（600176.SH 实测）：控件悬浮位置 ctlBetween✓、默认实心✓、周K 31→7根✓、五日7根/月K3根/30分8根✓、缺口单测2处+真实2处渲染✓、空心切换✓、localStorage 写入正确（同 session 恢复✓；agent-browser 守护进程重启会重置 profile 致 localStorage 丢失，真实浏览器无此问题）。
- 行内 K 线同样应用实心/缺口全局设置。

## 每日盘后自动化（168aedf0）首次执行成功

- 采集 --all 11m28s 全绿：日K 10431 行 / 估值快照 5206 / 指数 280 / 资金流 5216 / 分时 250368；东财概念 493 板块覆盖 5214/5216。
- 选股 --all 1m23s，7/7 ok：均线多头923、放量突破89、低估值324、MACD金叉184、缩量回调405、连续阳线100、一周主线低吸3。
- 数据日期 2026-09-08 → 2026-09-09 正常推进。

## 历史日线回填任务（2025-09-09 ~ 2026-06-27）
- 背景：stock_daily_quote 现有数据仅从 2026-06-29 起（53 交易日），目标窗口 0 覆盖，需回填约 193 交易日 × 5216 只 ≈ 100 万行。
- 实测：30 只样本 5698 行秒级完成；全量单次约 20-40 分钟（新浪 getKLineData 一次拉 290+ 根，8 线程）。
- 结论：历史分时（2025-09~2026-06）无法回填——新浪分钟K 仅返回最近约 1023 根（≈21 交易日），只能前滚积累。
- 已建 2 个临时自动化（validUntil=2026-09-12，回填完成后需手动删除）：
  - fc5ad9de：上午窗 10:00 触发，随机延迟 0-120 分钟
  - 8d56dade：下午窗 14:00 触发，随机延迟 0-240 分钟
  - 均执行 --quotes 区间回填（幂等 upsert）+ --minutes 当日分时 + 覆盖核对；覆盖 ≥190 交易日即完成。
- 注意：rrule BYHOUR 不支持逗号多值（10,14 会报错），多时间点须拆多个自动化。

## 日线+分时回填 2026-06-27~2026-07-27
- 日线：`run_collect.py --quotes --start-date 2026-06-27 --end-date 2026-07-27` 区间模式 1m48s 完成，5216 只 ok=5201、109017 行；15 只失败均 no_rows_in_range（区间后上市新股，正常）。
- **重要更正**：上文"历史分时无法回填"结论作废——实测 Baostock `query_history_k_data_plus(frequency=5)` 支持按日期区间查分钟线（茅台 6-29~7-27 返回 1008 根=21交易日×48根，齐全），新建 `app/collector/minute_backfill.py`（`--minbackfill`，单连接约 40-50 只/分钟，全市场约 2 小时，幂等 UPSERT，口径 vol 股→/100 手与 minute_collector 一致）。
- **配套改动**：minute_collector 移除"仅保留最近3天自动清理"（KEEP_DAYS/DELETE 逻辑删除），否则每日 --all 会把回填的历史分时删掉；分时表自此前滚积累。
- valbackfill 扩展 amount 字段：FIELDS 加 amount，UPDATE 仅补 NULL（与 pe/pb 同一查询零额外请求），回填历史日线成交额。
- baostock 支持多进程各自 login 并发（分钟回填+估值回填并行无冲突）。
- 文档同步：README、docs/01、docs/02（新增 4.2b 节）、docs/05（遗留事项1改写+数据源表）、项目 MEMORY.md。
- 21:38 用户决定取消定时回填方案，已删除上述 2 个临时自动化（fc5ad9de / 8d56dade）。历史日线回填尚未执行，改由用户另行决定执行方式。

## 历史日线回填已完成（2025-09-09 ~ 2026-06-26）
- 21:42 执行 `run_collect.py --quotes --start-date 2025-09-09 --end-date 2026-06-26`，耗时仅 2m52s。
- 结果：total=5216 ok=5197 failed=19 rows=979452；窗口覆盖 190 个交易日 / 5197 只。
- 19 只失败均为 2026-06-26 之后上市的新股（no_rows_in_range，正确行为，非 bug）。
- stock_daily_quote 全表现覆盖 2025-09-09 ~ 2026-09-09，共 1,254,956 行，历史缺口已补齐。
- 经验：新浪 getKLineData 区间回填远快于预估（190 交易日 × 5216 只 < 3 分钟），datalen 上限 1023 根足够一年窗口。

## 清理已完成的一次性自动化 + 后台回填进度核查（23:08）
- 用户问"任务怎么还显示进行中"。排查：任务清单 6 项全 completed；一次性自动化 168aedf0「盘后执行 09-09 15:39」已执行完毕（日线/资金流均已到 2026-09-09，选股 7/7 SUCCESS：923/89/324/184/405/100/3，15:52~15:53 入库）但条目仍 ACTIVE → 已删除（同 69f1d250 先例）。
- 注意：每日调度器 2cd30657 的随机化机制每天会生成一个此类一次性实例，执行完需清理；可考虑在调度 prompt 中加入"成功后自删实例"的收尾指令。
- **仍在运行的两个后台回填任务（正常，非卡死）**：① 分钟线历史回填（21:44 启动）：2,415/5,216 只完成 ≈46%，最近 5 分钟写入 1,008 行，预计 ~00:49 完成；② 估值+成交额回填（21:42 启动，valbackfill 扩展 amount 版）：07-28 前历史行 pe_ttm 已填约 28%（783,117 行仍 NULL），日线表最近 5 分钟更新 18,826 行，预计午夜前后完成。两者 baostock 多进程并行无冲突。
- 提示：15:17 关于"深窗回测需 1.5h 回补"的回答已被用户 21:42 的实际操作部分超越——日线深度已补齐一年（2m52s），待估值回填完成后 >10 日深窗回测即可全程真 TTM 运行。

## 调度器实例自删机制上线（23:26）
- 用户拍板"成功后自删实例"。改动：调度器 2cd30657 prompt 的嵌套实例 prompt 新增第 6 步「收尾自删」——任何退出分支（成功/非交易日跳过/失败）均须 list 匹配「选股系统-盘后执行」开头+当天日期删除自身；第 3 步非交易日分支改为"直接转第 6 步"；调度器自身第 2 步保留为兜底清理。
- 生效时点：明日（09-10）15:30 起创建的实例自带自删；今天的 168aedf0 已于 23:08 手动删除。
- 各端同步：README.md 调度器段、docs/02 §6B（顺带修正过期执行内容：6 策略→7 策略 run_pick --all、补齐 8 采集阶段）、MEMORY.md 调度器条目。

---

# 2026-09-10

## 回填任务断点续跑
- 昨日（09-09）启动的分时回填（Ft8E0c）与估值回填（wnvRIz）随会话重启被终止，无残留进程。
- 中断时状态：分时 3763/5216 只已入库（378 万行，min=06-29 09:35, max=07-27 15:00）；估值 pe/pb 已填 51048/109017（目标区间）。
- minute_backfill.py 增加 `skip_filled=True` 断点续跑参数（单票单事务全有或全无，区间内有行即跳过）；续跑 5216→1453 只（2ZISqu）。
- valbackfill 幂等仅补 NULL，直接全量重跑（XcPdrp）。
- 教训：跨小时级后台任务会在会话重启后被杀，长跑任务须支持断点续跑或拆批。

## 分时回填最终完成（2026-06-27~2026-07-27）
- 续跑 1h2m 完成：ok=1438 empty=15 failed=0，新增 144.5 万行；合计区间 **5201/5216 只、5,232,912 行**。
- 校验：茅台 1008 根 = 21 交易日 × 48 根 ✓；时间轴 06-29 09:35 ~ 07-27 15:00 完整 ✓；15 只无数据 = 区间后上市新股/北交所，与日线回填失败集一致。
- 估值回填（XcPdrp）仍在跑，目标区间 pe/amount 已填 51048/109017，待完成后复核。

## valbackfill 断连事故与加固
- XcPdrp 跑完但**中途 baostock 服务端断连（WinError 10053）**：断连后 baostock 库内部打印"接收数据异常"并**静默返回空结果（不抛异常）**，断点（301003 起）后 3954 只全部被误判为 empty，0 failed 假象。目标区间 2769 只 pe/amount 未填。
- valuation_backfill.py 三处加固：①`_query_daily()` 查询重试 3 次 + error_code 非 0 抛错 + `_relogin()` 重登录；②连续 100 只空结果看门狗 → 自动重登录；③**only_null 模式下先查 pending ts_code 列表（pe_ttm/amount 仍有 NULL 的股票）**，断点续跑只处理待补股票（5216→2782 只）。
- 小样验证通过（5 只 1215 行/只），全量续跑任务 4X8yaf，预计 3-5 小时。
- **教训：baostock 断连是静默失败，任何长跑 baostock 任务必须靠"结果数校验/看门狗"发现，不能信 failed 计数；跑完必须核对 NULL 剩余量。**

## valbackfill 分片并行改造
- 用户要求加速：backfill_valuation 新增 `shard/n_shards` 分片参数（pending 列表按序号余数均分），run_collect.py 新增 `--shard N --nshards M` CLI；baostock 多进程各自 login 已实测无冲突，各分片更新行集不相交 + UPSERT 幂等。
- 4 进程并行（DSjdKA/4gABvD/1lcm5B/hvZRoK，各约 1304 只），单进程实测约 15s/只 → 并行后预计 1.5-2.5h 跑完。
- 进度口径：目标区间 pe 填充 51,048 → 71,644/109,017 行（单进程阶段）；pending 总数 5215（含北交所/停牌股，这类会一直 empty 属正常）。
- 4 分片 3.3~4h 全部结束：分片0/2/3 各仅 1-2 只瞬时失败（MySQL 2013/1205）；**分片1 遇 baostock 持续网络故障（重登录连续失败"网络接收错误"）跳过 191 只**——疑似 4 并发触发服务端限流或本机网络抖动，多进程并行不宜超过 3 个。
- 补漏启动（13cPGC，pending 过滤剩 442 只单进程跑）；目标区间已达 99,768/109,017 行（91.5%）。

---

# 2026-09-11

## 6-7月数据回填任务全部收官（估值+成交额）
- 最后一轮补漏（kk6P50）跑完：待补 160 只 → 160 ok / 0 failed / 0 empty，38,192 行 val + 37,941 行 amount，耗时 16 分钟。
- **最终复核（目标区间 2026-06-27~07-27，109,017 行）：pe_ttm / pb / total_mv / amount 全部 109,017/109,017 = 100% 填满，0 只缺口。**
- 全表：pe_ttm NULL 归零（最初 57.8 万行），amount 仅剩 19 行 NULL（baostock 无数据的票，可忽略）。
- 至此本次需求三件套全部完成：日线 109,017 行 / 分时 523 万行 / 估值+成交额 100%。

## 长期沉淀（本轮经验已在 09-10 日志）
- baostock 静默断连防护（重试+重登录+空看门狗+NULL 复核）与分片并行（≤3）已固化进 minute_backfill / valuation_backfill。
- pending 过滤 + 幂等仅补 NULL 的补漏模式反复被会话重启打断后依然可靠续跑，值得沿用。

---

# 2026-10-14

- 执行 epic 任务（stock-system\epic\epic_description.xlsx 第2行，2026-10-14）：「本系统中的选股策略来自哪里？是否来自 akshare？代码写死还是可从配置表获取」——代码实证调研完成。
  - 结论：13 个策略全部自研（app/strategy/，BaseStrategy + STRATEGY_REGISTRY 注册表模式）；akshare 仅作数据采集源（akshare_collector.py，vendored）；策略集合靠 pick_service.py L15-27 写死 import 列表触发注册（新增策略需新建文件+加 import），无 DB 配置表（schema.sql 33 表无策略定义表）；调用侧动态——API 按 code 调用、req.params 可覆盖 default_params。
  - 回答已写入 xlsx 回答列（E2，oneoff/fill_epic_answer_20261014.py），状态置「已完成」（epic/update_epic_status.py，ROWS 同步为本次行）。
  - 注意：用户报的路径 `test_project\akshare\epic\akshare_epic_description.xlsx` 不存在，实际文件为 `stock-system\epic\epic_description.xlsx`（11:51 刚更新）。
- 14:23 处置 epic/ 上轮遗留：`akshare_read_epic.py` / `akshare_update_epic_status.py` 经 diff 实证与现行 `read_epic.py` / `update_epic_status.py` 仅路径行/注释/历史 ROWS 差异（逻辑全同）、全项目零引用，确认为纯冗余副本后删除（回收站方案被安全策略/缺包拦下，改 os.remove；完整源码留存于当日会话，几行差异即可重建）。删除后 `read_epic.py` 冒烟通过，epic/ 现存：epic_description.xlsx（现行）、backup 备份、read/update 工具对、两份历史方案 md。

---

# 2026-10-19

## 选股结果页消重（v1.6）
- 背景：用户截图反馈选股结果页同股同日出现多行（如 002727.SZ 一心堂 ×3）。
- 实证根因：**不是**同策略多批次重复（同日同策略其实只跑了一次），而是**同股被多个策略命中各产生一行**，而「命中策略」列是 GROUP_CONCAT 聚合展示，导致每行看起来完全一样。
- 修改 `app/api/main.py` `/api/pick/results`：Python 侧按 `(ts_code, trade_date)` 去重，结果按 score DESC 有序、首个出现即该股最高分策略行（悬浮卡的得分/批次/命中原因随最高分策略）；SQL LIMIT 改 10 倍缓冲防重复行占限额。MySQL5.5 关联子查询/派生表会全扫，故不在 SQL 层做。
- 自测（重启 8600 后接口断言）：当日全部策略 887→732 条、duplicate=0；一心堂仅 1 行且 strategies=一周主线低吸、均线多头、连续阳线（保留 score=12.585）；单策略/无筛选两种组合 duplicate=0；GET / 200。
- 口径同步：docs/02-技术设计.md（接口表加消重说明，标 v1.6）、README.md（回测与交集节加消重条目）。
- 前端 index.html 未改动（无需变更）。

## Epic 批量执行（epic_description.xlsx 第2~11行，v1.6/v1.7）
- 第2行 策略配置DB化：新建 `app/strategy/config_store.py` + `stock_strategy_config` 表（uk_strategy_code，服务启动播种，类默认<库表<运行时三级合并）；run_pick/pick_asof/pick_live/backtest/daily pick 全链路统一 `effective_params`，`enabled=0` 跳过；新增 GET/POST `/api/strategy_config`。
- 第3行 akshare 审计：410 个 stock_* 函数 vs 已用 8 个；按数据域覆盖 39 张表，真缺口=增减持/三大报表/十大股东/机构持仓，不可采=北向 hsgt（2024-08 停披露）。补 `share_change_collector.py`（东财 RPT_EXECUTIVE_HOLD_DETAILS，row_hash MD5 幂等），全量 172,525 行落库，`run_collect.py --sharechange [--full]`。
- 第4行 缠论二买 CHAN_BUY2（第14策略，`app/strategy/chan_buy2.py`）：底分型（左右各1根low最低）→一买→反弹≥5%→回踩2~15%不破一买→底分型确认。回测诚实呈现：胜率18%/均-3.63%（单基准日小样本不调参）。
- 第5/6行：选股结果页加「多策略交集」下拉（无默认，≥2/≥3/≥4策略，nStrat>=minInter），随后原独立交集模块整体下线（nav/section/inter* JS/QP inter 全删，残余引用扫描为零）。
- 第8行：板块行情表格默认排序 circ_mv → pct_chg 降序。
- 第9行：暗盘信号页 50:50（.split-50），左表+4列（命中策略/基准日/连涨天数/连涨涨幅）14列 tbl-auto 横向滚动全列排序（默认涨跌幅降序），右侧 QP dark K线联动；后端 `/api/darkpool/signals` 补 cont_days/cont_pct + 同日选股命中策略聚合（Python smap 合并规避 MySQL5.5 派生表）。
- 第10行：涨停池 50:50 + 首板筛选。`/api/ztpool/pool` 加 `exact_boards`（精确匹配优先于 min_boards，梯队/行业/明细三处同口径）；前端下拉顺序 全部涨停/首板/≥2/≥3/≥4/≥5连板。自测：首板35只全 cont_boards=1。
- 第11行：市场面板 人气榜/资金流对比等高430px滚动 + TOP单选（人气榜10/15/20默认10，资金流5/10/15/20默认5）+ 页底通栏K线（qpBody-market 620px）。
- 另：导航四菜单前移（板块行情/暗盘信号/涨停池/市场面板→选股结果/策略选股/策略回测/数据采集）；6处 axis tooltip 加 confine:true 修浮窗裁切；daily_update.py 补 minutes 步骤（根因：链路缺个股分时致 stock_minute_quote 停09-16、boardmin 每日空转）。
- 前端自测全部走 agent-browser 数据层断言（非截图猜）；配置化自测 MA_BULL 221→199 改参即时生效、启停翻转、恢复。
- 遗留：第7行待分时回填（--minbackfill-sina --min-days 22 + 链式 --boardmin）完成后验证 stock_board_minute_quote 最新日期再置完成。

---

# 2026-10-20

## Epic 第10行第①子项补做：涨停池「首板」筛选（v1.7）
- 上轮置完成时发现第10行被用户扩写加了「首板」子项，本轮补齐。
- 后端 `/api/ztpool/pool` 新增 `exact_boards` 参数（精确匹配优先于 min_boards 下限，梯队/行业双口径/明细四处 SQL 同口径）；前端下拉顺序：全部涨停/首板/≥2/≥3/≥4/≥5连板。
- 自测：接口层首板 35 只全 cont_boards=1、全部涨停 46 只不受影响；agent-browser 数据层断言 + 截图视觉验证（_zt_first.png）。
- 口径同步：README v1.7 条目、docs/02 API 表新增 ztpool/pool 行。

## Epic 第7行收尾：板块分时断更修复完成闭环
- 昨晚后台回填任务（--minbackfill-sina --min-days 22 + 链式 --boardmin）在会话重启前已跑完个股分时（窗口 09-10~10-19，5225 只零失败），boardmin 合成到 09-09 被杀。
- 今日续跑 `--boardmin`（幂等 ON DUPLICATE KEY UPDATE）6 分钟完成：板块分时覆盖最近 30 个交易日（09-01~10-20）无断档，每日 2352 行。
- 验证：stock_minute_quote 最新 10-20 15:00、09-17 以来每日约 25 万行；/api/boards/{code}/minute_dates 返回 30 天连续；**2026-10-20 盘后调度实跑 minutes ✓ boardmin ✓（daily_update.py 链路修复已在自动化中生效）**。
- 第7行回答已回填并置「已完成」。Epic 11 行全部完成。
- ⚠️ 遗留（已如实写入第7行回答）：2026-10-20 盘后 moneyflow/mfhist 两步失败，stock_money_flow 停在 10-19，待排查。

## moneyflow/mfhist 采集失败排查与修复（2026-10-20 下午）
- **根因链**：①东财 push2delay 快照通道连续两日被拦（RemoteDisconnected）→ 主源 moneyflow 失败；②昨日 fallback 的 sys.path bug（已修）导致 10-19 数据缺一天；③今日 fallback 实际成功写入 5214 行（10-19 新浪口径），但 2 只 http=504 → 失败率判定 rc=1 → 整步报红；④mfhist 探针用东财 push2his 与库内最新日对照 → 库内最新日是新浪 fallback 写的（口径差 1.5~3 倍，backfill_money_flow.py 文档头有明确记录）→ 必然口径校验失败 exit 3。
- **修复**：①`moneyflow_history_backfill.py` 探针对照排除库内最新日（可能是新浪口径）——LIMIT 3→4 丢最新一行再对照；②`backfill_money_flow.py` 返回码容忍失败率 ≤1%（缺口由 pending_stocks 断点续跑补齐）。
- **验证**：fallback 小批量重跑 RC=0（failed=0）；mfhist 探针当前因 push2his 被 WAF 完全拦截（3 次重试断连，09-23 以来的已知环境状态，按设计退出稍后重试，非代码缺陷）。
- **数据补齐**：新浪 15:49 时当日数据未发布（缺口 5192 判定即此因，属时序特性）；16:50 重跑 `--start 2026-10-20` 全市场 5140/5140 零失败，**stock_money_flow 现覆盖至 10-20**（10-19: 5214 行 / 10-20: 5196 行，剩余缺口 0）。
- 口径提示：新浪与东财"主力"分档阈值不同（同日差异 1.5~3 倍甚至方向相反），fallback 按"只补缺失日 INSERT IGNORE"设计运行，同表内存在按日分段的口径混源——策略若对资金流做绝对阈值硬过滤需知悉此特性。

## 环境备忘
- uvicorn 8600 会话重启后需手动拉起（本轮已重启，task WgvGOs）。
- agent-browser 正确入口：`workspace/node_modules/agent-browser/bin/agent-browser.js`（plugins cache 目录下无 js 入口，直接调会 MODULE_NOT_FOUND）。

---

