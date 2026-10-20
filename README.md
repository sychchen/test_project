# 选股系统

基于真实 A 股行情（腾讯 + 新浪财经数据源）的策略选股系统：数据采集 → MySQL 存储 → 策略选股 → Web 展示。

## 快速开始

```bat
:: 1) 建表（仅首次）
python run_collect.py --init-db

:: 2) 一键全量采集（基本面 → 上市日期补缺 → 板块标签 → 东财概念 → 日线增量 → 估值快照 → 指数日线 → 资金流向 → 当日分时 → 五池/业绩报表/复权因子）
python run_collect.py --all

:: 也可单项执行：
python run_collect.py --basic                                  :: 股票列表+基本面（5215 只）
python run_collect.py --listdate                               :: 补缺上市日期(新浪F10, 首次全量后续仅补新股)
python run_collect.py --tags                                   :: 行业/概念/指数 板块标签(新浪)
python run_collect.py --emconcepts                             :: 东财概念板块(PCB/CPO等热点, 合入concepts列)
python run_collect.py --quotes                                 :: 日线增量（默认最近 2 个交易日）
python run_collect.py --quotes --start-date 2026-08-03 --end-date 2026-08-07  :: 指定时间段采集
python run_collect.py --valsnap                                :: 估值快照入日线表(PE/PB/市值, 须在quotes后; 顺带算换手率)
python run_collect.py --indicators                             :: 重算日频衍生指标(连涨天数/连涨涨幅/量比/换手率, 须在quotes+valsnap后; 空=全表)
python run_collect.py --ztpool [--days 3]                      :: 涨停/强势/跌停/炸板/昨涨停 五池(东财 push2ex, akshare; 默认最新交易日)
python run_collect.py --finreport [--quarters N]               :: 业绩报表(东财按报告期批量页翻, ~23页/季6s; 默认最新报告期, 首次回填 --quarters 8)
python run_collect.py --adjseed                                :: 复权因子一次性回填(腾讯qfq vs 库内不复权, ~20-25分钟, 建议后台; 断点续跑幂等)
python run_collect.py --adjdaily                               :: 复权因子日常增量(零请求预警+候选逐股qfq验证; 须在quotes+thsmoney后, 进--all)
python run_collect.py --valuation [--val-days N] [--full]      :: 估值历史(东财, akshare; 默认增量仅补新行, 全历史8.7年;
                                                               ::   --full 首次全量建议后台+分片 --shard/--nshards; 不进 --all)
python run_collect.py --index [--days N]                       :: 主流指数日线（上证/深成/创业板等 7 指数）
python run_collect.py --valtopup                               :: 估值日度顶格(daily quote快照列→stock_valuation, 须在valsnap后, 零API成本)
python run_collect.py --valpctl                                 :: 估值分位重算落库stock_valuation_pctl(近5年窗口~3分钟, 供VALUE_PCTL读表)
python run_collect.py --thsmoney                                :: 同花顺个股资金流快照(全市场~12s, 盘后采, 与新浪资金流交叉验证, 进--all)
python run_collect.py --swmember                                :: 申万一级行业成分全量刷新(31行业/全A覆盖, 破新浪2563只上限; 慢变量建议周度)
                                                               ::   默认写最近 40 个交易日; --days 1030 可回填 ~4.2 年
                                                               ::   均线另多拉 25 根预热, 保证写入行 ma5/ma10/ma20 全有值
python run_collect.py --moneyflow                              :: 个股资金流向（东财延时快照, 按最近交易日记账; 仅当天）
python run_collect.py --minutes                                :: 当日 5 分钟分时（新浪增量, 默认回溯3天可自愈漏跑, 前滚积累）
python run_collect.py --minbackfill-sina --min-days 30         :: 历史分时回填(新浪, 覆盖最近30个交易日, 双域名轮询, 可反复重跑)
                                                               ::   --min-members-only 只补板块成分股(2563只, 板块分时只需这些)
                                                               ::   --min-host vip|money 限定单域名(与另一条回填并行用)
python run_collect.py --histbackfill-sina                      :: 日线历史向前扩展(新浪, 约4.2年/1030根; 腾讯 WAF 封禁时的替代通道)
                                                               ::   --hist-sina-count N 单票根数 / --hist-host 限定域名
python run_collect.py --board                                  :: 板块基础(新浪行业快照+成分映射+人气, 49 个申万行业)
python run_collect.py --boardsynth                             :: 板块日线合成(成分股流通市值加权, 基期1000, 全量覆盖)
python run_collect.py --boardmin                               :: 板块分时合成(最近30个有效分时日; --boardmin-one 只补最新一天)
python run_collect.py --darkpool --days 7                      :: 暗盘采集(东财大宗交易+龙虎榜+席位)
python run_collect.py --darkind                                :: 暗盘派生指标重算(最近10个交易日; --darkind-all 全历史重算)

:: 3) 资金流历史回填（新浪；INSERT IGNORE 只补缺口，不覆盖东财口径既有行）
::    两个域名限流独立且恢复时间不同 —— 冷却必须按域名设：money≈70s / vip≈200s
python oneoff/backfill_money_flow_loop.py --host money --cooldown 70  --start 2025-09-09
:: 若要再快一点，另开一片走 vip 域名（分片互斥、不重复抓）：
python oneoff/backfill_money_flow_loop.py --host vip --shard 1/2 --cooldown 200 --start 2025-09-09
python oneoff/backfill_money_flow_loop.py --host money --shard 0/2 --cooldown 70 --start 2025-09-09
:: 单轮调试（不带 loop）：
python oneoff/backfill_money_flow.py --start 2025-09-09 --host money --limit 20

:: 4) 执行选股
python run_pick.py --list                      :: 查看策略
python run_pick.py --strategy MA_BULL          :: 均线多头
python run_pick.py --strategy VOLUME_BREAKOUT  :: 放量突破
python run_pick.py --strategy VALUE            :: 低估值

:: 4b) 每日一键更新（数据全链路 + 全策略选股；或双击 每日更新.bat，
::     控制台实时输出 + 日志落盘 logs\daily_update_*.log。建议每交易日收盘后运行）
python daily_update.py
::    链路内含 gapfill 加固: 自动探测近 30 日 amount/估值缺口, 有缺口才走
::    baostock 回填(oneoff/gapfill_amount_valuation.py), 平时空转 ~1s

:: 5) 启动 Web 服务（或双击 start_server.bat），打开 http://127.0.0.1:8600
::    「市场面板」页: 两融情绪 / 估值温度 / 人气榜 Top20 / 资金流对比（二期面板）
start_server.bat
```

## 内置策略

| 策略 | 规则 | 参数 |
|---|---|---|
| 均线多头 MA_BULL | MA5>MA10>MA20 且收盘站上 MA20 且 MA5 连续上行 | ma_rising_days=3 |
| 放量突破 VOLUME_BREAKOUT | 收盘创 20 日新高 且量≥2×20日均量 且涨幅>3% | window/vol_multiple/min_pct_chg |
| 低估值 VALUE | 0<PE<20 且 0<PB<2 且总市值≥100亿 | max_pe/max_pb/min_mv |
| MACD金叉 MACD_GOLDEN | DIF 上穿 DEA（EMA12/26/9） | fast/slow/signal/above_zero |
| 缩量回调 PULLBACK | MA20上行中回调1%~12%不破MA20 且量比<0.7 且贴近MA10 | pullback_days/vol_shrink/near_ma10_pct |
| 连续阳线 CONSEC_YANG | 连续4日收阳且逐日走高 且累计涨幅≤15% | days/max_total_gain |
| 一周主线低吸 WEEKLY_MAINLINE | 主线(通信/电子/计算机)分歧低吸 + 农业资源轮动套利 + 大盘环境闸门 | 详见 docs/04 |
| 涨停复盘 ZT_POOL_FOLLOW | 涨停池连板接力: 1-3板 + 封板质量(开板≤2/首封≤10:30/封单≥2000万/换手≤25%) + 行业分散 + 大盘闸门 | min_boards/max_boards/max_break_cnt/lock_before/min_lock_amt/max_turnover/max_per_industry/use_index_gate |
| 低估值分位 VALUE_PCTL | PB 处自身近5年历史 ≤20% 分位(纵向比便宜, 与 VALUE 绝对阈值互补) + PE>0 + 市值≥50亿 + 历史≥250天 | lookback_years/max_pb_pctl/min_mv/min_hist_days/require_positive_pe |
| 价资背离 FUND_FLOW_DIVERGE | 股价未大涨(∈[-7,+2.5]%)但新浪主力净流入≥1000万/占比≥3% + THS净额同向确认 + 换手∈[1,15]% + 大盘闸门 | min_pct/max_pct/min_main_net/min_main_pct/min_ths_net/min_turnover/max_turnover/require_both_sources/use_index_gate/top_n |
| 两融趋势 MARGIN_TREND | 融资余额5日中≥4日连升且累计+2% + 融资买入≥前5日均(放量) + 价∈[-3,5]% + 量比≤3 + 成交额≥2亿 + 大盘闸门(数据: stock_margin_detail, 交易所官网250日) | ramp_days/min_rise_days/min_rzye_pct/min_buy_ratio/min_pct/max_pct/max_vol_ratio/min_amount/use_index_gate/top_n |
| 业绩质量 FIN_QUALITY | 财报硬指标: ROE按报告期折算年化当量≥10 + 营收/净利同比双正 + 毛利率与上期变化≤5pp + 市值≥30亿(数据: stock_fin_report, 按 announce_date ≤ t0 重放防未来函数) | min_roe_annual/max_gm_change_pp/min_mv/top_n |
| 低估+质量双因子 VALUE_QUALITY | VALUE_PCTL ∩ FIN_QUALITY 交集: 质量侧复用 FIN_QUALITY 全口径(as-of 公告日重放), 估值侧 PB/PE 按 t0 截断窗口现算分位(修正回测未来函数); 两侧池内 rank 归一化加权合成 | lookback_years/max_pb_pctl/min_mv/min_hist_days/min_roe_annual/max_gm_change_pp/min_mv_quality/quality_weight/top_n |
| 缠论二买 CHAN_BUY2 | 简化缠论日线量化: 窗口内底分型一买 L1 → 反弹≥5% → 回踩不破一买(L2≥L1)且幅度 2%~15%、L2 底分型 → 3 日内收阳确认; 缩量回踩/DIF 底背驰(DIF 现算)/站上 MA5 加分(数据: stock_daily_quote) | lookback_days/rebound_min_pct/pullback_min_pct/pullback_max_pct/confirm_days/break_tolerance_pct/require_vol_shrink/min_hist_days/top_n |

> **CHAN_BUY2 回测实录（2026-10-19，诚实口径）**：10-12 基准日 50 命中、持有 5 日胜率 18%、平均 -3.63%（同期市场回调，二买形态在下跌中继失效）；纪律化退出（-3% 止损）平均 -2.45%。单基准日小样本，不按此调参（防过拟合）。

> 所有策略均剔除 ST 股；选股范围统一为大A（沪深主板+创业板，688/689 科创板由框架 `filter_universe` 统一剔除）。

> **策略参数 DB 化（v1.6）**：参数与启停存 `stock_strategy_config` 表（服务启动自动建表播种，seed=类默认值不改变行为），合并优先级 **类默认值 < 库表 params_json < 运行时传入**；`POST /api/strategy_config/{code}` 改参/停用即时生效、无需改代码重启；`enabled=0` 时 `run_pick_all`（每日盘后调度）跳过该策略；回测同样读库表口径。

> **0 命中怎么判读**：`WEEKLY_MAINLINE` 与 `ZT_POOL_FOLLOW` 带「大盘环境闸门」——上证指数 MA5<MA20（死叉）时按设计**不开新仓**，此时必然 0 命中（如 2026-09-11~09-16 连续死叉；09-08/09-09 金叉时各出 3 只）。
> 批次表 `stock_pick_batch.message` 会写明原因（如 `大盘弱势不开新仓: 上证MA5<MA20(3893/3923)(上证死叉)`），选股结果页顶部以「批次说明」展示，用于区分**防御性 0** 与**策略被筛空**。

## 回测与交集

- 回测：`GET /api/backtest/{strategy}?days_ago=5` —— 历史基准日模拟选股，双口径对照：持有至今 + 纪律化退出（-3%硬止损/+5%上移保本/T+3强平，参数可调 stop_pct/breakeven_trigger/max_hold_days）
- **多策略交集已并入「选股结果」页（v1.7，原独立页面下线）**：筛选栏新增「多策略交集」下拉（无默认，≥2/≥3/≥4策略，按当日命中策略数过滤）；接口 `GET /api/pick/intersection?min_strategies=2` 保留可用
- 选股结果页支持：**表头三态排序**（点击 升序▲ → 降序▼ → 原序⇅）与 **「指数成分」下拉筛选**（选项来自 `stock_basic.index_tags` 去重）
- 选股结果页的「按代码/名称」筛选框在 `stock_pick_result` 查不到匹配时，**自动降级查询 `stock_daily_quote` 最近交易日数据**并标注「兜底行情」徽标
- 选股结果列表**按 (股票, 基准日) 消重**：同股同日被多个策略命中只显示一行（保留 score 最高策略的得分/批次/命中原因），全部命中策略在「命中策略」列聚合展示（v1.6）

## 行情面板（多页共用）

选股结果/暗盘信号/涨停池/市场面板各挂载一个行情面板实例（同一套 `QP` 模板，`data-r` 角色标记区分），交互完全一致：

- 报价带：名称/现价/涨跌额 + 今开/昨收/最高/最低/成交量/**量比**/**换手率**/PE(TTM)/总市值/**连涨天数**/**连涨涨幅** + 行业/概念/指数成分标签
- 悬浮提示：OHLC、涨跌额/幅、振幅、成交量、量比、换手率、连涨天数/连涨涨幅、MA/(BOLL) 值、各副图指标值
- 「⚙ 详情设置」（localStorage 持久化）：周期（分时/30分/日K/五日/周K/月K）、K线样式（实心/空心阳线）、显示缺口、**主图叠加（MA(5,10,20) / BOLL(20,2) / 两者 / 不叠加）**
- 副图 1-3 张可选（VOL/MACD/KDJ/RSI/BIAS/OBV/WR/CCI），标题右侧标注最新值；KDJ(20/50/80)、RSI(30/50/70)、WR(20/50/80)、CCI(±100) 带超买超卖参考线；MACD 柱按强弱分级配色
- 主图价格轴右侧外置；柱宽随缩放动态变化（`fixBarGap`，蜡烛与副图柱同步）

相关 API：

- `GET /api/search?q=关键词` —— 代码/名称模糊搜索（含标签）
- `GET /api/stock/{ts_code}` —— 个股基础信息+板块标签
- `GET /api/quote/{ts_code}?days=30` —— 日K
- `GET /api/minute/{ts_code}?date=YYYY-MM-DD&scale=5` —— 分时（date 默认库内最新）

## 板块行情与暗盘信号

两个独立页面（菜单「板块行情」「暗盘信号」），方案与口径见 `docs/06-板块行情与暗盘-实施方案.md`。

- **v1.7 布局变更（2026-10-19）**：板块行情表格默认按**涨跌幅降序**；暗盘信号页改 **50:50 分栏**——左表新增 命中策略/基准日/连涨天数/连涨涨幅 4 列、列宽自适应+横向滚动、全列三态排序（默认涨跌幅降序），右侧为行情K线（点行联动）；涨停池页同样 50:50（左表格+梯队/行业图，右 K 线联动），连板筛选新增「首板」选项（精确 cont_boards=1，选项排序：全部涨停/首板/≥2/≥3/≥4/≥5连板；`/api/ztpool/pool` 新增 `exact_boards` 参数，精确匹配优先于 min_boards 下限）；市场面板底部新增行情K线模块（人气榜/资金流对比点股联动），人气榜/资金流对比均加 TOP 单选筛选（默认 TOP10/TOP5）且两模块等高滚动。

**板块行情（需求 #19）** —— 新浪申万行业口径，49 个一级行业：

- 基础信息：成分股数量、总市值/流通市值、人气排名（同花顺热榜按成分股人气前 10 均值聚合）、板块日线指数（成分股流通市值加权、基期 1000）设**成分覆盖门槛**：当日有效成分股数 <max(3, 20%×成分股数) 不出行（防单只股票冒充板块指数）；板块概念解读（49 条行业知识库 + 实时统计两段式：先行业范围与关键驱动，再成分股数/市值/龙头/涨跌幅；外部源同花顺/东财板块页需 token，2026-09-15 实测不可直接抓取）、**创建日期为代理口径**（成分股最早上市日，外源不公开）
  - ⚠️ 人气排名口径：同花顺热榜端点**硬上限 100 只**（`page`/`size`/`list_type` 均无法扩大），49 个行业里只有约 26 个"有成分股在榜"，名次**只在这 26 个板块内部排序**，其余板块该列为空。页面顶部会显示实际覆盖数（如 `人气榜覆盖 26/49`）。
- 成分股：`stock_board_member` 打两类标签 —— **龙头**（0.4×流通市值分位 + 0.3×近20日涨幅分位 + 0.3×近20日成交额占比分位，TOP3）与**高相关**（与板块指数 60 日收益率相关系数 ≥0.7 或 TOP20%）
  - ⚠️ 覆盖面：新浪 `newSinaHy.php` 这 49 个行业的成分股**合计仅 2,563 只**（全市场 5,218 只），即创业板/科创板多数股票不在其行业分类内 —— 属**源侧限制**（逐板块核对：`stock_board_basic.member_cnt` 与 `stock_board_member` 实际行数**完全一致**，无抓取遗漏）。故板块指数/分时只反映这 2,563 只。
- 日线：**成分股流通市值加权收益复利累乘**合成板块指数（基期 1000，同时落等权对照列 `pct_chg_eq`）；覆盖范围随 `stock_daily_quote` 的历史深度变化 —— 该表已向前扩展到 **2019-12-20（1,168 个交易日）**，重跑 `--boardsynth` 即把板块指数一并拉长（成分股历史不足的早期交易日会自动少算，看 `stock_board_daily_quote` 的实际 range）；含 MA5/10/20、连涨天数/连涨涨幅
- 分时：成分股 5 分钟 bar 按流通市值加权（相对昨收归一），保留最近 30 个有效分时日（有效日 = 当日成分股覆盖 ≥100 只，用于滤掉零星残留）

**暗盘信号（需求 #20）** —— A 股盘后暗线 = 大宗交易 + 龙虎榜（不涉港股新股暗盘）：

- `stock_block_trade` 大宗逐笔（成交价/量/额、折溢价率、买卖营业部）；`stock_billboard` 龙虎榜（上榜原因、买卖额、净买入、成交占比、机构专用席位净买入）
- `stock_dark_pool_indicator` 派生指标：近 5 日大宗加权折溢价率、龙虎榜净买入、`dark_score`（横截面 z-score 加权：折溢价 0.35 / 龙虎榜净买占流通市值 0.25 / 机构净买 0.20 / 大宗占成交额比 0.20），信号标签 **机构吸筹 / 减持出货 / 游资活跃 / 无**
  - **「机构吸筹」口径 2026-09-15 已修订**：原「大宗折溢价 > 2% 且机构净买 > 0」全区间 **0 命中**（溢价大宗仅占 4.7%，与龙虎榜同日同股几乎不共存）；现改为「**大宗折价成交（折溢价 < 0）且当日机构席位净买 > 0**」= 折价出让 + 机构承接 → 实测 103 行 / 85 只。

相关 API：

- `GET /api/boards` —— 板块列表（最新快照）
- `GET /api/boards/{board_code}` —— 基础信息 + 概念解读
- `GET /api/boards/{board_code}/members` —— 成分股（含龙头/高相关标签与相关系数）
- `GET /api/boards/{board_code}/daily?days=1200` —— 板块日线（合成指数，已覆盖 ~1,037 天）
- `GET /api/boards/{board_code}/minute_dates` —— 该板块有分时的交易日列表（默认最近 30 个）
- `GET /api/boards/{board_code}/minutes?date=` —— 板块分时（近 30 交易日）
- `GET /api/darkpool/signals?trade_date=&signal=&limit=` —— 暗盘信号列表（按 dark_score 降序）
- `GET /api/darkpool/stock/{ts_code}` —— 个股暗盘明细（大宗逐笔 + 龙虎榜 + 指标序列）

## 每日盘后自动化

WorkBuddy 自动化「选股系统-每日盘后随机调度器」：周一至周五 15:30 触发，将当天盘后任务调度到 **15:30~18:30 之间的随机时刻**执行（每日时间不固定）；执行内容为 `run_collect.py --all` + 7 策略选股，非交易日自动跳过；一次性实例执行完毕（含跳过/失败分支）自动自删，调度器每日创建前兜底清理历史同名实例。

## 数据库

MySQL `172.16.3.16:30306 / test_project`：`stock_basic`（含 industry/concepts/index_tags/list_date 列；PE/PB/市值为**最新快照**）、`stock_daily_quote`（含 **pe_ttm/pb/total_mv/circ_mv 日频估值快照**，自 2026-09-08 前滚积累，回测按 t0 取用防未来函数）、`stock_index_daily`（7 主流指数日线）、`stock_money_flow`（个股资金流向, 前滚积累）、`stock_minute_quote`（5分钟分时，前滚积累，2026-09-09 起取消"仅保留3日"自动清理；历史区间由新浪 `CN_MarketData.getKLineData` 回填（baostock 已不可用））、`stock_pick_batch`、`stock_pick_result`、`stock_board_basic` / `stock_board_daily_quote` / `stock_board_minute_quote` / `stock_board_member`（板块行情，需求 #19）、`stock_block_trade` / `stock_billboard` / `stock_dark_pool_indicator`（暗盘信号，需求 #20）、`stock_zt_pool_dt` / `stock_zt_pool_zbgc` / `stock_zt_pool_prev`（跌停/炸板/昨涨停池）、`stock_fin_report`（业绩报表季度）、`stock_adjust_factor`（复权因子事件）、`stock_hot_rank`（人气榜Top100快照）、`stock_margin_detail`（两融明细）、`stock_zt_pool` / `stock_zt_pool_strong` / `stock_zt_pool_dt` / `stock_zt_pool_zbgc` / `stock_zt_pool_prev`（涨跌停五池，2026-09-20 扩容）、`stock_valuation` / `stock_valuation_pctl` / `stock_money_flow_ths` / `stock_sw_industry_member`、`stock_fin_report`（业绩报表·季度，2026-09-20 加）、`stock_adjust_factor`（复权因子事件表，只存除权变化点，2026-09-20 加）（详见 sql/schema.sql；策略数据要素完整性审计见 docs/05）。

**复权因子口径**：`stock_daily_quote` 为不复权且 `pre_close=close.shift(1)` 派生，除权日 pct_chg 会把缺口算成跌幅。`stock_adjust_factor` 只存变化点（factor = raw_close/qfq_close，除权日跳变；跨除权日复权收益 = raw(t)/factor(t) ÷ (raw(t-1)/factor(t-1)) − 1，段内 raw 收益即复权收益）。回填走腾讯 qfq（`--adjseed`），日常增量零请求预警（THS 官方 close/pct 反推隐含昨收 vs 库内昨收偏差 >0.5% 才逐股验证，单轮上限 50 只防 WAF）。

## 数据源说明（新浪财经，实测可用）

| 用途 | 接口 |
|---|---|
| 股票列表+名称 | `Market_Center.getHQNodeData`（hs_a 节点分页；估值字段已弃用，per 为静态口径） |
| **估值四字段（PE-TTM/PB/总市值/流通市值）** | 腾讯 `qt.gtimg.cn` 88字段快照（f39/f46/f45/f44，60只/批，真TTM口径，2026-09-08 起；新浪值兜底） |
| **历史估值回填** | Baostock `query_history_k_data_plus`（peTTM/pbMRQ 日频，免费免 token，与腾讯口径偏差<1%；市值=当日收盘×最新股本回推） |
| 板块标签 | `Market_Center.getHQNodes`（节点树：申万行业 31 + 概念 214 + 指数成分白名单；概念体系陈旧、缺热点） |
| **概念板块（热点）** | 东财 `push2delay` clist（板块列表 fs=`m:90+t:3+f:!50` 约 500 板块含 PCB/CPO概念/液冷服务器/算力概念等，成分 fs=`b:BKxxxx` 分页；与新浪概念合并去重写入 concepts，东财概念在前） |
| 日K线 | **腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get`（主源，`param=code,day,,,N,` 末段留空=不复权）**；新浪 `CN_MarketData.getKLineData`（scale=240，成交量股÷100转手）兜底。两源字段口径一致（均无成交额，由 moneyflow 回填） |
| 指数日K | **新浪主源** `CN_MarketData.getKLineData` + **腾讯兜底** `web.ifzq.gtimg.cn/appstock/app/fqkline/get`（7 指数：sh000001/sz399001/sz399006/sh000016/sh000300/sh000905/sh000688）。两源实测口径一致（同日 close 差 ≤0.005、**vol 逐位相同**）；腾讯单次上限 ~1500 根（N=3000 返空），新浪无上限 |
| 分钟K | 新浪（scale=5，48 根/日；腾讯分钟端点公司 DNS 不可达） |
| 上市日期 | 新浪 F10 `vCI_CorpInfo`（HTML 解析，仅补缺；东财 f26 兜底） |
| **个股资金流向（日度·历史）** | **新浪 `MoneyFlow.ssl_qsfx_zjlrqs`**（`num=400` 单次回溯约 21 个月；`vip` / `money` **双域名限流独立**，按 `--host` + `--shard I/N` 分片并行，回填用 `oneoff/backfill_money_flow.py` + `oneoff/backfill_money_flow_loop.py`（已归档 oneoff/），`INSERT IGNORE` 只补缺口） |
| 资金流向（当日增量） | 东财 `push2delay` clist 延时批量快照（f62 主力/f66 超大/f72 大/f78 中/f84 小/f184 主力占比，单页上限100；f6 成交额顺带回填 stock_daily_quote.amount；f26 上市日期顺带补缺 stock_basic.list_date 的 NULL 行） |
| **板块基础信息（行业）** | 新浪 `vip.stock.finance.sina.com.cn/q/view/newSinaHy.php`（行业代码/名称/成分股数/均价/涨跌幅/成交额/领涨股）；成分股 = 本地 `stock_basic.industry` 映射；市值/涨跌家数本地聚合 |
| **板块人气排名** | 同花顺热榜 `dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock`（`rate` 人气值 / `hot_rank_chg` 名次变化）→ 板块人气 = 成分股人气前 10 均值 |
| **板块日线/分时** | 本地合成（东财 `push2his` 板块K线已被 IP 封禁、`push2`/`trends2` 仅支持 BK 代码且无历史）——日线 = 成分股流通市值加权收益复利累乘；分时 = 成分股 5 分钟 bar 加权 |
| **大宗交易（逐笔）** | 东财 `datacenter-web.eastmoney.com/api/data/v1/get` `reportName=RPT_DATA_BLOCKTRADE`（折溢价率/成交量额/买卖营业部，历史可回溯） |
| **龙虎榜** | 同上 `RPT_DAILYBILLBOARD_DETAILSNEW`（上榜原因/成交占比）+ `RPT_BILLBOARD_DAILYDETAILSBUY/SELL`（席位明细，机构专用/知名游资） |
| **涨跌停五池（2026-09-20 扩容）** | 东财 `push2ex.eastmoney.com`（getTopicZTPool/getTopicQSPool/getTopicDT_POOL/getTopicZB_POOL + 昨涨停表现），经 akshare `stock_zt_pool_*_em`（vendored 源码）；跌停空日返回无列空 df，0 行正常 |
| **业绩报表（季度）** | 东财 `datacenter` `RPT_LICO_FN_CPD`，经 akshare `stock_yjbb_em(date=报告期)`——按报告期批量页翻（全市场 ~23 页/6s），避免单股 5000+ 请求；含北交所（ts_code 后缀按代码首位推断，全量入库策略层过滤） |
| **复权因子** | 腾讯 `fqkline` `param=code,day,,,N,qfq`（返回 key=`qfqday`，与不复权同构；2026-09-20 实测茅台 2025-06 除权跳变清晰）与库内不复权 close 比对取变化点 |

- 注：东财 `push2his` 对本机 IP 长期封禁（`RemoteDisconnected`）且已按需求约定**全面弃用**；东财 `push2`/限流、腾讯 fqkline 批量采集会触发 WAF（约 5000+ 次/短窗口）、新浪约 400 次/窗口触发 456 挑战页（约 5 分钟恢复）；东财仅 `push2delay` 延时快照可用；采集会话直连（`trust_env=False` 绕过系统代理）
- 并发：日K 16 线程（腾讯主源，全市场约 2~3 分钟）+ 双源熔断兜底；分时约 83 秒；资金流历史回填 **money 域名约 280 只/分钟**、vip 域名约 60 只/分钟（全局共享节奏 + 请求预算 + 挑战页即熔断）；分时历史回填 `--minbackfill-sina` 单票 1 请求取回 32 天，双域名轮询，全市场约 20~40 分钟（取决于域名冷却）
- **指数双源（2026-09-16 加）**：`index_collector` 由新浪单源改为**新浪主源 + 腾讯 `fqkline` 兜底**（`_fetch_index_with_source`，逐指数自动切换）。此前指数只有新浪单源，反爬即整批失败，而旧兜底走 baostock（已整体不可用）。实测两源同日值一致：close 差 ≤0.005（三位小数舍入）、**vol 逐位相同**（故腾讯指数 vol **不除 100**，新浪需 ÷100）、ma20 差 ≤0.002；科创 50 腾讯亦有数据。腾讯单次 N≤1500 可用、N=3000 返回空。`sync_index_daily` 返回值新增 `sources` / `tx_fallback` 便于观测。
- **baostock 已不可用（2026-09-15 实测）**：`login()` / `query_history_k_data_plus()` 均返回成功，但结果集 `rs.next()` **永久阻塞**（响应头到达、数据体永不返回），日线查询与 `query_all_stock()` 同样卡死；TCP `public-api.baostock.com:10030` 可正常建连。故 baostock 链路的 `--minbackfill`/`--valbackfill` 及 gap_fill_*_baostock.py 已于 2026-09-17 全部移除；历史估值已由 akshare 通道补齐（`--valuation`，东财 RPT_VALUEANALYSIS_DET 全历史 8.7 年）
- **资金流回填冷却要按域名设**：`money.finance` 限流后约 60s 恢复 → `--cooldown 70`；`vip.stock.finance` 约 204s 恢复 → `--cooldown 200`。两域名额度独立，用 `--host` + `--shard I/N` 分片并行
- 日频衍生指标：`cont_days`（连涨天数）/`cont_pct`（连涨涨幅）/`vol_ratio`（量比）/`turnover_rate`（换手率）。增量：`--quotes` 顺带计算前三个（自足于K线），`--valsnap` 在估值落库后补算换手率；全量/区间重算：`python -m app.collector.indicator_collector [--days N]`

- **滚动窗口指标写库的预热陷阱（2026-09-16 修）**：`stock_index_daily` 旧的 `ma5/ma10/ma20` 只在本轮抓取的 `count` 根窗口内 `rolling()` 计算，窗口头部 (window-1) 根必为 NaN，却仍被 UPSERT 写库 —— 等于用 NULL **覆盖**库里原本正确的均线值；且抓取窗口逐日前移，旧行永不回补，空洞永久留存。实测 `--index`（days=25）使 `ma20` 出现 **2026-07-21~09-08 连续 33 个交易日的整段 NULL**，令「大盘环境闸门」在这些日子静默退化/失效。修法：`_fetch_index` 多拉 25 根预热、丢弃 `ma20` 仍为 NaN 的行后再写；并全量重算 + 回填指数历史至 ~4.2 年（1030 行 × 7 指数，ma20 NULL=0）。**凡是给受限窗口算滚动指标并写库的采集器都要按此校验。**
- **均线口径全表审计（2026-09-16）**：把所有写 `rolling()` 指标的表/脚本都过了一遍 ——
  `stock_index_daily` 曾有中部空洞（已修，见上）；**`stock_daily_quote` 正常**（`ma5` NULL 合计 20,871 ≈ 5,218 票 × 前 4 根、`vol_ma20` 99,100 ≈ 5,216 × 前 19 根，正是序列开头预热行，**无中部空洞**；抽验 000001.SZ/600036.SH 均在各自第 20 根才出现 ma20）；
  **`stock_board_daily_quote` 已修**：原 `ma5/ma10/ma20` 以实际切片长度作分母，序列初段拿「短窗均值」冒充（第 2 根就给出 MA20），与个股/指数「满窗才有值」口径不一致，现改为 `i >= w-1` 才有值（`ma5/ma10/ma20` NULL = 196/441/931 = 49 板块 × 4/9/19）；
  ~~`gap_fill_index_baostock.py` / `gap_fill_quotes_baostock.py`~~（baostock 链路 2026-09-17 已随死链清理删除，防御经验保留在此备忘）；`darkpool` 指标（落库 `block_days5` 自证参与天数）经核无此问题。

> 免责声明：本系统仅供研究学习，不构成任何投资建议。
