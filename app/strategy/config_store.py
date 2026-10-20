# -*- coding: utf-8 -*-
"""策略配置 DB 化（stock_strategy_config）

口径：类内 default_params 仍是兜底默认值；库表存「覆盖参数 + 启停」。
合并优先级：类默认值 default_params < 库表 params_json < 运行时传入 params。
enabled=0 的策略在 run_pick_all 中跳过（单策略手动执行不拦截，便于调试）。

懒初始化：首次读写时 ensure_table + seed（INSERT IGNORE 全量注册策略，
seed 的 params_json 等于类默认值，故 seed 本身不改变任何策略行为）。
"""
import json
import threading

from sqlalchemy import text

from app.db import engine
from app.strategy.base import STRATEGY_REGISTRY

_DDL = """
CREATE TABLE IF NOT EXISTS stock_strategy_config (
    id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    strategy_code   VARCHAR(32)  NOT NULL COMMENT '策略代码(唯一)',
    strategy_name   VARCHAR(64)  NOT NULL COMMENT '策略名称',
    enabled         TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用(1启用/0停用,run_pick_all跳过停用)',
    params_json     TEXT         NULL COMMENT '参数覆盖(JSON),合并优先级: 类默认值<本配置<运行时传入',
    remark          VARCHAR(255) NULL COMMENT '备注',
    etl_insert_time TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    etl_update_time TIMESTAMP    NULL DEFAULT NULL COMMENT '更新时间(应用层显式写,MySQL5.5只允许一个自动TIMESTAMP列)',
    PRIMARY KEY (id),
    UNIQUE KEY uk_strategy_code (strategy_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='策略配置表(参数/启停DB化,替代代码写死)'
"""

_init_lock = threading.Lock()
_initialized = False


def ensure_seeded() -> None:
    """建表 + 播种（幂等）。seed 值=类默认值，不改变行为。"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with engine.begin() as conn:
            conn.execute(text(_DDL))
            for s in STRATEGY_REGISTRY.values():
                conn.execute(text("""
                    INSERT IGNORE INTO stock_strategy_config
                        (strategy_code, strategy_name, enabled, params_json, remark)
                    VALUES (:c, :n, 1, :p, 'seed:类默认值')
                """), {"c": s.code, "n": s.name,
                       "p": json.dumps(s.default_params or {}, ensure_ascii=False)})
        _initialized = True


def load_all() -> dict:
    """读全部配置 {code: {enabled, params, name}}；表异常时回退空 dict（=纯代码默认值）。"""
    try:
        ensure_seeded()
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT strategy_code, strategy_name, enabled, params_json"
                " FROM stock_strategy_config")).mappings().all()
        out = {}
        for r in rows:
            try:
                p = json.loads(r["params_json"]) if r["params_json"] else {}
            except (TypeError, ValueError):
                p = {}
            out[r["strategy_code"]] = {
                "name": r["strategy_name"], "enabled": int(r["enabled"]),
                "params": p}
        return out
    except Exception:
        return {}  # 表不存在/库不可用等场景：回退代码默认值，不阻塞选股主流程


def db_params(strategy_code: str) -> dict:
    """某策略的库表覆盖参数（无则 {}）"""
    return load_all().get(strategy_code.upper(), {}).get("params", {})


def effective_params(strategy_code: str, runtime_params: dict | None) -> dict:
    """库表覆盖 + 运行时传入（运行时优先）。返回传给 strategy.select 的 params，
    策略内部再 merged_params 叠加类默认值，形成 默认<库表<运行时 三级。"""
    merged = db_params(strategy_code)
    if runtime_params:
        merged.update(runtime_params)
    return merged


def is_enabled(strategy_code: str) -> bool:
    return load_all().get(strategy_code.upper(), {}).get("enabled", 1) == 1


def list_configs() -> list[dict]:
    """注册表 LEFT JOIN 库表：返回每个策略的 默认值/库表覆盖/生效值/启停。"""
    db = load_all()
    out = []
    for s in STRATEGY_REGISTRY.values():
        row = db.get(s.code, {})
        over = row.get("params", {})
        eff = dict(s.default_params or {})
        eff.update(over)
        out.append({
            "code": s.code, "name": s.name,
            "enabled": row.get("enabled", 1),
            "default_params": s.default_params or {},
            "db_params": over,
            "effective_params": eff,
        })
    return out


def update_config(strategy_code: str, params: dict | None = None,
                  enabled: int | None = None, remark: str | None = None) -> dict:
    """更新库表配置（UPSERT），返回该策略最新配置行。"""
    code = strategy_code.upper()
    if code not in STRATEGY_REGISTRY:
        raise KeyError(f"策略不存在: {code}")
    ensure_seeded()
    sets, args = [], {"c": code}
    if params is not None:
        sets.append("params_json = :p")
        args["p"] = json.dumps(params, ensure_ascii=False)
    if enabled is not None:
        sets.append("enabled = :e")
        args["e"] = 1 if enabled else 0
    if remark is not None:
        sets.append("remark = :r")
        args["r"] = remark[:255]
    if not sets:
        raise ValueError("无可更新字段（params/enabled/remark 至少传一个）")
    sets.append("etl_update_time = NOW()")
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE stock_strategy_config SET {', '.join(sets)}"
            " WHERE strategy_code = :c"), args)
    for c in list_configs():
        if c["code"] == code:
            return c
    raise RuntimeError(f"更新后未读到配置: {code}")
