# -*- coding: utf-8 -*-
"""策略基类与注册表"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd


@dataclass
class PickItem:
    ts_code: str
    name: str
    trade_date: date
    close_price: Optional[float]
    pct_chg: Optional[float]
    score: float
    hit_reason: str


class BaseStrategy(ABC):
    code: str = ""
    name: str = ""
    default_params: dict = {}

    @abstractmethod
    def select(self, quotes: pd.DataFrame, basics: pd.DataFrame, params: dict) -> list[PickItem]:
        """
        quotes: stock_daily_quote 全量 DataFrame（按 ts_code, trade_date 排序）
        basics: stock_basic 全量 DataFrame
        返回命中明细列表
        """
        raise NotImplementedError

    def merged_params(self, params: dict | None) -> dict:
        p = dict(self.default_params)
        if params:
            p.update(params)
        return p


# 选股范围限定: 只选大A(沪深主板+创业板), 不考虑科创板(688/689)
EXCLUDED_CODE_PREFIXES = ("688", "689")


def filter_universe(quotes: pd.DataFrame, basics: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """统一剔除科创板(688/689), 所有策略共享的股票池过滤"""
    mask_q = ~quotes["ts_code"].str.startswith(EXCLUDED_CODE_PREFIXES)
    mask_b = ~basics["ts_code"].str.startswith(EXCLUDED_CODE_PREFIXES)
    return quotes[mask_q], basics[mask_b]


STRATEGY_REGISTRY: dict[str, BaseStrategy] = {}


def register(strategy: BaseStrategy) -> None:
    STRATEGY_REGISTRY[strategy.code] = strategy


def get_strategy(code: str) -> BaseStrategy:
    if code not in STRATEGY_REGISTRY:
        raise KeyError(f"策略不存在: {code}, 可用: {list(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[code]


def list_strategies() -> list[dict]:
    return [
        {"code": s.code, "name": s.name, "default_params": s.default_params}
        for s in STRATEGY_REGISTRY.values()
    ]
