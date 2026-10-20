-- =============================================================
-- 选股系统 数据库 schema
-- 目标库: test_project @ 172.16.3.16:30306
-- 说明: 所有表带 etl_insert_time / etl_update_time 审计字段
-- =============================================================

-- 股票基础信息表（含基本面快照）
CREATE TABLE IF NOT EXISTS stock_basic (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '带市场后缀代码, 如 600519.SH',
    symbol          VARCHAR(8)   NOT NULL COMMENT '纯数字代码, 如 600519',
    name            VARCHAR(32)  NOT NULL COMMENT '股票名称',
    market          VARCHAR(8)   NOT NULL COMMENT '市场: SH/SZ/BJ',
    pe_ttm          DECIMAL(12,4) NULL COMMENT '市盈率TTM(快照)',
    pb              DECIMAL(12,4) NULL COMMENT '市净率(快照)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    is_active       TINYINT      NOT NULL DEFAULT 1 COMMENT '是否在市 1/0',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_ts_code (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息表';

-- 日线行情表（含均线指标）
CREATE TABLE IF NOT EXISTS stock_daily_quote (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    open            DECIMAL(12,3) NULL COMMENT '开盘价',
    high            DECIMAL(12,3) NULL COMMENT '最高价',
    low             DECIMAL(12,3) NULL COMMENT '最低价',
    close           DECIMAL(12,3) NULL COMMENT '收盘价',
    pre_close       DECIMAL(12,3) NULL COMMENT '昨收价',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    vol             DECIMAL(20,2) NULL COMMENT '成交量(手)',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    ma5             DECIMAL(12,3) NULL COMMENT '5日均线',
    ma10            DECIMAL(12,3) NULL COMMENT '10日均线',
    ma20            DECIMAL(12,3) NULL COMMENT '20日均线',
    vol_ma20        DECIMAL(20,2) NULL COMMENT '20日均量',
    cont_days       INT           NULL COMMENT '连涨天数(正=连续上涨,负=连续下跌,0=平盘)',
    -- 估值日频快照(由 basic_collector.snapshot_valuation 幂等加列+每日同源拷贝,
    -- 自 2026-09-08 起前滚积累, stock_basic 仅存最新值, 回测须用本表 t0 快照防未来函数):
    -- pe_ttm DECIMAL(12,4) / pb DECIMAL(12,4) / total_mv DECIMAL(20,2) / circ_mv DECIMAL(20,2)
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情表';

-- 选股批次表
CREATE TABLE IF NOT EXISTS stock_pick_batch (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '批次号',
    batch_no        VARCHAR(32)  NOT NULL COMMENT '批次编号 P<时间>-<策略>',
    strategy_code   VARCHAR(32)  NOT NULL COMMENT '策略代码',
    strategy_name   VARCHAR(64)  NOT NULL COMMENT '策略名称',
    trade_date      DATE         NULL COMMENT '数据基准日(最近交易日)',
    params          VARCHAR(512) NULL COMMENT '策略参数JSON',
    hit_count       INT          NOT NULL DEFAULT 0 COMMENT '命中数量',
    status          VARCHAR(16)  NOT NULL DEFAULT 'SUCCESS' COMMENT 'SUCCESS/FAILED',
    message         VARCHAR(512) NULL COMMENT '备注/错误信息',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '执行时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_batch_no (batch_no),
    KEY idx_strategy (strategy_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股批次表';

-- 选股结果明细表
CREATE TABLE IF NOT EXISTS stock_pick_result (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    batch_id        BIGINT       NOT NULL COMMENT '关联批次ID',
    batch_no        VARCHAR(32)  NOT NULL COMMENT '批次编号(冗余)',
    strategy_code   VARCHAR(32)  NOT NULL COMMENT '策略代码',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    name            VARCHAR(32)  NOT NULL COMMENT '股票名称',
    trade_date      DATE         NULL COMMENT '基准交易日',
    close_price     DECIMAL(12,3) NULL COMMENT '收盘价',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '当日涨跌幅%',
    score           DECIMAL(10,3) NULL COMMENT '命中强度得分',
    hit_reason      VARCHAR(256)  NULL COMMENT '命中原因',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    KEY idx_batch (batch_id),
    KEY idx_strategy_date (strategy_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股结果明细表';

-- 分时行情表（分钟级K线, 默认5分钟）
CREATE TABLE IF NOT EXISTS stock_minute_quote (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    scale           SMALLINT     NOT NULL DEFAULT 5 COMMENT '周期(分钟): 5/15/30/60',
    bar_time        DATETIME     NOT NULL COMMENT 'K线时间',
    open            DECIMAL(12,3) NULL COMMENT '开盘价',
    high            DECIMAL(12,3) NULL COMMENT '最高价',
    low             DECIMAL(12,3) NULL COMMENT '最低价',
    close           DECIMAL(12,3) NULL COMMENT '收盘价',
    vol             DECIMAL(20,2) NULL COMMENT '成交量(手)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_scale_time (ts_code, scale, bar_time),
    KEY idx_bar_time (bar_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='分时行情表(分钟K线)';

-- 指数日线行情表（大盘环境判断用, 数据源: 新浪K线）
CREATE TABLE IF NOT EXISTS stock_index_daily (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    index_code      VARCHAR(16)  NOT NULL COMMENT '指数代码, 如 sh000001',
    index_name      VARCHAR(32)  NOT NULL COMMENT '指数名称, 如 上证指数',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    open            DECIMAL(12,3) NULL COMMENT '开盘点位',
    high            DECIMAL(12,3) NULL COMMENT '最高点位',
    low             DECIMAL(12,3) NULL COMMENT '最低点位',
    close           DECIMAL(12,3) NULL COMMENT '收盘点位',
    pre_close       DECIMAL(12,3) NULL COMMENT '昨收点位',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    vol             DECIMAL(20,2) NULL COMMENT '成交量(手)',
    ma5             DECIMAL(12,3) NULL COMMENT '5日均线',
    ma10            DECIMAL(12,3) NULL COMMENT '10日均线',
    ma20            DECIMAL(12,3) NULL COMMENT '20日均线',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (index_code, trade_date),
    KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数日线行情表';

-- 个股资金流向表（日度, 数据源: 东方财富 push2his fflow）
CREATE TABLE IF NOT EXISTS stock_money_flow (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    main_net        DECIMAL(20,2) NULL COMMENT '主力净流入(元)=超大单+大单',
    super_net       DECIMAL(20,2) NULL COMMENT '超大单净流入(元)',
    large_net       DECIMAL(20,2) NULL COMMENT '大单净流入(元)',
    mid_net         DECIMAL(20,2) NULL COMMENT '中单净流入(元)',
    small_net       DECIMAL(20,2) NULL COMMENT '小单净流入(元)',
    main_pct        DECIMAL(8,3)  NULL COMMENT '主力净流入占成交额比%',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股资金流向表(日度)';

-- 上市日期（stock_basic 扩展列, 由 basic_collector.sync_list_dates 幂等添加）:
-- ALTER TABLE stock_basic ADD COLUMN list_date DATE NULL COMMENT '上市日期' AFTER market;

-- =============================================================
-- 板块行情（v1.5, 2026-09-15, 需求#19, 口径见 docs/06）
-- 板块体系: 新浪申万行业（board_code = 行业节点代码, board_type='industry'）
-- =============================================================

-- 板块基础信息快照（每日一行, 前滚）
CREATE TABLE IF NOT EXISTS stock_board_basic (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    board_code      VARCHAR(16)  NOT NULL COMMENT '板块代码: 新浪行业节点代码, 如 new_sw270000',
    board_name      VARCHAR(64)  NOT NULL COMMENT '板块名称(申万行业名)',
    board_type      VARCHAR(16)  NOT NULL DEFAULT 'industry' COMMENT 'industry 行业 / concept 概念(预留)',
    src             VARCHAR(16)  NOT NULL DEFAULT 'sina+local' COMMENT '来源: sina 新浪快照 / local 本地聚合 / sina+local 混合',
    member_cnt      INT           NULL COMMENT '成分股数量(当前活跃, 含停牌)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值合计(元, 本地聚合)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值合计(元, 本地聚合)',
    list_proxy_date DATE          NULL COMMENT '创建日期代理口径(成分股最早上市日, 非官方)',
    hot_rank        INT           NULL COMMENT '板块人气排名(按 hot_score 横截面排名)',
    hot_score       DECIMAL(16,4) NULL COMMENT '板块人气值(成分股同花顺人气前10均值)',
    up_cnt          INT           NULL COMMENT '上涨家数(最近交易日)',
    down_cnt        INT           NULL COMMENT '下跌家数(最近交易日)',
    lead_stock      VARCHAR(32)   NULL COMMENT '领涨股(新浪快照)',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '板块涨跌幅%(新浪快照)',
    amount          DECIMAL(20,2) NULL COMMENT '板块成交额(元, 新浪快照)',
    summary         VARCHAR(1024) NULL COMMENT '板块概念解读(本地模板生成)',
    trade_date      DATE          NOT NULL COMMENT '交易日',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (board_code, trade_date),
    KEY idx_date_type (trade_date, board_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块基础信息快照(日度, 新浪行业口径)';

-- 板块日线（成分股流通市值加权合成, 含等权对照列）
CREATE TABLE IF NOT EXISTS stock_board_daily_quote (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    board_code      VARCHAR(16)  NOT NULL COMMENT '板块代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    open            DECIMAL(12,3) NULL COMMENT '开盘指数',
    high            DECIMAL(12,3) NULL COMMENT '最高指数',
    low             DECIMAL(12,3) NULL COMMENT '最低指数',
    close           DECIMAL(12,3) NULL COMMENT '收盘指数(基期=1000)',
    pre_close       DECIMAL(12,3) NULL COMMENT '昨收指数',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%(流通市值加权)',
    pct_chg_eq      DECIMAL(8,3)  NULL COMMENT '涨跌幅%(等权对照)',
    vol             DECIMAL(20,2) NULL COMMENT '成交量合计(手)',
    amount          DECIMAL(20,2) NULL COMMENT '成交额合计(元)',
    ma5             DECIMAL(12,3) NULL COMMENT '5日均线',
    ma10            DECIMAL(12,3) NULL COMMENT '10日均线',
    ma20            DECIMAL(12,3) NULL COMMENT '20日均线',
    cont_days       INT           NULL COMMENT '连涨天数(正=连续上涨,负=连续下跌,0=平盘)',
    cont_pct        DECIMAL(10,4) NULL COMMENT '连续涨跌幅%',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (board_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块日线(成分股流通市值加权合成)';

-- 板块分时（成分股 5 分钟分时成交额加权合成, 仅保留最近 30 个交易日）
CREATE TABLE IF NOT EXISTS stock_board_minute_quote (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    board_code      VARCHAR(16)  NOT NULL COMMENT '板块代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    bar_time        DATETIME     NOT NULL COMMENT 'K线时间(5分钟)',
    open            DECIMAL(12,3) NULL COMMENT '开盘指数',
    high            DECIMAL(12,3) NULL COMMENT '最高指数',
    low             DECIMAL(12,3) NULL COMMENT '最低指数',
    close           DECIMAL(12,3) NULL COMMENT '收盘指数',
    vol             DECIMAL(20,2) NULL COMMENT '成交量合计(手)',
    amount          DECIMAL(20,2) NULL COMMENT '成交额合计(元)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_time (board_code, bar_time),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块分时(最近30个交易日)';

-- 板块成分股映射 + 关联度 + 龙头标签（全量覆盖式刷新）
CREATE TABLE IF NOT EXISTS stock_board_member (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    board_code      VARCHAR(16)  NOT NULL COMMENT '板块代码',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '成分股代码',
    corr60          DECIMAL(8,4)  NULL COMMENT '与板块指数60日收益率相关系数',
    rel_tag         VARCHAR(16)   NULL COMMENT '相关性标签: core 高相关(>=0.7或TOP20%) / normal',
    leader_tag      VARCHAR(16)   NULL COMMENT '龙头标签: leader 龙头(TOP3) / normal',
    leader_score    DECIMAL(10,4) NULL COMMENT '龙头综合得分(0.4流通市值分位+0.3近20日涨幅分位+0.3成交额占比分位)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    chg20           DECIMAL(10,4) NULL COMMENT '近20日涨幅%',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_board_code (board_code, ts_code),
    KEY idx_ts (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='板块成分股映射+相关度+龙头标签';

-- =============================================================
-- 暗盘数据（v1.5, 2026-09-15, 需求#20, A股盘后暗线 = 大宗交易+龙虎榜, 口径见 docs/06）
-- =============================================================

-- 大宗交易（逐笔, 盘后披露, 数据源: 东财 datacenter RPT_DATA_BLOCKTRADE）
CREATE TABLE IF NOT EXISTS stock_block_trade (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日期',
    deal_price      DECIMAL(12,3) NULL COMMENT '成交价',
    deal_volume     DECIMAL(20,2) NULL COMMENT '成交量(股)',
    deal_amt        DECIMAL(20,2) NULL COMMENT '成交额(元)',
    premium_ratio   DECIMAL(10,4) NULL COMMENT '折溢价率%(正=溢价,负=折价)',
    close_price     DECIMAL(12,3) NULL COMMENT '当日收盘价(折价计算基准)',
    buyer_name      VARCHAR(128)  NULL COMMENT '买方营业部',
    seller_name     VARCHAR(128)  NULL COMMENT '卖方营业部',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date_price_vol (ts_code, trade_date, deal_price, deal_volume),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='大宗交易(逐笔, 盘后披露)';

-- 龙虎榜（按个股/日期/上榜原因聚合, 数据源: 东财 datacenter RPT_DAILYBILLBOARD_DETAILSNEW）
CREATE TABLE IF NOT EXISTS stock_billboard (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日期',
    reason          VARCHAR(255)  NULL COMMENT '上榜原因(EXPLANATION)',
    buy_amt         DECIMAL(20,2) NULL COMMENT '买入金额(元)',
    sell_amt        DECIMAL(20,2) NULL COMMENT '卖出金额(元)',
    net_amt         DECIMAL(20,2) NULL COMMENT '净买入(元)',
    deal_amt_ratio  DECIMAL(10,4) NULL COMMENT '龙虎榜成交占总成交比%',
    inst_net_amt    DECIMAL(20,2) NULL COMMENT '机构专用席位净买入(元)',
    hot_money_net   DECIMAL(20,2) NULL COMMENT '知名游资席位净买入(元, 预留)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date_reason (ts_code, trade_date, reason(100)),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='龙虎榜(盘后披露)';

-- 暗盘资金派生指标（日度, 由 dark_indicator 按 B6 口径重算, 供选股/展示直接读）
CREATE TABLE IF NOT EXISTS stock_dark_pool_indicator (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    block_amt       DECIMAL(20,2) NULL COMMENT '当日大宗成交额(元)',
    block_discount  DECIMAL(10,4) NULL COMMENT '当日大宗加权折溢价率%',
    block_days5     INT           NULL COMMENT '近5日出现大宗的天数',
    block_discount5 DECIMAL(10,4) NULL COMMENT '近5日大宗加权折溢价率%',
    bb_net_amt      DECIMAL(20,2) NULL COMMENT '当日龙虎榜净买入(元)',
    bb_net_amt5     DECIMAL(20,2) NULL COMMENT '近5日龙虎榜净买入合计(元)',
    dark_score      DECIMAL(10,4) NULL COMMENT '暗盘信号综合分(横截面z-score加权)',
    signal_tag      VARCHAR(32)   NULL COMMENT '信号标签: 机构吸筹/减持出货/游资活跃/无',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_sig (trade_date, signal_tag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='暗盘资金派生指标(日度)';

-- =============================================================
-- akshare 数据源（2026-09-17, P0: 涨停池/强势股池/估值, 口径见 akshare/epic 方案文档）
-- akshare 以 vendored 源码方式接入(config.AKSHARE_PATH), 采集器 app/collector/akshare_collector.py
-- =============================================================

-- 涨停股池（日度, 数据源: 东财 push2ex getTopicZTPool, 经 akshare stock_zt_pool_em）
CREATE TABLE IF NOT EXISTS stock_zt_pool (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    name            VARCHAR(32)  NULL COMMENT '股票名称',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    close_price     DECIMAL(12,3) NULL COMMENT '最新价',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    turnover_rate   DECIMAL(8,3)  NULL COMMENT '换手率%',
    lock_amt        DECIMAL(20,2) NULL COMMENT '封板资金(元)',
    first_lock_time CHAR(6)       NULL COMMENT '首次封板时间 HHMMSS',
    last_lock_time  CHAR(6)       NULL COMMENT '最后封板时间 HHMMSS',
    break_cnt       INT           NULL COMMENT '炸板次数',
    zt_stat         VARCHAR(8)    NULL COMMENT '涨停统计 "连板数/涨停天数", 如 2/2',
    cont_boards     INT           NULL COMMENT '连板数',
    industry        VARCHAR(32)   NULL COMMENT '所属行业(东财)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date_boards (trade_date, cont_boards)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='涨停股池(日度, 东财)';

-- 强势股池（日度, 数据源: 东财 push2ex getTopicQSPool, 经 akshare stock_zt_pool_strong_em）
CREATE TABLE IF NOT EXISTS stock_zt_pool_strong (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    name            VARCHAR(32)  NULL COMMENT '股票名称',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    close_price     DECIMAL(12,3) NULL COMMENT '最新价',
    zt_price        DECIMAL(12,3) NULL COMMENT '涨停价',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    turnover_rate   DECIMAL(8,3)  NULL COMMENT '换手率%',
    speed           DECIMAL(8,3)  NULL COMMENT '涨速%',
    is_new_high     VARCHAR(4)    NULL COMMENT '是否新高(60日)',
    vol_ratio       DECIMAL(10,3) NULL COMMENT '量比',
    zt_stat         VARCHAR(8)    NULL COMMENT '涨停统计 "连板数/涨停天数"',
    reason          VARCHAR(64)   NULL COMMENT '入选理由',
    industry        VARCHAR(32)   NULL COMMENT '所属行业(东财)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='强势股池(日度, 东财)';

-- 估值历史（日度, 数据源: 东财 datacenter RPT_VALUEANALYSIS_DET, 经 akshare stock_value_em）
-- 与 stock_daily_quote 的 pe_ttm/pb 快照列互补: 本表存全历史 + PE静/PEG/市现率/市销率,
-- 供低估值分位类策略计算历史分位用
CREATE TABLE IF NOT EXISTS stock_valuation (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    close_price     DECIMAL(12,3) NULL COMMENT '当日收盘价',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '当日涨跌幅%',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    total_share     DECIMAL(20,2) NULL COMMENT '总股本(股)',
    circ_share      DECIMAL(20,2) NULL COMMENT '流通股本(股)',
    pe_ttm          DECIMAL(12,4) NULL COMMENT 'PE(TTM)',
    pe_static       DECIMAL(12,4) NULL COMMENT 'PE(静)',
    pb              DECIMAL(12,4) NULL COMMENT '市净率',
    peg             DECIMAL(12,4) NULL COMMENT 'PEG值',
    pcf             DECIMAL(14,4) NULL COMMENT '市现率',
    ps              DECIMAL(12,4) NULL COMMENT '市销率',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='估值历史(日度, 东财估值分析)';

-- 估值历史分位(日度, 由 valuation_pctl 按近5年窗口重算, 供 VALUE_PCTL 策略直接读)
CREATE TABLE IF NOT EXISTS stock_valuation_pctl (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    close_price     DECIMAL(12,3) NULL COMMENT '当日收盘价',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '当日涨跌幅%',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    pe_ttm          DECIMAL(12,4) NULL COMMENT 'PE(TTM)',
    pb              DECIMAL(12,4) NULL COMMENT '市净率',
    pb_pctl         DECIMAL(8,5)  NULL COMMENT 'PB近5年分位(0-1)',
    pe_pctl         DECIMAL(8,5)  NULL COMMENT 'PE-TTM近5年分位(0-1)',
    hist_days       INT           NULL COMMENT '参与分位计算的历史交易日数',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_pctl (trade_date, pb_pctl)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='估值历史分位(日度)';

-- 同花顺个股资金流(日度盘后快照, 与新浪 stock_money_flow 交叉验证)
CREATE TABLE IF NOT EXISTS stock_money_flow_ths (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '交易日(采集日)',
    close           DECIMAL(12,3) NULL COMMENT '最新价',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    turnover_rate   DECIMAL(10,4) NULL COMMENT '换手率%',
    inflow          DECIMAL(20,2) NULL COMMENT '流入资金(元)',
    outflow         DECIMAL(20,2) NULL COMMENT '流出资金(元)',
    net_amt         DECIMAL(20,2) NULL COMMENT '净额(元)',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='同花顺个股资金流(日度快照)';

-- 申万一级行业成分(申万宏源研究官网, 全量刷新, 破新浪板块成分2563只上限)
CREATE TABLE IF NOT EXISTS stock_sw_industry_member (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    sw_code         VARCHAR(12)  NOT NULL COMMENT '申万行业代码',
    sw_name         VARCHAR(32)  NOT NULL COMMENT '申万行业名称',
    weight          DECIMAL(10,4) NULL COMMENT '最新权重%',
    in_date         DATE          NULL COMMENT '计入日期',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_sw (ts_code, sw_code),
    KEY idx_sw (sw_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='申万一级行业成分(全量刷新)';

-- =============================================================
-- 2026-09-20 扩容(性价比梯队1): 跌停池/炸板池/昨涨停表现(与涨停池同端点 push2ex, +3请求/日)
-- 业绩报表(东财 datacenter 按报告期批量, ~23页/季 6s, 避免单股5000+请求)
-- 复权因子事件表(腾讯 fqkline qfq 派生, 只存除权变化点, 空间~8k行/年)
-- =============================================================

-- 跌停股池（日度, 数据源: 东财 push2ex getTopicDT_POOL, 经 akshare stock_zt_pool_dtgc_em;
-- 无跌停的交易日接口返回空 DataFrame(无列), 入库 0 行属正常）
CREATE TABLE IF NOT EXISTS stock_zt_pool_dt (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    name            VARCHAR(32)  NULL COMMENT '股票名称',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    close_price     DECIMAL(12,3) NULL COMMENT '最新价',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    turnover_rate   DECIMAL(8,3)  NULL COMMENT '换手率%',
    lock_amt        DECIMAL(20,2) NULL COMMENT '封单资金(元)',
    last_lock_time  CHAR(6)       NULL COMMENT '最后封板时间 HHMMSS',
    break_cnt       INT           NULL COMMENT '炸板次数',
    zt_stat         VARCHAR(8)    NULL COMMENT '涨停统计 "连板数/涨停天数"',
    cont_down       INT           NULL COMMENT '连续跌停天数',
    industry        VARCHAR(32)   NULL COMMENT '所属行业(东财)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='跌停股池(日度, 东财)';

-- 炸板股池（日度, 数据源: 东财 push2ex getTopicZB_POOL, 经 akshare stock_zt_pool_zbgc_em）
CREATE TABLE IF NOT EXISTS stock_zt_pool_zbgc (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    trade_date      DATE         NOT NULL COMMENT '交易日',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    name            VARCHAR(32)  NULL COMMENT '股票名称',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '涨跌幅%',
    close_price     DECIMAL(12,3) NULL COMMENT '最新价',
    zt_price        DECIMAL(12,3) NULL COMMENT '涨停价',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    turnover_rate   DECIMAL(8,3)  NULL COMMENT '换手率%',
    speed           DECIMAL(8,3)  NULL COMMENT '涨速%',
    first_lock_time CHAR(6)       NULL COMMENT '首次封板时间 HHMMSS',
    break_cnt       INT           NULL COMMENT '炸板次数',
    zt_stat         VARCHAR(8)    NULL COMMENT '涨停统计 "涨停天数/连板数"',
    amplitude       DECIMAL(8,3)  NULL COMMENT '振幅%',
    industry        VARCHAR(32)   NULL COMMENT '所属行业(东财)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='炸板股池(日度, 东财)';

-- 昨日涨停股池表现（日度, 数据源: 东财 push2ex getTopicZTPool[prev], 经 akshare stock_zt_pool_previous_em;
-- 记录昨日涨停股今日表现, 供打板情绪/接力因子使用）
CREATE TABLE IF NOT EXISTS stock_zt_pool_prev (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    trade_date      DATE         NOT NULL COMMENT '交易日(今日)',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    name            VARCHAR(32)  NULL COMMENT '股票名称',
    pct_chg         DECIMAL(8,3)  NULL COMMENT '今日涨跌幅%',
    close_price     DECIMAL(12,3) NULL COMMENT '最新价',
    zt_price        DECIMAL(12,3) NULL COMMENT '涨停价',
    amount          DECIMAL(20,2) NULL COMMENT '成交额(元)',
    circ_mv         DECIMAL(20,2) NULL COMMENT '流通市值(元)',
    total_mv        DECIMAL(20,2) NULL COMMENT '总市值(元)',
    turnover_rate   DECIMAL(8,3)  NULL COMMENT '换手率%',
    speed           DECIMAL(8,3)  NULL COMMENT '涨速%',
    amplitude       DECIMAL(8,3)  NULL COMMENT '振幅%',
    prev_lock_time  CHAR(6)       NULL COMMENT '昨日封板时间 HHMMSS',
    prev_boards     INT           NULL COMMENT '昨日连板数',
    zt_stat         VARCHAR(8)    NULL COMMENT '涨停统计 "已连板数/昨日涨停数"',
    industry        VARCHAR(32)   NULL COMMENT '所属行业(东财)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='昨日涨停股池表现(日度, 东财)';

-- 业绩报表（季度, 数据源: 东财 datacenter RPT_LICO_FN_CPD, 经 akshare stock_yjbb_em;
-- 按报告期批量页翻采集(全市场~23页/6s), 避免单股全历史通道的 5000+ 请求;
-- 含北交所等非本地池代码, 全量入库, 策略层自行过滤(与 valuation/zt_pool 同哲学)）
CREATE TABLE IF NOT EXISTS stock_fin_report (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    report_date     DATE         NOT NULL COMMENT '报告期(YYYY-03-31/06-30/09-30/12-31)',
    name            VARCHAR(32)  NULL COMMENT '股票简称',
    eps             DECIMAL(12,4) NULL COMMENT '每股收益(元)',
    revenue         DECIMAL(20,2) NULL COMMENT '营业总收入(元)',
    revenue_yoy     DECIMAL(10,4) NULL COMMENT '营收同比增长%',
    revenue_qoq     DECIMAL(10,4) NULL COMMENT '营收季度环比增长%',
    net_profit      DECIMAL(20,2) NULL COMMENT '净利润(元)',
    net_profit_yoy  DECIMAL(10,4) NULL COMMENT '净利润同比增长%',
    net_profit_qoq  DECIMAL(10,4) NULL COMMENT '净利润季度环比增长%',
    bps             DECIMAL(12,4) NULL COMMENT '每股净资产(元)',
    roe             DECIMAL(10,4) NULL COMMENT '净资产收益率%',
    ocfps           DECIMAL(12,4) NULL COMMENT '每股经营现金流量(元)',
    gross_margin    DECIMAL(10,4) NULL COMMENT '销售毛利率%',
    industry        VARCHAR(32)   NULL COMMENT '所处行业(东财)',
    announce_date   DATE          NULL COMMENT '最新公告日期',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_report (ts_code, report_date),
    KEY idx_report (report_date),
    KEY idx_announce (announce_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='业绩报表(季度, 东财按报告期批量)';

-- 复权因子事件表（只存除权除息变化点, 不存逐日因子以省空间;
-- 口径(2026-09-20 重构, 腾讯 qfq 为加法前复权, gap=raw-qfq=未来累计分红段内恒定):
--   factor_after(t) = [raw(t)/raw(t-1)] / [qfq(t)/qfq(t-1)] 为乘法修正系数, 非事件日=1;
--   跨除权日复权收益 = naive_ret(t) / factor_after(t), 即 raw(t)/raw(t-1)-1 再除以当日 factor_after;
--   factor_before 恒为 1.0(历史字段, 保留兼容);
-- source: seed=一次性回填(腾讯fqkline qfq gap跳变检测), daily=日常增量(THS官方涨跌幅隐含昨收偏差预警后单股验证)）
CREATE TABLE IF NOT EXISTS stock_adjust_factor (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    ex_date         DATE         NOT NULL COMMENT '除权除息日(因子跳变日)',
    factor_before   DECIMAL(20,8) NULL COMMENT '跳变前因子(除权日前一交易日)',
    factor_after    DECIMAL(20,8) NULL COMMENT '跳变后因子(除权日当日起)',
    adj_pct         DECIMAL(10,4) NULL COMMENT '因子跳变幅度% = (after/before-1)*100, 负值=分红/送转除权',
    source          VARCHAR(8)   NOT NULL DEFAULT 'seed' COMMENT '来源: seed/daily',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_exdate (ts_code, ex_date),
    KEY idx_exdate (ex_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='复权因子事件表(除权变化点)';

-- 个股人气榜日度快照(东财 stock_hot_rank_em Top100, 无历史可回填, 逐日累积)
CREATE TABLE IF NOT EXISTS stock_hot_rank (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '快照日期',
    rank_num        INT          NULL COMMENT '东财人气排名(当日)',
    price           DECIMAL(12,4) NULL COMMENT '最新价(快照时点)',
    pct_chg         DECIMAL(10,4) NULL COMMENT '涨跌幅%(快照时点)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date_rank (trade_date, rank_num)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股人气榜日度快照(东财Top100)';

-- 个股融资融券明细(沪深交易所官网, 按日全量, SSE 含 ETF 需过滤仅留个股)
CREATE TABLE IF NOT EXISTS stock_margin_detail (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '股票代码',
    trade_date      DATE         NOT NULL COMMENT '信用交易日期',
    rzye            DECIMAL(20,2) NULL COMMENT '融资余额(元)',
    rzmre           DECIMAL(20,2) NULL COMMENT '融资买入额(元)',
    rqye            DECIMAL(20,2) NULL COMMENT '融券余额(元)',
    rqyl            DECIMAL(20,2) NULL COMMENT '融券余量(股/份)',
    rzrqye          DECIMAL(20,2) NULL COMMENT '融资融券余额(元)',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股融资融券明细(交易所官网日频)';

-- 停复牌信息表（覆盖度审计P1, 修复回测幸存者偏差; 东财数据中心日频快照）
CREATE TABLE IF NOT EXISTS stock_trade_suspend (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '带市场后缀代码, 如 600519.SH',
    trade_date      DATE         NOT NULL COMMENT '信息日期(该日处于停牌)',
    suspend_start   DATE         NULL COMMENT '停牌开始日期',
    suspend_end     DATE         NULL COMMENT '停牌截止时间',
    suspend_term    VARCHAR(64)  NULL COMMENT '停牌期限',
    reason          VARCHAR(255) NULL COMMENT '停牌原因',
    market          VARCHAR(32)  NULL COMMENT '所属市场',
    resume_plan     DATE         NULL COMMENT '预计复牌时间',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='停复牌信息(东财数据中心, 修复回测幸存者偏差)';

-- 业绩预告表（覆盖度审计P2; 东财数据中心 RPT_PUBLIC_OP_NEWPREDICT, 按报告期）
CREATE TABLE IF NOT EXISTS stock_fin_forecast (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)  NOT NULL COMMENT '带市场后缀代码',
    report_date     DATE         NOT NULL COMMENT '报告期',
    announce_date   DATE         NULL COMMENT '公告日期',
    indicator       VARCHAR(64)  NOT NULL COMMENT '预测指标(如归属于上市公司股东的净利润)',
    predict_type    VARCHAR(32)  NULL COMMENT '预告类型(预增/预减/扭亏/首亏...)',
    forecast_low    DECIMAL(24,2) NULL COMMENT '预测数值下限(元)',
    forecast_high   DECIMAL(24,2) NULL COMMENT '预测数值上限(元)',
    change_pct_low  DECIMAL(12,4) NULL COMMENT '业绩变动幅度下限%',
    change_pct_high DECIMAL(12,4) NULL COMMENT '业绩变动幅度上限%',
    prev_value      DECIMAL(24,2) NULL COMMENT '上年同期值(元)',
    content         VARCHAR(1000) NULL COMMENT '预告内容',
    reason          VARCHAR(1000) NULL COMMENT '变动原因',
    is_latest       TINYINT      NULL COMMENT '是否最新一次预告 1/0',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_rp_ann_ind (ts_code, report_date, announce_date, indicator),
    KEY idx_report (report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='业绩预告(东财数据中心 RPT_PUBLIC_OP_NEWPREDICT)';

-- 限售解禁事件表（覆盖度审计P5; 东财数据中心 RPT_LIFT_STAGE, 含未来解禁日历用于抛压排雷）
CREATE TABLE IF NOT EXISTS stock_restricted_release (
    id               BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code          VARCHAR(16)   NOT NULL COMMENT '带市场后缀代码',
    free_date        DATE          NOT NULL COMMENT '解禁时间',
    shares_type      VARCHAR(64)   NOT NULL COMMENT '限售股类型',
    free_shares      DECIMAL(20,4) NULL COMMENT '解禁数量(万股)',
    actual_free_shares DECIMAL(20,4) NULL COMMENT '实际解禁数量(万股)',
    lift_market_cap  DECIMAL(20,4) NULL COMMENT '实际解禁市值(万元)',
    free_ratio       DECIMAL(12,8) NULL COMMENT '占解禁前流通市值比例(%)',
    total_ratio      DECIMAL(12,8) NULL COMMENT '占总市值比例(%)',
    b20_chg          DECIMAL(12,4) NULL COMMENT '解禁前20日涨跌幅(%)',
    a20_chg          DECIMAL(12,4) NULL COMMENT '解禁后20日涨跌幅(%)',
    batch_holder_num INT           NULL COMMENT '解禁股东数',
    etl_insert_time  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time  TIMESTAMP     NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date_type (ts_code, free_date, shares_type),
    KEY idx_free_date (free_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='限售解禁事件(东财 RPT_LIFT_STAGE, 含未来解禁日历)';

-- 个股股权质押比例快照（覆盖度审计P6; 中证登周频, 东财 RPT_CSDC_LIST）
CREATE TABLE IF NOT EXISTS stock_pledge_ratio (
    id            BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code       VARCHAR(16)   NOT NULL COMMENT '带市场后缀代码',
    trade_date    DATE          NOT NULL COMMENT '快照交易日(中证登周频更新)',
    pledge_ratio  DECIMAL(10,4) NULL COMMENT '质押比例(%)',
    pledge_shares DECIMAL(20,4) NULL COMMENT '质押股数(万股)',
    pledge_mv     DECIMAL(20,4) NULL COMMENT '质押市值(万元)',
    deal_num      INT           NULL COMMENT '质押笔数',
    industry      VARCHAR(64)   NULL COMMENT '所属行业',
    etl_insert_time TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP   NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股股权质押比例(中证登周频, 东财 RPT_CSDC_LIST)';

-- 个股商誉明细（覆盖度审计P6; 东财 RPT_GOODWILL_STOCKDETAILS, 按报告期）
CREATE TABLE IF NOT EXISTS stock_goodwill (
    id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)   NOT NULL COMMENT '带市场后缀代码',
    report_date     DATE          NOT NULL COMMENT '报告期',
    notice_date     DATE          NULL COMMENT '公告日期',
    goodwill        DECIMAL(20,2) NULL COMMENT '商誉(元)',
    goodwill_pre    DECIMAL(20,2) NULL COMMENT '上年商誉(元)',
    impairment      DECIMAL(20,2) NULL COMMENT '商誉减值(元)',
    net_asset_ratio DECIMAL(12,6) NULL COMMENT '商誉占净资产比例(%)',
    net_profit      DECIMAL(20,2) NULL COMMENT '净利润(元)',
    etl_insert_time TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP     NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_report (ts_code, report_date),
    KEY idx_report (report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股商誉明细(东财 RPT_GOODWILL_STOCKDETAILS)';

-- 股东户数表（覆盖度审计P4; 东财 RPT_HOLDERNUM_DET, 按报告期, 筹码集中度因子）
CREATE TABLE IF NOT EXISTS stock_holder_num (
    id               BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code          VARCHAR(16)   NOT NULL COMMENT '带市场后缀代码',
    end_date         DATE          NOT NULL COMMENT '股东户数统计截止日(报告期)',
    holder_num       BIGINT        NULL COMMENT '股东户数-本次',
    pre_holder_num   BIGINT        NULL COMMENT '股东户数-上次',
    holder_num_change BIGINT       NULL COMMENT '股东户数-增减',
    holder_num_ratio DECIMAL(12,6) NULL COMMENT '股东户数-增减比例(%)',
    interval_chrate  DECIMAL(12,6) NULL COMMENT '区间涨跌幅(%)',
    avg_hold_num     DECIMAL(20,4) NULL COMMENT '户均持股数量(股)',
    avg_market_cap   DECIMAL(24,2) NULL COMMENT '户均持股市值(元)',
    total_market_cap DECIMAL(24,2) NULL COMMENT '总市值(元)',
    total_a_shares   DECIMAL(24,2) NULL COMMENT '总股本(股)',
    hold_notice_date DATE          NULL COMMENT '公告日期',
    pre_end_date     DATE          NULL COMMENT '上期统计截止日',
    etl_insert_time  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time  TIMESTAMP     NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_end (ts_code, end_date),
    KEY idx_end (end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股东户数(东财 RPT_HOLDERNUM_DET, 按报告期)';

-- 千股千评快照表（覆盖度审计P8; 东财 RPT_DMSK_TS_STOCKNEW, 日频快照, 错过历史无法回填）
CREATE TABLE IF NOT EXISTS stock_comment (
    id               BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code          VARCHAR(16)   NOT NULL COMMENT '带市场后缀代码',
    trade_date       DATE          NOT NULL COMMENT '交易日',
    close_price      DECIMAL(16,4) NULL COMMENT '最新价(元)',
    change_rate      DECIMAL(12,4) NULL COMMENT '涨跌幅(%)',
    turnover_rate    DECIMAL(12,4) NULL COMMENT '换手率(%)',
    pe_dynamic       DECIMAL(16,4) NULL COMMENT '动态市盈率',
    prime_cost       DECIMAL(16,4) NULL COMMENT '主力成本(元)',
    prime_cost_20d   DECIMAL(16,4) NULL COMMENT '20日主力成本(元)',
    prime_cost_60d   DECIMAL(16,4) NULL COMMENT '60日主力成本(元)',
    org_participate  DECIMAL(12,6) NULL COMMENT '机构参与度(%)',
    total_score      DECIMAL(12,4) NULL COMMENT '综合得分',
    rank_no          INT           NULL COMMENT '目前排名',
    rank_up          INT           NULL COMMENT '排名上升',
    focus_index      DECIMAL(16,4) NULL COMMENT '关注指数',
    prime_inflow     DECIMAL(24,2) NULL COMMENT '主力流入(元)',
    etl_insert_time  TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time  TIMESTAMP     NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='千股千评快照(东财 RPT_DMSK_TS_STOCKNEW, 日频)';


-- 个股筹码分布(本地衍生, 换手率衰减法, P9计划外的P7替代方案:
-- 东财 stock_cyq_em 依赖被拦的 push2his 且逐股 5221 请求, 改由库内日线+换手率自算)
CREATE TABLE IF NOT EXISTS stock_cyq (
    id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)   NOT NULL COMMENT '股票代码',
    trade_date      DATE          NOT NULL COMMENT '交易日',
    profit_ratio    DECIMAL(8,4)  NULL COMMENT '获利盘比例(收盘价下方筹码占比 0~1)',
    avg_cost        DECIMAL(12,3) NULL COMMENT '平均成本(筹码加权均价)',
    cost_90_low     DECIMAL(12,3) NULL COMMENT '90%筹码成本下界(5%分位价)',
    cost_90_high    DECIMAL(12,3) NULL COMMENT '90%筹码成本上界(95%分位价)',
    conc_90         DECIMAL(8,4)  NULL COMMENT '90%成本集中度((上界-下界)/(上界+下界), 越小越集中)',
    etl_insert_time TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP     NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date (ts_code, trade_date),
    KEY idx_date_profit (trade_date, profit_ratio)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股筹码分布日度快照(本地衍生,换手率衰减法)';

-- 技术形态榜单(本地衍生, P9: THS 榜单接口未内置, 连涨/创新高由库内日线直接计算)
CREATE TABLE IF NOT EXISTS stock_rank_deriv (
    id              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code         VARCHAR(16)   NOT NULL COMMENT '股票代码',
    trade_date      DATE          NOT NULL COMMENT '交易日',
    rank_type       VARCHAR(20)   NOT NULL COMMENT '榜单类型(NEW_HIGH_60创60日新高/NEW_LOW_60创60日新低/CONT_UP连涨榜/CONT_DOWN连跌榜)',
    rank_num        INT           NULL COMMENT '名次(按 metric_value 排序)',
    metric_value    DECIMAL(16,4) NULL COMMENT '指标值(新高/新低=当日涨跌幅%, 连涨/连跌=天数)',
    etl_insert_time TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP     NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_date_type (ts_code, trade_date, rank_type),
    KEY idx_date_type_rank (trade_date, rank_type, rank_num)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='技术形态榜单日度快照(本地衍生)';

-- 策略配置表(2026-10-19, Epic: 策略参数/启停 DB 化, 替代代码写死;
-- 合并优先级: 类 default_params < 本表 params_json < 运行时传入)
CREATE TABLE IF NOT EXISTS stock_strategy_config (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    strategy_code   VARCHAR(32)  NOT NULL COMMENT '策略代码(唯一)',
    strategy_name   VARCHAR(64)  NOT NULL COMMENT '策略名称',
    enabled         TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用(1启用/0停用,run_pick_all跳过停用)',
    params_json     TEXT         NULL COMMENT '参数覆盖(JSON)',
    remark          VARCHAR(255) NULL COMMENT '备注',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间(应用层显式写,MySQL5.5只允许一个自动TIMESTAMP列)',
    PRIMARY KEY (id),
    UNIQUE KEY uk_strategy_code (strategy_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='策略配置表(参数/启停DB化)';

-- 董监高及相关人员持股变动(2026-10-19, Epic#3 akshare覆盖补缺;
-- 东财 RPT_EXECUTIVE_HOLD_DETAILS, uk=row_hash 全字段MD5幂等, share_change_collector.py)
CREATE TABLE IF NOT EXISTS stock_share_change (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
    ts_code VARCHAR(16) NOT NULL COMMENT '带市场后缀代码',
    sec_name VARCHAR(32) NULL COMMENT '证券名称',
    change_date DATE NOT NULL COMMENT '变动日期',
    person_name VARCHAR(64) NULL COMMENT '变动人',
    change_shares DECIMAL(20,2) NULL COMMENT '变动股数(正=增持,负=减持)',
    avg_price DECIMAL(12,3) NULL COMMENT '成交均价(元)',
    change_amount DECIMAL(24,2) NULL COMMENT '变动金额(元)',
    change_reason VARCHAR(128) NULL COMMENT '变动原因',
    change_ratio DECIMAL(12,6) NULL COMMENT '变动比例%',
    after_holdnum DECIMAL(20,2) NULL COMMENT '变动后持股数',
    hold_type VARCHAR(32) NULL COMMENT '持股种类',
    dse_person VARCHAR(64) NULL COMMENT '董监高人员姓名',
    position_name VARCHAR(64) NULL COMMENT '职务',
    person_relation VARCHAR(32) NULL COMMENT '变动人与董监高的关系',
    row_hash CHAR(32) NOT NULL COMMENT '全字段MD5(幂等键)',
    etl_insert_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP NULL DEFAULT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_hash (row_hash),
    KEY idx_code_date (ts_code, change_date),
    KEY idx_date (change_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='董监高及相关人员持股变动(东财 RPT_EXECUTIVE_HOLD_DETAILS)';
