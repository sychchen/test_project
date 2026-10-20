# -*- coding: utf-8 -*-
"""选股系统全局配置"""
import os

# 公司代理对东方财富接口间歇性拒绝连接，直连稳定，故行情请求一律绕过系统代理
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# MySQL 连接
DB_HOST = "172.16.3.16"
DB_PORT = 30306
DB_NAME = "test_project"
DB_USER = "admin"
DB_PASSWORD = "admin"

DB_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
)

# 采集参数
QUOTE_DAYS = 60          # 日常增量采集窗口（自然日反推）
QUOTE_DAYS_INIT = 120    # 首次采集窗口
REQUEST_INTERVAL = 0.2   # 单票请求间隔（秒），防频控
RETRY_TIMES = 3          # 单票失败重试次数

# akshare vendored 源码路径（不装 pip 包, 目标站改版可直接改源码修复）
AKSHARE_PATH = r"D:\kiro-worspace\test_project\akshare"

# 选股/回测的数据加载窗口（自然日）。stock_daily_quote 已向前扩展到约 4 年，
# 全表加载会随历史线性变慢；策略最长回看约 60 个交易日，取 400 自然日
# （≈270 个交易日）留足余量，同时让选股耗时回到与 246 天时相当的水平。
UNIVERSE_LOOKBACK_DAYS = 400

# API 服务
API_HOST = "127.0.0.1"
API_PORT = 8600
