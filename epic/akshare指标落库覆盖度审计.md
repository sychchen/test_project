# akshare 指标 → stock-system 落库覆盖度审计

> 审计日期：2026-09-20
> 对标对象：`D:\kiro-worspace\test_project\akshare`（akshare 1.18.95 源码，`__all__` 中 A 股相关 stock_* 函数 **355 个** + index_* 指数辅助 27 个，分 36 个主题域）
> 被审对象：`D:\kiro-worspace\test_project\stock-system`（MySQL 20 张表，13 个采集器模块，daily_update 18 步编排）
>
> **结论先行：没有全部落库。** akshare 已接入并落库的主要是「涨停/强势股池、估值全历史、同花顺资金流快照、申万一级成分」4 组（经 `akshare_collector.py` vendored 调用），加上与 akshare 同源但走自研通道的日线/分钟/指数/资金流/大宗/龙虎榜。**36 个主题域中约 21 个完全无库表**，其中财务三表、两融、沪深港通、股东持仓、分红除权（复权因子）是对选股/回测影响最大的缺口。

---

## 一、已落库清单（akshare 能力 ↔ stock-system 表）

### 1.1 经 akshare 通道直接落库（4 组）

| akshare 函数 | 落库表 | 频率 | 备注 |
|---|---|---|---|
| stock_zt_pool_em / stock_zt_pool_strong_em | `stock_zt_pool` / `stock_zt_pool_strong` | 日频盘后 | 东财 push2ex，进 --all |
| stock_value_em | `stock_valuation` | 单股全历史回填 + 日度顶格 | 东财 RPT_VALUEANALYSIS_DET；日常靠 valtopup 保日鲜 |
| stock_fund_flow_individual（即时） | `stock_money_flow_ths` | 日频盘后 | 同花顺快照，~5200 只/12s，进 --all |
| sw_index_first_info + index_component_sw | `stock_sw_industry_member` | 慢变量周度 | 申万 31 个一级行业，不进 --all |

### 1.2 与 akshare 同数据域、走自研通道落库（等价覆盖）

| 数据域 | akshare 对应函数 | stock-system 表 | 实际数据源 |
|---|---|---|---|
| 日线行情 | stock_zh_a_hist / _daily | `stock_daily_quote` | 腾讯 fqkline 主 + 新浪 KLine 兜底 |
| 分钟线 | stock_zh_a_hist_min_em / _minute | `stock_minute_quote` | 新浪 CN_MarketData（腾讯分钟 DNS 不可达） |
| 指数日线 | stock_zh_index_daily / _tx | `stock_index_daily` | 新浪主 + 腾讯 fqkline 兜底（7 指数） |
| 股票列表/基础信息 | stock_info_a_code_name 等 | `stock_basic` | 新浪 HQNodeData + 腾讯 qt.gtimg.cn 估值快照 |
| 资金流（日度） | stock_individual_fund_flow 等 | `stock_money_flow` | 东财 push2delay clist 增量 + 新浪 MoneyFlow 历史回填 |
| 板块行情/成分 | stock_board_industry_* / stock_sector_* | `stock_board_basic` / `stock_board_member` / `stock_board_daily_quote` / `stock_board_minute_quote` | 新浪 newSinaHy + 本地流通市值加权合成 |
| 大宗交易 | stock_dzjy_* | `stock_block_trade` | 东财 datacenter RPT_DATA_BLOCKTRADE |
| 龙虎榜 | stock_lhb_* | `stock_billboard` | 东财 datacenter 3 张报表 + 席位聚合 |

---

## 二、未落库清单（akshare 有、stock-system 无表）

按对选股/回测的价值排序：

| # | 主题域 | akshare 代表函数 | 频率 | 缺口影响 |
|---|---|---|---|---|
| 1 | 财务三表 | stock_zcfz_em / stock_lrb_em / stock_xjll_em、by_report 系列 | 季报 | **基本面因子全缺**（营收/净利/ROE/毛利率），基本面选股无从谈起 |
| 2 | 财务指标 | stock_financial_analysis_indicator_em | 季报 | 同上，成长/盈利质量指标缺失 |
| 3 | 业绩报告 | stock_yjbb_em / stock_yjkb_em / stock_yjyg_em | 季度事件 | 业绩预告驱动策略不可用 |
| 4 | 分红配股 / 复权因子 | stock_fhps_em / stock_history_dividend 等 | 不定期 | **K 线为不复权口径，无复权因子表**，跨除权日收益率/回测口径有风险 |
| 5 | 融资融券 | stock_margin_sse / stock_margin_detail_szse 等 | 日频 | 杠杆资金情绪因子缺失 |
| 6 | 沪深港通 | stock_hsgt_hold_stock_em / stock_hsgt_hist_em 等 | 日频 | 北向持股因子缺失（注：2024-08 起实时披露口径已变，需先实测） |
| 7 | 股东/机构持仓 | stock_gdfx_* / stock_zh_a_gdhs / stock_report_fund_hold | 季度+事件 | 筹码集中度、基金重仓因子缺失 |
| 8 | 涨跌停池扩展 | stock_zt_pool_dtgc_em（跌停）/ _zbgc_em（炸板）/ _previous_em（昨涨停表现） | 日频 | 已有涨停池+强势股池，补 3 张同源表成本低 |
| 9 | 停复牌 / ST 历史 | stock_tfp_em / stock_zh_a_stop_em | 日频事件 | ST 仅靠 name 现值判断，**回测有幸存者口径问题** |
| 10 | 限售解禁 | stock_restricted_release_*_em | 事件 | 解禁抛压事件因子缺失 |
| 11 | 人气/热度 | stock_hot_rank_em 系列 | 实时/日频 | 热度因子缺失（同花顺热榜 100 只已用于板块 hot_score，个股级未落库） |
| 12 | 千股千评 | stock_comment_em 系列 | 日频 | 东财评分因子缺失 |
| 13 | 技术形态排行 | stock_rank_cxg_ths / _lxsz_ths 等 11 个 | 日频 | 连涨/创新高等榜单缺失（部分可由本地衍生替代） |
| 14 | 盈利预测/评级/研报 | stock_profit_forecast_em / stock_research_report_em | 事件 | 一致预期因子缺失 |
| 15 | 公告/新闻舆情 | stock_notice_report / stock_news_em | 事件/实时 | 事件驱动、舆情策略不可用 |
| 16 | 股权质押 | stock_gpzy_*_em | 日频/事件 | 风险排雷因子缺失 |
| 17 | 商誉 | stock_sy_em / stock_sy_profile_em | 季度 | 商誉减值排雷缺失 |
| 18 | 高管持股变动 | stock_ggcg_em / stock_share_hold_change_sse | 事件 | 内部人增减持信号缺失 |
| 19 | 新股/IPO | stock_xgsglb_em / stock_ipo_info 等 | 事件 | 打新/次新策略缺数据（现有日线不含上市前信息） |
| 20 | 筹码分布 | stock_cyq_em | 日频 | 获利盘比例因子缺失 |
| 21 | 机构调研 | stock_jgdy_tj_em / _detail_em | 事件 | 调研热度因子缺失 |
| 22 | 行业分级深化 | index_*_sw 二三级 / stock_board_concept_* | 慢变量 | 现仅申万一级 31 行业；概念板块仅标签无行情 |
| 23 | 指数成分权重 | index_stock_cons_weight_csindex | 慢变量 | 沪深300 等仅 index_tags 标签，无权重 |
| 24 | 北交所 | stock_bj_a_spot_em 等 | — | 采集显式剔除 bj |
| 25 | 市场统计/ESG/宏观 | stock_sse_summary / stock_esg_* / macro_* | 日/月/季 | 非选股核心，优先级低 |

---

## 三、接入建议（按性价比）

1. **第一梯队（高价值 + 源已验证稳定）**：① 跌停池/炸板池/昨涨停表现（与现有 ztpool 同端点 push2ex，改动最小）；② 财务三表与财务指标（东财 emweb/datacenter，同 darkpool 已验证的 datacenter 域）；③ 分红配股 + 复权因子（东财/新浪，直接修复回测口径风险）。
2. **第二梯队（高价值 + 需冒烟测试源）**：两融（交易所官网）、股东户数/十大股东（东财 datacenter）、业绩预告（东财）。
3. **第三梯队（锦上添花）**：人气榜、千股千评、技术形态排行、限售解禁、股权质押/商誉排雷。
4. **接入纪律**（沿用项目既有约定）：新要素必须入库前滚、禁运行时现拉；优先"顺带零请求"挂已有批量快照；每个要素至少双源或注明单源风险；写库前过一致性抽验（锚点股 600519.SH 交叉核对）；接入 --all 时注意写当日行的步骤排在 quotes 之后。

---

## 四、统计

- akshare A 股主题域：36 组 / 355 个 stock_* 函数（+27 个 index_*）
- stock-system 已覆盖（含自研等价）：**约 15 组**
- 完全未覆盖：**约 21 组**（见上表 25 行，含合并项）
- 已覆盖域多为「行情/资金/事件」类；未覆盖域集中于「基本面/持仓/公司行动」类——恰是策略从中短线情绪向基本面扩展所需的全部输入。


---

## 五、2026-09-23 复核：已执行落地情况

按第一/第二梯队建议，以下缺口已在本轮迭代中落地（见 epic/行情面板与选股策略方案.md）：

| 原缺口 | 落库表 | 状态 |
|---|---|---|
| #8 涨跌停池扩展（跌停/炸板/昨涨停） | stock_zt_pool_dt / _zbgc / _prev | ✅ 已落库，进 daily_update |
| #4 复权因子/除权事件 | stock_adjust_factor（41,606 行/3,981 只，池 100%）| ✅ 已回填 + 回测复权口径接入 |
| #1/#2 财务核心指标（摘要口径） | stock_fin_report（8 报告期 70,016 行）| ✅ 摘要级覆盖（EPS/ROE/毛利率/营收净利同比），**三表全量明细仍未落库** |
| #5 融资融券明细 | stock_margin_detail（250 日 87 万行）| ✅ 已落库 |
| #11 人气榜 | stock_hot_rank（快照逐日积累中）| ✅ 已落库（App Top100 无公开历史源，只能前滚） |

## 六、落库计划（待确认后执行）

> 按性价比排序：请求成本（首轮回填 + 日增）、存储、策略价值、源稳定性。

### 第一梯队（建议先做）

| # | 要素 | akshare/源 | 请求成本 | 落库表 | 策略价值 |
|---|---|---|---|---|---|
| P1 | 停复牌/ST 历史 | stock_tfp_em（东财 datacenter） | 首轮 ~250 请求(日频分页)+日增 1 | stock_trade_suspend | **修复回测幸存者偏差**（现 ST 仅按 name 现值剔除） |
| P2 | 业绩预告/快报 | stock_yjyg_em / stock_yjkb_em（datacenter, 同 finreport 翻页模式） | 每季 ~23 页/张 | stock_fin_forecast | 业绩预告驱动事件策略（暑期/年报季强） |
| P3 | 沪深港通持股 | stock_hsgt_hold_stock_em | 日增 1~2 请求 | stock_hsgt_hold | 北向持股因子（2024-08 披露口径变化，需先实测可用字段） |

### 第二梯队（筹码与排雷）

| # | 要素 | 源 | 成本 | 落库表 | 策略价值 |
|---|---|---|---|---|---|
| P4 | 股东户数/十大股东/基金重仓 | stock_gdfx_* / stock_zh_a_gdhs（datacenter 季度） | 每季 ~40 页 | stock_holder_num / stock_top_holder | 筹码集中度因子 |
| P5 | 限售解禁 | stock_restricted_release_em | 事件表, 首轮 ~10 请求 | stock_restricted_release | 解禁抛压排雷 |
| P6 | 股权质押+商誉 | stock_gpzy_* / stock_sy_em | 周/季度快照 | stock_pledge / stock_goodwill | 风险排雷 |

### 第三梯队（低成本顺带）

| # | 要素 | 源 | 成本 | 落库表 |
|---|---|---|---|---|
| P7 | 筹码分布(获利盘) | stock_cyq_em | 日增 1 | stock_cyq_ema |
| P8 | 千股千评 | 东财快照 | 日增 1 | stock_comment_em |
| P9 | 技术形态榜单(连涨/创新高) | THS 榜单 | 日增 ~11 请求 | stock_rank_ths |

### 暂缓（性价比不足或链路未定）

- 三表全量明细（fin_report 摘要已覆盖当前全部基本面因子；全量 3 表 × 5,200 股 × 季度 = 存储与采集成本高，待有明确因子需求再上）
- 公告/新闻舆情、盈利预测/研报（非结构化，事件驱动策略链路未定）
- 北交所（采集显式剔除）、宏观/ESG、行业二三级行情、指数成分权重（慢变量，低优先）

### 接入纪律（沿用既有约定）

新要素入库前滚、禁运行时现拉；优先「顺带零请求」挂已有批量快照；每要素双源或注明单源风险；锚点股 600519.SH 一致性抽验；进 --all 时排 quotes 之后。

## 七、2026-09-23 执行记录（用户确认"均按建议方案进行"后落地）

| 项 | 结果 | 说明 |
|---|---|---|
| P1 停复牌/ST 历史 | ✅ 已落库 | `stock_trade_suspend`，1,788 行/242 个交易日（2025-09-24~2026-09-23），区间展开口径；采集器 app/collector/suspend_collector.py，进 daily_update（suspend 步骤） |
| P2 业绩预告 | ✅ 已落库 | `stock_fin_forecast`，141,936 行/42 个报告期（2016Q1~2026Q2）/5,270 只；采集器 app/collector/forecast_collector.py，进 daily_update（forecast 步骤）；锚点 600187.SH/600519.SH 抽验一致 |
| P3 沪深港通持股 | ❌ 实测不可行，暂缓 | `RPT_MUTUAL_STOCK_NORTHSTA` 返回空/code 9701（2024-08 北向披露口径变化后停更）；待有替代源再评估 |
| P5/P6 | ✅ 已落地(2026-09-23) | 解禁 5,593 事件(2024-01~2027-03, 含未来日历 718 条)/质押 33,867 行 15 期周频/商誉 19,188 行 8 期；锚点 600370.SH 质押 78.74% 与探针一致；均挂 daily_update(restricted/pledge/goodwill 步骤) |
| P4/P8 | ✅ 已落地(2026-09-23 晚) | 股东户数 132,764 行/30 期(2019Q1~2026Q2)/5,103 只；千股千评 5,199 行 9/23 快照起日增；锚点 600519/000001 与探针一致；均挂 daily_update |
| P7/P9 | ⏳ 暂缓 | P7 筹码依赖被拦的 push2his 且逐股请求；P9 THS 榜单 vendored akshare 未内置该接口, 且连涨/创新高可由库内 cont_days/K线本地衍生 |

**P1 接口语径重要实证（写入采集器头注）**：`DATETIME='D'` 过滤等价于「SUSPEND_END>=D 的全部停牌区间记录」，并非 D 当日快照——akshare 的逐日快照用法会把未来停牌记到历史日（实测 DATETIME='2025-09-24' 返回 527 行，含 2026-04-30 起停的 37 只）。本系统改为「拉区间记录→本地按库内交易日展开」，单股行数=实际停牌交易日数（如 300585.SZ 自 2026-09-21 起仅 3 行）。

## 八、第二/三梯队执行记录（2026-09-23 晚，P5/P6 落地）

- **P5 限售解禁** stock_restricted_release：5,593 事件（2024-01-02~2027-03-22，其中未来解禁日历 718 条，直接服务解禁抛压排雷）。源 RPT_LIFT_STAGE 按日期窗口分页（500/页，全量 ~20 请求），uk(ts_code, free_date, shares_type)。口径：解禁数量/万股、解禁市值/万元、FREE_RATIO·TOTAL_RATIO 接口原值小数已 ×100 转百分比。增量：daily_update 每次拉滚动窗口（过去 90 天~未来 180 天，~2 请求）。
- **P6 个股质押比例** stock_pledge_ratio：33,867 行/15 期（中证登周频，月末快照回填 2025-01 起 + 接口最新期 2026-09-18）。源 RPT_CSDC_LIST（5 请求/期）。锚点 600370.SH 78.74% 与探针一致。增量：探测接口最新 TRADE_DATE 库内已有则空转（1 请求）。
- **P6 个股商誉** stock_goodwill：19,188 行/8 期（源仅年报期+最新中报期有数据，2019~2026）。源 RPT_GOODWILL_STOCKDETAILS（1-2 请求/期）。锚点 601318.SH 平安商誉 379.7 亿/占比 2.61%、000333.SZ 美的 315.8 亿/13.98%。口径：商誉/元、占净资产比例已统一为百分数。增量：缺报告期才拉。
- **P7 筹码分布暂缓**：stock_cyq_em 依赖 push2his（自 2026-09-23 12:09 被风控拦截 8h+）且需逐股请求（~5,221 只），成本-收益不匹配，待行情域解封后评估。
- 采集器：app/collector/risk_collector.py（sync_restricted_release / sync_pledge_ratio / sync_goodwill），已挂 daily_update（restricted/pledge/goodwill 步骤）。
- schema.sql 已追加三表 DDL；市场概况（RPT_CSDC_STATISTICS）裁剪不落库（个股数据足以排雷）。
- **P4 股东户数** stock_holder_num：132,764 行/30 报告期（2019Q1~2026Q2）/5,103 只（132.8k 行, uk(ts_code,end_date)）。源 RPT_HOLDERNUM_DET（~11 请求/期, 500/页）。锚点 600519.SH 2026Q2 股东户数 29.64 万户环比 +21.90%。增量：1 请求探测接口最新 END_DATE（非季末披露日自动回退最近已过季末），库内该期>=3000 行则空转；季中回退上一季末。
- **P8 千股千评** stock_comment：5,199 行（2026-09-23 快照起）。源 RPT_DMSK_TS_STOCKNEW（~11 请求/日, 日频快照历史错过无法回填）。锚点 000001.SZ close 11.60/机构参与度 41.89% 与探针一致（原值小数已 ×100 转百分数）。增量：1 请求探测 TRADE_DATE 已有则空转, 否则全量拉。
- **P9 THS 榜单暂缓**：vendored akshare 未内置 stock_rank_cxg_ths/_lxsz_ths 等接口；且连涨天数(cont_days)已库内、创新高可本地 K 线衍生, 性价比不足。
- 采集器：app/collector/feature_collector.py（sync_holder_num / sync_comment），已挂 daily_update（holder_num/comment 步骤, 插在 goodwill 之后）。
- **P1 数据接入回测（幸存者偏差真正闭环, 2026-09-23 晚）**：backtest.run_backtest 原逻辑将「t0 买入后至 t_end 停牌」的股票因 t_end 无行情行而静默剔除（统计只含正常交易股票）。现改为：以停牌前最后可得复权收盘计浮盈, details 增加 suspended(期间涉停牌)/suspended_now(t_end 仍停牌) 字段, stats 增加 suspended_hits/suspended_now 计数, 回测页 stats 行显示「涉停牌」、明细行标注「停牌中/曾停牌」。定向验证(假策略恒选样本)：300585.SZ t0=9/16 买入 15.90, 9/21 起停牌, 以 9/18 收盘 16.04 计 +0.88% 不再被剔除；t0 当天即停牌的股票(如 000016.SZ, 行情 9/14 后即断)仍被剔除——属正确行为(t0 无法成交)。注意：停复牌表起始日口径可能晚于行情断行日(000016 行情断于 9/15, 表记 9/17), 行情断行才是买卖可行性的真值。


## 九、收尾：P3 归档关闭 + P7/P9 本地衍生落地（2026-09-24 16:20-16:55）

经用户确认（三项均按建议方案），暂缓项全部收口：

| 项 | 处置 | 说明 |
|---|---|---|
| P3 沪深港通持股 | **归档关闭** | RPT_MUTUAL_STOCK_NORTHSTA 为监管停发（2024-08 北向披露口径变化），非风控问题；同源无替代，港交所 CCASS 需自写爬虫且口径不同，成本-收益不匹配。不再挂起。 |
| P7 筹码分布 | **本地衍生落地** | 东财 stock_cyq_em 依赖被拦的 push2his 且逐股 5,221 请求。改换手率衰减法自算（换手率字段 baostock 已补齐 250 个交易日）。 |
| P9 技术形态榜单 | **本地衍生落地** | THS 榜单接口 vendored akshare 未内置；连涨/创新高由库内日线直接计算，零外部请求。 |

### P7 落地明细（app/collector/cyq_collector.py → stock_cyq）

- 算法：逐股换手率衰减——旧筹码按 (1-t) 衰减，新筹码权重 t 以三角分布摊在当日 [low, high]（峰值=近似均价），停牌日筹码不变；250 日预热窗口消除初始假设影响
- 指标：获利盘比例 profit_ratio、筹码加权平均成本 avg_cost、90% 成本上下界（5%/95% 分位）、90% 成本集中度 conc_90
- 落库：最近 30 个交易日 × 全市场，2026-09-24 实测 156,164 行 / 5,207 只 / 110s，越界 0 空值 0
- 抽样：600519 收盘 1237 < 平均成本 1423 → 获利盘 5.8%（高位套牢，合理）；获利盘 100% 均为近期新股（合理）
- 口径注意：东财 cyq 为分钟级成交，本表为日线近似，适合趋势与横截面相对比较，不与东财数值直接比对

### P9 落地明细（feature_collector.sync_rank_deriv → stock_rank_deriv）

- 四类榜单各 top200：NEW_HIGH_60 创 60 日新高 / NEW_LOW_60 创 60 日新低 / CONT_UP 连涨 / CONT_DOWN 连跌
- 2026-09-24 实测：新高 142、新低 259、连涨 1,079、连跌 4,004，落库 742 行 / 12s，零外部请求
- 抽样验证：新高股当日收盘=60 日最高收盘 ✓；连涨榜第 1 名 9 连涨 ✓

### 跑批接入

- daily_update.py 新增两步骤（valpctl 之后、pick 之前）：cyq 筹码分布(本地衍生)、rank_deriv 形态榜单(本地衍生)，均幂等可重跑
- 至此覆盖度审计 P1~P9 全部闭环：P1/P2/P4/P5/P6/P8 采集落地、P7/P9 本地衍生落地、P3 归档关闭
