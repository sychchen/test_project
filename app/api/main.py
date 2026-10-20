# -*- coding: utf-8 -*-
"""FastAPI 入口: 选股系统 API + 静态页面托管"""
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from app import config
from app.db import check_connection, engine
from app.collector.basic_collector import (
    sync_stock_basic, sync_list_dates, snapshot_valuation)
from app.collector.quote_collector import sync_daily_quotes
from app.collector.minute_collector import sync_minute_quotes
from app.collector.tag_collector import sync_tags
from app.collector.em_concept_collector import sync_em_concepts
from app.collector.index_collector import sync_index_daily
from app.collector.moneyflow_collector import sync_money_flow
from app.collector.indicator_collector import refresh as refresh_indicators
from app.collector.board_collector import sync_board_basic
from app.collector.board_synth import synth_board_daily, synth_board_minutes
from app.collector.darkpool_collector import (
    sync_block_trade, sync_billboard, refresh_dark_indicator)
from app.pick_service import run_pick, pick_asof, pick_live
from app.backtest import run_backtest
from app.strategy.base import STRATEGY_REGISTRY, list_strategies
from app.strategy import config_store
import app.pick_service  # noqa: F401  确保策略注册

app = FastAPI(title="选股系统", version="1.0.0")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


@app.on_event("startup")
def _startup_seed_strategy_config():
    """策略配置表建表+播种（seed 值=类默认值，不改变行为；失败不阻塞服务）"""
    try:
        config_store.ensure_seeded()
    except Exception:
        pass


class PickRequest(BaseModel):
    params: Optional[dict] = None


@app.get("/api/health")
def health():
    return {"status": "ok", "db_connected": check_connection(),
            "strategies": len(STRATEGY_REGISTRY)}


@app.post("/api/collect/basic")
def collect_basic():
    try:
        return sync_stock_basic()
    except Exception as e:
        raise HTTPException(500, f"基础信息采集失败: {e}")


@app.post("/api/collect/quotes")
def collect_quotes(days: int = Query(2, ge=1, le=500),
                   limit: Optional[int] = Query(None, ge=1),
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None):
    try:
        return sync_daily_quotes(days=days, limit=limit,
                                 start_date=start_date, end_date=end_date)
    except Exception as e:
        raise HTTPException(500, f"行情采集失败: {e}")


@app.post("/api/collect/minutes")
def collect_minutes(scale: int = Query(5, ge=5, le=60),
                    days: int = Query(1, ge=1, le=5),
                    limit: Optional[int] = Query(None, ge=1)):
    try:
        return sync_minute_quotes(scale=scale, days=days, limit=limit)
    except Exception as e:
        raise HTTPException(500, f"分时采集失败: {e}")


@app.post("/api/collect/tags")
def collect_tags():
    try:
        return sync_tags()
    except Exception as e:
        raise HTTPException(500, f"标签采集失败: {e}")


@app.post("/api/collect/emconcepts")
def collect_emconcepts(limit_boards: Optional[int] = Query(None, ge=1)):
    try:
        return sync_em_concepts(limit_boards=limit_boards)
    except Exception as e:
        raise HTTPException(500, f"东财概念采集失败: {e}")


@app.post("/api/collect/listdate")
def collect_listdate(limit: Optional[int] = Query(None, ge=1)):
    try:
        return sync_list_dates(limit=limit)
    except Exception as e:
        raise HTTPException(500, f"上市日期采集失败: {e}")


@app.post("/api/collect/valsnap")
def collect_valsnap():
    try:
        return snapshot_valuation()
    except Exception as e:
        raise HTTPException(500, f"估值快照写入失败: {e}")


@app.post("/api/collect/index")
def collect_index(days: int = Query(40, ge=25, le=300)):
    try:
        return sync_index_daily(days=days)
    except Exception as e:
        raise HTTPException(500, f"指数采集失败: {e}")


@app.post("/api/collect/moneyflow")
def collect_moneyflow(limit: Optional[int] = Query(None, ge=1)):
    try:
        return sync_money_flow(limit=limit)
    except Exception as e:
        raise HTTPException(500, f"资金流向采集失败: {e}")


@app.post("/api/collect/indicators")
def collect_indicators(days: Optional[int] = Query(None, ge=1, le=2000)):
    """重算日频衍生指标: 连涨天数/连涨涨幅/量比/换手率(days 为空=全表)"""
    try:
        return refresh_indicators(days=days)
    except Exception as e:
        raise HTTPException(500, f"衍生指标重算失败: {e}")


@app.post("/api/collect/board")
def collect_board(skip_members: bool = False,
                  limit_boards: Optional[int] = Query(None, ge=1)):
    """板块基础采集: 新浪行业快照+成分映射+人气(v1.5)"""
    try:
        return sync_board_basic(skip_members=skip_members,
                                limit_boards=limit_boards)
    except Exception as e:
        raise HTTPException(500, f"板块基础采集失败: {e}")


@app.post("/api/collect/boardsynth")
def collect_boardsynth():
    """板块日线合成: 流通市值加权全量覆盖 + 成员标签(v1.5)"""
    try:
        return synth_board_daily()
    except Exception as e:
        raise HTTPException(500, f"板块日线合成失败: {e}")


@app.post("/api/collect/boardmin")
def collect_boardmin(all_days: bool = Query(True)):
    """板块分时合成: 默认合成库内最近30个有分时的交易日(v1.5)"""
    try:
        return synth_board_minutes(all_days=all_days)
    except Exception as e:
        raise HTTPException(500, f"板块分时合成失败: {e}")


@app.post("/api/collect/darkpool")
def collect_darkpool(days: int = Query(7, ge=1, le=120)):
    """暗盘采集: 大宗交易+龙虎榜+席位(v1.5, 报表接口低频)"""
    try:
        r1 = sync_block_trade(days=days)
        r2 = sync_billboard(days=days)
        return {"block_trade": r1, "billboard": r2}
    except Exception as e:
        raise HTTPException(500, f"暗盘采集失败: {e}")


@app.post("/api/collect/darkind")
def collect_darkind():
    """暗盘派生指标重算: dark_score/信号(v1.5)"""
    try:
        return refresh_dark_indicator()
    except Exception as e:
        raise HTTPException(500, f"暗盘指标重算失败: {e}")


@app.get("/api/strategies")
def strategies():
    """策略清单：default_params 返回「生效值」（类默认值 合并 库表 stock_strategy_config 覆盖），
    另带 enabled（run_pick_all 是否执行）与 db_params（库表覆盖原文），前端原字段不变。"""
    out = []
    for c in config_store.list_configs():
        out.append({
            "code": c["code"], "name": c["name"],
            "default_params": c["effective_params"],   # 生效口径（含库表覆盖）
            "pure_default_params": c["default_params"],
            "db_params": c["db_params"],
            "enabled": c["enabled"],
        })
    return {"strategies": out}


class StrategyConfigRequest(BaseModel):
    params: Optional[dict] = None    # 库表覆盖参数（整体替换 params_json）
    enabled: Optional[int] = None    # 1 启用 / 0 停用（run_pick_all 跳过）
    remark: Optional[str] = None


@app.get("/api/strategy_config")
def strategy_config_list():
    """策略配置表全量：默认值 / 库表覆盖 / 生效值 / 启停"""
    return {"configs": config_store.list_configs()}


@app.post("/api/strategy_config/{strategy_code}")
def strategy_config_update(strategy_code: str, req: StrategyConfigRequest):
    """改库表策略配置；改完即生效（下次选股/回测按新参数，无需改代码重启）"""
    try:
        return {"config": config_store.update_config(
            strategy_code, params=req.params, enabled=req.enabled,
            remark=req.remark)}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"策略配置更新失败: {e}")


@app.post("/api/pick/{strategy_code}")
def pick(strategy_code: str, req: PickRequest | None = None):
    try:
        return run_pick(strategy_code.upper(), req.params if req else None)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"选股执行失败: {e}")


class AsofRequest(BaseModel):
    date: str
    params: dict | None = None


@app.get("/api/pick/tradedates")
def pick_tradedates(limit: int = Query(400, ge=1, le=1000)):
    """库内全部交易日(倒序), 供「指定日期选股」下拉。"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT trade_date FROM stock_daily_quote "
            "ORDER BY trade_date DESC LIMIT :l"), {"l": limit}).fetchall()
    return {"dates": [str(r[0]) for r in rows]}


@app.post("/api/pick/asof/{strategy_code}")
def pick_asof_api(strategy_code: str, req: AsofRequest):
    """指定基准日选股: 行情截断至该日重放策略, 只返回结果不落库。"""
    try:
        return pick_asof(strategy_code.upper(), req.date, req.params)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"指定日选股失败: {e}")


@app.post("/api/pick/live/{strategy_code}")
def pick_live_api(strategy_code: str, req: PickRequest | None = None):
    """盘中实时选股: 实时快照->内存临时 bar->直跑策略, 全程不落库。

    近似口径: 快照类策略(估值分位/两融/资金流/涨停池)的快照表为上一盘后数据;
    盘中 amount/turnover 为盘中累计值; 停牌股沿用库内最后一根 bar。
    """
    try:
        return pick_live(strategy_code.upper(), req.params if req else None)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"盘中选股失败: {e}")


@app.get("/api/pick/batches")
def batches(limit: int = Query(20, ge=1, le=200)):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, batch_no, strategy_code, strategy_name, trade_date,
                   params, hit_count, status, message, etl_insert_time
            FROM stock_pick_batch ORDER BY id DESC LIMIT :lim
        """), {"lim": limit}).mappings().all()
    return {"batches": [dict(r) for r in rows]}


@app.get("/api/pick/results")
def results(batch_no: Optional[str] = None,
            strategy_code: Optional[str] = None,
            trade_date: Optional[str] = None,
            limit: int = Query(200, ge=1, le=1000)):
    sql = """
        SELECT r.batch_no, r.strategy_code, r.ts_code, r.name, r.trade_date,
               r.close_price, r.pct_chg, r.score, r.hit_reason, b.strategy_name,
               sb.industry, sb.concepts, sb.index_tags,
               qd.cont_days, qd.cont_pct, qd.vol_ratio, qd.turnover_rate
        FROM stock_pick_result r
        JOIN stock_pick_batch b ON b.id = r.batch_id
        LEFT JOIN stock_basic sb ON sb.ts_code = r.ts_code
        LEFT JOIN stock_daily_quote qd
               ON qd.ts_code = r.ts_code AND qd.trade_date = r.trade_date
        WHERE 1=1
    """
    # SQL LIMIT 在去重前生效，同股跨策略最多 ~7 行会占限额 → 10 倍缓冲，Python 侧去重后再截断到 limit
    args: dict = {"lim": limit * 10}
    if batch_no:
        sql += " AND r.batch_no = :batch_no"
        args["batch_no"] = batch_no
    if strategy_code:
        sql += " AND r.strategy_code = :sc"
        args["sc"] = strategy_code.upper()
    if trade_date:
        sql += " AND r.trade_date = :td"
        args["td"] = trade_date
    sql += " ORDER BY r.score DESC LIMIT :lim"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), args).mappings().all()
        # 消重：同股同日被多个策略命中会产生多行，而「命中策略」列本就是聚合展示，
        # 表格看起来是完全重复的行。按 (ts_code, trade_date) 去重，
        # 结果按 score DESC 有序，首个出现的即该股的最高分策略行
        # （悬浮卡的 得分/批次/命中原因 随之取最高分策略），其余列为股票级数据不受影响。
        # MySQL5.5 关联子查询/派生表会全扫，故在 Python 侧做。
        best: dict = {}
        order: list = []
        for r in rows:
            key = (str(r["ts_code"]), str(r["trade_date"]))
            if key not in best:
                order.append(key)
                best[key] = r
        rows = [best[k] for k in order][:limit]
        # 命中策略集合（同股同日跨批次被哪些策略命中）：MySQL5.5 无派生表索引，
        # 关联子查询/派生表 JOIN 都会退化为嵌套循环全扫，改为单独查询后 Python 侧合并，口径一致
        sg_rows = conn.execute(text("""
            SELECT r2.ts_code, r2.trade_date,
                   GROUP_CONCAT(DISTINCT b2.strategy_name
                        ORDER BY b2.strategy_name SEPARATOR '、') AS strategies
            FROM stock_pick_result r2
            JOIN stock_pick_batch b2 ON b2.id = r2.batch_id
            GROUP BY r2.ts_code, r2.trade_date
        """)).mappings().all()
    smap = {(str(x["ts_code"]), str(x["trade_date"])): x["strategies"] for x in sg_rows}
    out = []
    for r in rows:
        d = dict(r)
        d["strategies"] = smap.get((str(d["ts_code"]), str(d["trade_date"])))
        out.append(d)
    return {"count": len(out), "results": out}


@app.get("/api/backtest/{strategy_code}")
def backtest(strategy_code: str, days_ago: int = Query(5, ge=1, le=29),
             stop_pct: float = Query(3.0, ge=0.5, le=20),
             breakeven_trigger: float = Query(5.0, ge=1, le=50),
             max_hold_days: int = Query(3, ge=1, le=29)):
    try:
        return run_backtest(strategy_code.upper(), days_ago,
                            stop_pct=stop_pct, breakeven_trigger=breakeven_trigger,
                            max_hold_days=max_hold_days)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"回测执行失败: {e}")


@app.get("/api/pick/dates")
def pick_dates(limit: int = Query(250, ge=1, le=1000)):
    """选股结果可回看的基准日列表（降序，附当日命中只数/策略数）"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT trade_date, COUNT(*) AS n, COUNT(DISTINCT strategy_code) AS sc "
            "FROM stock_pick_result "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT :lim"),
            {"lim": limit}).fetchall()
    return {"count": len(rows),
            "dates": [{"trade_date": str(r[0]), "count": int(r[1]),
                       "strategies": int(r[2])} for r in rows]}


@app.get("/api/pick/intersection")
def intersection(min_strategies: int = Query(2, ge=2, le=10),
                 trade_date: Optional[str] = None):
    """多策略交集: 同一基准日下，被 >=min_strategies 个不同策略同时命中的股票

    trade_date 缺省取 stock_pick_result 最新基准日（前端日期筛选默认最新一天）。
    额外带回 连涨天数(cont_days, 取自 stock_daily_quote，按 ts_code + trade_date 唯一键关联，
    trade_date 为常量，故为单行等值 LEFT JOIN，MySQL 5.5 下无性能风险)。
    """
    with engine.connect() as conn:
        td = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_pick_result")).scalar()
        if not td:
            return {"count": 0, "trade_date": None, "results": []}
        rows = conn.execute(text("""
            SELECT r.ts_code, MAX(r.name) AS name,
                   MAX(r.trade_date) AS trade_date,
                   COUNT(DISTINCT r.strategy_code) AS strat_cnt,
                   GROUP_CONCAT(DISTINCT b.strategy_name ORDER BY b.strategy_name SEPARATOR '、') AS strategies,
                   MAX(r.close_price) AS close_price, MAX(r.pct_chg) AS pct_chg,
                   ROUND(SUM(r.score), 3) AS total_score,
                   MAX(sb.industry) AS industry, MAX(sb.concepts) AS concepts,
                   MAX(sb.index_tags) AS index_tags,
                   MAX(dq.cont_days) AS cont_days,
                   MAX(dq.cont_pct) AS cont_pct,
                   MAX(dq.vol_ratio) AS vol_ratio,
                   MAX(dq.turnover_rate) AS turnover_rate
            FROM stock_pick_result r
            JOIN stock_pick_batch b ON b.id = r.batch_id
            LEFT JOIN stock_basic sb ON sb.ts_code = r.ts_code
            LEFT JOIN stock_daily_quote dq
                   ON dq.ts_code = r.ts_code AND dq.trade_date = r.trade_date
            WHERE r.trade_date = :td
            GROUP BY r.ts_code
            HAVING strat_cnt >= :min_cnt
            ORDER BY strat_cnt DESC, total_score DESC
            LIMIT 200
        """), {"min_cnt": min_strategies, "td": td}).mappings().all()
    out = [dict(r) for r in rows]
    for r in out:
        if r.get("trade_date") is not None:
            r["trade_date"] = str(r["trade_date"])
    return {"count": len(out), "trade_date": str(td), "results": out}


@app.get("/api/quote/{ts_code}")
def quote_history(ts_code: str):
    """个股近期日K（页面画 K 线用）"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT trade_date, open, high, low, close, vol, amount, pct_chg,
                   ma5, ma10, ma20, cont_days, cont_pct, vol_ratio, turnover_rate
            FROM stock_daily_quote WHERE ts_code = :tc ORDER BY trade_date
        """), {"tc": ts_code.upper()}).mappings().all()
    if not rows:
        raise HTTPException(404, f"无行情数据: {ts_code}")
    return {"ts_code": ts_code.upper(), "quotes": [dict(r) for r in rows]}


@app.get("/api/daily/latest")
def daily_latest(q: Optional[str] = None,
                 trade_date: Optional[str] = None,
                 limit: int = Query(50, ge=1, le=200)):
    """按代码/名称查最新交易日的日线行情（选股结果筛选框的兜底数据源）。

    与 /api/pick/results 返回同构字段（strategies/score/batch_no 为 None），
    供前端在 stock_pick_result 查不到匹配时降级展示 stock_daily_quote 最近一日数据。
    trade_date 缺省取 MAX(trade_date)。q 支持 ts_code / symbol / name 模糊匹配。
    """
    with engine.connect() as conn:
        td = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
        if not td:
            return {"count": 0, "trade_date": None, "results": []}
        sql = """
            SELECT q.ts_code, b.name, q.trade_date, q.close AS close_price,
                   q.pct_chg, q.vol, q.amount,
                   q.cont_days, q.cont_pct, q.vol_ratio, q.turnover_rate,
                   b.industry, b.concepts, b.index_tags
            FROM stock_daily_quote q
            LEFT JOIN stock_basic b ON b.ts_code = q.ts_code
            WHERE q.trade_date = :td
        """
        args = {"td": td, "lim": limit}
        if q:
            # 新浪名称含全角/半角空格(如 "万 科Ａ")，故对名称做空格不敏感匹配
            sql += (" AND (q.ts_code LIKE :lk OR b.symbol LIKE :lk"
                    " OR REPLACE(REPLACE(b.name,' ',''),'　','') LIKE :lkn)")
            args["lk"] = f"%{q}%"
            args["lkn"] = f"%{q.replace(' ', '').replace('　', '')}%"
        sql += " ORDER BY q.ts_code LIMIT :lim"
        rows = conn.execute(text(sql), args).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["strategies"] = None
        d["score"] = None
        d["batch_no"] = None
        d["strategy_code"] = None
        d["hit_reason"] = None
        d["is_fallback"] = True
        out.append(d)
    return {"count": len(out), "trade_date": str(td), "results": out}


@app.get("/api/minute/{ts_code}")
def minute_history(ts_code: str, date: Optional[str] = None,
                   scale: int = Query(5, ge=5, le=60)):
    """个股分时数据。date 默认库内最新交易日。"""
    with engine.connect() as conn:
        if not date:
            date = conn.execute(text(
                "SELECT MAX(DATE(bar_time)) FROM stock_minute_quote WHERE ts_code=:tc"
            ), {"tc": ts_code.upper()}).scalar()
            if not date:
                raise HTTPException(404, f"无分时数据: {ts_code}")
        rows = conn.execute(text("""
            SELECT bar_time, open, high, low, close, vol
            FROM stock_minute_quote
            WHERE ts_code=:tc AND scale=:sc AND DATE(bar_time)=:d
            ORDER BY bar_time
        """), {"tc": ts_code.upper(), "sc": scale, "d": date}).mappings().all()
    return {"ts_code": ts_code.upper(), "date": str(date), "scale": scale,
            "bars": [dict(r) for r in rows]}


@app.get("/api/index_tags")
def index_tags():
    """可选「指数成分」筛选项: stock_basic.index_tags 为逗号分隔串, 此处拆分去重"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT index_tags FROM stock_basic "
            "WHERE is_active=1 AND index_tags IS NOT NULL AND index_tags<>''")).fetchall()
    tags = set()
    for (s,) in rows:
        for t in str(s).replace('，', ',').split(','):
            t = t.strip()
            if t:
                tags.add(t)
    order = ['上证50', '沪深300', '中证500', '中证1000', '国证2000', '科创50',
             '创业板指', '北证50', '深证成指', '上证指数']
    out = [t for t in order if t in tags] + sorted(t for t in tags if t not in order)
    return {"tags": out}


@app.get("/api/search")
def search_stock(q: str = Query(..., min_length=1),
                limit: int = Query(10, ge=1, le=50)):
    """按代码/名称模糊搜索股票（含标签）
    名称做空格不敏感匹配(新浪存在 "万 科Ａ" 这类全角/半角空格名称)"""
    like = f"%{q}%"
    like_n = f"%{q.replace(' ', '').replace('　', '')}%"
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ts_code, symbol, name, industry, concepts, index_tags
            FROM stock_basic
            WHERE is_active=1 AND (ts_code LIKE :lk OR symbol LIKE :lk
                  OR REPLACE(REPLACE(name,' ',''),'　','') LIKE :lkn)
            ORDER BY (symbol LIKE :pfx) DESC, ts_code LIMIT :lim
        """), {"lk": like, "lkn": like_n, "pfx": f"{q}%", "lim": limit}).mappings().all()
    return {"results": [dict(r) for r in rows]}


@app.get("/api/stock/{ts_code}")
def stock_detail(ts_code: str):
    """个股基础信息+标签+所属板块(新浪行业, 供前端报价带展示并跳转板块行情)"""
    tc = ts_code.upper()
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT ts_code, symbol, name, market, pe_ttm, pb, total_mv, circ_mv,
                   industry, concepts, index_tags
            FROM stock_basic WHERE ts_code=:tc
        """), {"tc": tc}).mappings().first()
        if not r:
            raise HTTPException(404, f"股票不存在: {ts_code}")
        # 所属板块: stock_board_member 只有 board_code, 名称取该板块最新快照
        bds = conn.execute(text("""
            SELECT m.board_code,
                   (SELECT b.board_name FROM stock_board_basic b
                     WHERE b.board_code=m.board_code
                     ORDER BY b.trade_date DESC LIMIT 1) AS board_name
            FROM stock_board_member m WHERE m.ts_code=:tc
        """), {"tc": tc}).mappings().all()
    d = dict(r)
    d["boards"] = [dict(x) for x in bds]
    return d


# ---------------------------------------------------------------- 板块行情(v1.5)

@app.get("/api/boards")
def board_list(trade_date: Optional[str] = None):
    """板块列表: 最新交易日快照(成分股数/市值/涨跌家数/人气/领涨股)"""
    with engine.connect() as conn:
        td = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_board_basic")).scalar()
        if not td:
            return {"count": 0, "trade_date": None, "boards": []}
        rows = conn.execute(text("""
            SELECT b.board_code, b.board_name, b.member_cnt, b.total_mv,
                   b.circ_mv, b.list_proxy_date, b.hot_rank, b.hot_score,
                   b.up_cnt, b.down_cnt, b.lead_stock, b.pct_chg, b.amount,
                   d.pct_chg_eq, d.close AS index_close, d.cont_days
            FROM stock_board_basic b
            LEFT JOIN stock_board_daily_quote d
              ON d.board_code=b.board_code AND d.trade_date=b.trade_date
            WHERE b.trade_date=:td
            ORDER BY b.circ_mv DESC
        """), {"td": td}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["trade_date"] = str(td)
        if d.get("list_proxy_date") is not None:
            d["list_proxy_date"] = str(d["list_proxy_date"])
        out.append(d)
    return {"count": len(out), "trade_date": str(td), "boards": out,
            # 同花顺热榜硬上限 100 只 → 只有部分板块有在榜成分股,
            # 人气排名仅在这些板块内产生名次, 前端据此如实标注覆盖面
            "hot_covered": sum(1 for d in out if d.get("hot_rank") is not None)}


@app.get("/api/boards/{board_code}")
def board_detail(board_code: str):
    """板块详情: 最新快照 + 概念解读"""
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT board_code, board_name, board_type, member_cnt, total_mv,
                   circ_mv, list_proxy_date, hot_rank, hot_score, up_cnt,
                   down_cnt, lead_stock, pct_chg, amount, summary, trade_date
            FROM stock_board_basic
            WHERE board_code=:bc ORDER BY trade_date DESC LIMIT 1
        """), {"bc": board_code}).mappings().first()
    if not r:
        raise HTTPException(404, f"板块不存在: {board_code}")
    d = dict(r)
    d["trade_date"] = str(d["trade_date"])
    if d.get("list_proxy_date") is not None:
        d["list_proxy_date"] = str(d["list_proxy_date"])
    return d


@app.get("/api/boards/{board_code}/members")
def board_members(board_code: str,
                  only_tag: Optional[str] = Query(
                      None, description="core=只看高相关, leader=只看龙头")):
    """板块成分股: 相关度/龙头标签/市值/近20日涨幅"""
    sql = """
        SELECT m.ts_code, b.name, m.corr60, m.rel_tag, m.leader_tag,
               m.leader_score, m.total_mv, m.circ_mv, m.chg20,
               q.close, q.pct_chg
        FROM stock_board_member m
        JOIN stock_basic b ON b.ts_code=m.ts_code
        LEFT JOIN stock_daily_quote q
          ON q.ts_code=m.ts_code
         AND q.trade_date=(SELECT MAX(trade_date) FROM stock_daily_quote)
        WHERE m.board_code=:bc
    """
    args = {"bc": board_code}
    if only_tag == "core":
        sql += " AND m.rel_tag='core'"
    elif only_tag == "leader":
        sql += " AND m.leader_tag='leader'"
    sql += " ORDER BY m.leader_score DESC, m.circ_mv DESC"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), args).mappings().all()
    return {"board_code": board_code, "count": len(rows),
            "members": [dict(r) for r in rows]}


@app.get("/api/boards/{board_code}/daily")
def board_daily(board_code: str, days: int = Query(246, ge=1, le=1500)):
    """板块合成日线(基期=1000, 流通市值加权, 含等权对照列)

    days 上限 1500：板块日线已随个股日线历史扩展到 ~1,037 天/板块(近 4 年)。
    """
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT trade_date, open, high, low, close, pre_close, pct_chg,
                   pct_chg_eq, vol, amount, ma5, ma10, ma20, cont_days, cont_pct
            FROM stock_board_daily_quote
            WHERE board_code=:bc ORDER BY trade_date DESC LIMIT :lim
        """), {"bc": board_code, "lim": days}).mappings().all()
    if not rows:
        raise HTTPException(404, f"板块无日线数据: {board_code}")
    out = [dict(r) for r in reversed(rows)]
    for r in out:
        r["trade_date"] = str(r["trade_date"])
    return {"board_code": board_code, "quotes": out}


@app.get("/api/boards/{board_code}/minutes")
def board_minutes(board_code: str, date: Optional[str] = None):
    """板块分时(5分钟, 默认最新交易日)"""
    with engine.connect() as conn:
        if not date:
            date = conn.execute(text("""
                SELECT MAX(trade_date) FROM stock_board_minute_quote
                WHERE board_code=:bc
            """), {"bc": board_code}).scalar()
            if not date:
                raise HTTPException(404, f"板块无分时数据: {board_code}")
        rows = conn.execute(text("""
            SELECT bar_time, open, high, low, close, vol
            FROM stock_board_minute_quote
            WHERE board_code=:bc AND trade_date=:d ORDER BY bar_time
        """), {"bc": board_code, "d": date}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["bar_time"] = str(d["bar_time"])
        out.append(d)
    return {"board_code": board_code, "date": str(date), "bars": out}


@app.get("/api/boards/{board_code}/minute_dates")
def board_minute_dates(board_code: str, limit: int = Query(30, ge=1, le=60)):
    """该板块有分时数据的交易日列表（倒序，默认最近 30 个）"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT trade_date FROM stock_board_minute_quote
            WHERE board_code=:bc ORDER BY trade_date DESC LIMIT :lim
        """), {"bc": board_code, "lim": limit}).fetchall()
    return {"board_code": board_code, "dates": [str(r[0]) for r in rows]}


# ---------------------------------------------------------------- 暗盘(v1.5)

@app.get("/api/darkpool/signals")
def darkpool_signals(trade_date: Optional[str] = None,
                     signal: Optional[str] = None,
                     limit: int = Query(200, ge=1, le=1000)):
    """暗盘信号列表: 默认最新交易日, 按 dark_score 降序"""
    with engine.connect() as conn:
        td = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_dark_pool_indicator")).scalar()
        if not td:
            return {"count": 0, "trade_date": None, "results": []}
        sql = """
            SELECT d.ts_code, b.name, d.trade_date, d.block_amt,
                   d.block_discount, d.block_days5, d.block_discount5,
                   d.bb_net_amt, d.bb_net_amt5, d.dark_score, d.signal_tag,
                   q.close, q.pct_chg, q.cont_days, q.cont_pct
            FROM stock_dark_pool_indicator d
            LEFT JOIN stock_basic b ON b.ts_code=d.ts_code
            LEFT JOIN stock_daily_quote q
              ON q.ts_code=d.ts_code AND q.trade_date=d.trade_date
            WHERE d.trade_date=:td
        """
        args = {"td": td, "lim": limit}
        if signal:
            sql += " AND d.signal_tag=:sig"
            args["sig"] = signal
        sql += " ORDER BY d.dark_score DESC LIMIT :lim"
        rows = conn.execute(text(sql), args).mappings().all()
        # 命中策略集合(同日选股命中了哪些策略; Epic#9 暗盘表新增列):
        # 与 /api/pick/results 同口径, MySQL5.5 派生表无索引故单独查后 Python 合并
        sg_rows = conn.execute(text("""
            SELECT r.ts_code,
                   GROUP_CONCAT(DISTINCT b.strategy_name
                        ORDER BY b.strategy_name SEPARATOR '、') AS strategies
            FROM stock_pick_result r
            JOIN stock_pick_batch b ON b.id = r.batch_id
            WHERE r.trade_date = :td
            GROUP BY r.ts_code
        """), {"td": td}).mappings().all()
    smap = {str(x["ts_code"]): x["strategies"] for x in sg_rows}
    out = [dict(r) for r in rows]
    for r in out:
        r["trade_date"] = str(r["trade_date"])
        r["strategies"] = smap.get(str(r["ts_code"]))
    return {"count": len(out), "trade_date": str(td), "results": out}


@app.get("/api/darkpool/dates")
def darkpool_dates(limit: int = Query(250, ge=1, le=1000)):
    """暗盘可回看的交易日列表（降序，附当日覆盖股票数）"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT trade_date, COUNT(*) AS n FROM stock_dark_pool_indicator "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT :lim"),
            {"lim": limit}).fetchall()
    return {"count": len(rows),
            "dates": [{"trade_date": str(r[0]), "count": int(r[1])} for r in rows]}


@app.get("/api/darkpool/stock/{ts_code}")
def darkpool_stock(ts_code: str, days: int = Query(60, ge=1, le=250)):
    """个股暗盘明细: 大宗逐笔 + 龙虎榜 + 指标序列"""
    tc = ts_code.upper()
    with engine.connect() as conn:
        blocks = conn.execute(text("""
            SELECT trade_date, deal_price, deal_volume, deal_amt, premium_ratio,
                   close_price, buyer_name, seller_name
            FROM stock_block_trade WHERE ts_code=:tc
            ORDER BY trade_date DESC LIMIT :lim
        """), {"tc": tc, "lim": days}).mappings().all()
        bbs = conn.execute(text("""
            SELECT trade_date, reason, buy_amt, sell_amt, net_amt,
                   deal_amt_ratio, inst_net_amt, hot_money_net
            FROM stock_billboard WHERE ts_code=:tc
            ORDER BY trade_date DESC LIMIT :lim
        """), {"tc": tc, "lim": days}).mappings().all()
        inds = conn.execute(text("""
            SELECT trade_date, block_amt, block_discount, block_days5,
                   block_discount5, bb_net_amt, bb_net_amt5, dark_score,
                   signal_tag
            FROM stock_dark_pool_indicator WHERE ts_code=:tc
            ORDER BY trade_date DESC LIMIT :lim
        """), {"tc": tc, "lim": days}).mappings().all()

    def _c(rows):
        out = []
        for r in rows:
            d = dict(r)
            d["trade_date"] = str(d["trade_date"])
            out.append(d)
        return out
    return {"ts_code": tc, "block_trades": _c(blocks),
            "billboards": _c(bbs), "indicators": _c(inds)}


# ---------------------------------------------------------------- 涨停池(v1.6)

@app.get("/api/ztpool/dates")
def ztpool_dates(limit: int = Query(250, ge=1, le=1000)):
    """涨停池可回看的交易日列表（降序，附当日涨停家数）"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT trade_date, COUNT(*) AS n FROM stock_zt_pool "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT :lim"),
            {"lim": limit}).fetchall()
    return {"count": len(rows),
            "dates": [{"trade_date": str(r[0]), "count": int(r[1])} for r in rows]}


@app.get("/api/ztpool/pool")
def ztpool_pool(trade_date: Optional[str] = None,
                min_boards: int = Query(1, ge=1, le=20),
                exact_boards: Optional[int] = Query(None, ge=1, le=20)):
    """涨停池面板数据（引擎无关纯数据，供 ECharts/Plotly 双引擎渲染）

    返回:
      ladder   连板梯队分布 [{cont_boards, count}] 按连板数升序
      industry 行业分布 Top10 [{industry, count}] 按家数降序
      stocks   涨停明细(按连板数/封单额降序)
      strong_count 当日强势股池家数(stock_zt_pool_strong)
    """
    with engine.connect() as conn:
        td = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_zt_pool")).scalar()
        if not td:
            return {"trade_date": None, "ladder": [], "industry": [],
                    "stocks": [], "strong_count": 0}
        args = {"td": td, "mb": min_boards}
        # exact_boards 精确匹配(如首板=恰好1连板)优先于 min_boards 下限过滤
        flt = "cont_boards=:mb" if exact_boards is not None else "cont_boards>=:mb"
        if exact_boards is not None:
            args["mb"] = exact_boards
        ladder = conn.execute(text(f"""
            SELECT cont_boards, COUNT(*) AS n FROM stock_zt_pool
            WHERE trade_date=:td AND {flt}
            GROUP BY cont_boards ORDER BY cont_boards
        """), args).fetchall()
        industry = conn.execute(text(f"""
            SELECT COALESCE(NULLIF(industry,''),'未分类') AS ind, COUNT(*) AS n
            FROM stock_zt_pool
            WHERE trade_date=:td AND {flt}
            GROUP BY ind ORDER BY n DESC, ind LIMIT 10
        """), args).fetchall()
        # 申万一级行业口径(stock_sw_industry_member): 与东财口径并列, 前端可切换
        sw_industry = conn.execute(text(f"""
            SELECT COALESCE(sw.sw_name,'未分类') AS ind, COUNT(*) AS n
            FROM stock_zt_pool z
            LEFT JOIN stock_sw_industry_member sw ON sw.ts_code=z.ts_code
            WHERE z.trade_date=:td AND z.{flt}
            GROUP BY ind ORDER BY n DESC, ind LIMIT 10
        """), args).fetchall()
        stocks = conn.execute(text(f"""
            SELECT ts_code, name, cont_boards, zt_stat, first_lock_time,
                   last_lock_time, break_cnt, lock_amt, turnover_rate,
                   circ_mv, industry, pct_chg
            FROM stock_zt_pool
            WHERE trade_date=:td AND {flt}
            ORDER BY cont_boards DESC, lock_amt DESC
        """), args).mappings().all()
        strong_cnt = conn.execute(text(
            "SELECT COUNT(*) FROM stock_zt_pool_strong WHERE trade_date=:td"),
            {"td": td}).scalar()
    out = [dict(r) for r in stocks]
    for r in out:
        if r.get("first_lock_time"):
            r["first_lock_time"] = str(r["first_lock_time"])
        if r.get("last_lock_time"):
            r["last_lock_time"] = str(r["last_lock_time"])
    return {
        "trade_date": str(td),
        "ladder": [{"cont_boards": int(r[0]), "count": int(r[1])} for r in ladder],
        "industry": [{"industry": r[0], "count": int(r[1])} for r in industry],
        "sw_industry": [{"industry": r[0], "count": int(r[1])} for r in sw_industry],
        "stocks": out,
        "strong_count": int(strong_cnt or 0),
    }


# ==================== 二期面板: 市场情绪四板 ====================

@app.get("/api/panel/margin")
def panel_margin(days: int = Query(60, ge=10, le=250)):
    """两融情绪: 全市场融资余额总额/融资买入额 按日序列(供曲线)。
    两融数据 T+1 发布, 最新一日通常为上一交易日。"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT trade_date, SUM(rzye) rzye, SUM(rzmre) rzmre, COUNT(*) cnt
            FROM stock_margin_detail
            WHERE trade_date >= (SELECT MAX(trade_date) FROM stock_margin_detail)
              - INTERVAL :d DAY
            GROUP BY trade_date ORDER BY trade_date
        """), {"d": days}).mappings().all()
    out = []
    prev = None
    for r in rows:
        rzye = float(r["rzye"]) if r["rzye"] else None
        chg = (round(rzye - prev, 2) if (rzye is not None and prev is not None) else None)
        prev = rzye
        out.append({
            "trade_date": str(r["trade_date"]),
            "rzye_yi": round(rzye / 1e8, 1) if rzye else None,        # 亿元
            "rzmre_yi": round(float(r["rzmre"]) / 1e8, 1) if r["rzmre"] else None,
            "rzye_chg_yi": round(chg / 1e8, 1) if chg is not None else None,
            "stocks": int(r["cnt"]),
        })
    return {"count": len(out), "series": out}


@app.get("/api/panel/valuation")
def panel_valuation(days: int = Query(120, ge=10, le=750)):
    """估值温度: 全市场 PB 分位中位数/PE 分位中位数 按日序列(供曲线)。
    口径: stock_valuation_pctl 日度落库, 中位数代表市场整体便宜/贵程度。"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT trade_date,
                   AVG(pb_pctl) pb_pctl_med, AVG(pe_pctl) pe_pctl_med,
                   COUNT(*) cnt
            FROM stock_valuation_pctl
            WHERE trade_date >= (SELECT MAX(trade_date) FROM stock_valuation_pctl)
              - INTERVAL :d DAY
            GROUP BY trade_date ORDER BY trade_date
        """), {"d": days}).mappings().all()
    # 注: 中位数用 AVG 近似的说明 —— MySQL 5.5 无窗口函数, 逐日取真中位数成本高;
    # 均值与中位数在 4000+ 只的横截面上走势一致, 曲线用途(看温度拐点)不受影响
    out = [{"trade_date": str(r["trade_date"]),
            "pb_pctl": round(float(r["pb_pctl_med"]), 4) if r["pb_pctl_med"] else None,
            "pe_pctl": round(float(r["pe_pctl_med"]), 4) if r["pe_pctl_med"] else None,
            "stocks": int(r["cnt"])} for r in rows]
    return {"count": len(out), "series": out}


@app.get("/api/panel/hotrank")
def panel_hotrank(days: int = Query(10, ge=1, le=60), top: int = Query(20, ge=5, le=100)):
    """人气榜: 最新快照 Top N + 近 N 日在榜次数(连续上榜=关注度高)"""
    with engine.connect() as conn:
        td = conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_hot_rank")).scalar()
        if not td:
            return {"count": 0, "trade_date": None, "items": []}
        rows = conn.execute(text("""
            SELECT h.ts_code, b.name, h.rank_num, h.price, h.pct_chg,
                   (SELECT COUNT(DISTINCT h2.trade_date) FROM stock_hot_rank h2
                    WHERE h2.ts_code=h.ts_code
                      AND h2.trade_date >= :td - INTERVAL :d DAY) days_onboard
            FROM stock_hot_rank h
            LEFT JOIN stock_basic b ON b.ts_code=h.ts_code
            WHERE h.trade_date=:td
            ORDER BY h.rank_num LIMIT :top
        """), {"td": td, "d": days, "top": top}).mappings().all()
    out = [{"ts_code": r["ts_code"], "name": r["name"],
            "rank_num": int(r["rank_num"]) if r["rank_num"] else None,
            "price": float(r["price"]) if r["price"] is not None else None,
            "pct_chg": float(r["pct_chg"]) if r["pct_chg"] is not None else None,
            "days_onboard": int(r["days_onboard"])} for r in rows]
    return {"count": len(out), "trade_date": str(td), "days_window": days, "items": out}


@app.get("/api/panel/fundflow")
def panel_fundflow(days: int = Query(20, ge=5, le=120), top: int = Query(10, ge=5, le=50)):
    """资金流对比: 近 N 日全市场主力净流入合计(按日) + 最新一日净流入/流出 Top N"""
    with engine.connect() as conn:
        daily = conn.execute(text("""
            SELECT trade_date, SUM(main_net) total_net,
                   SUM(CASE WHEN main_net>0 THEN 1 ELSE 0 END) inflow_cnt,
                   SUM(CASE WHEN main_net<0 THEN 1 ELSE 0 END) outflow_cnt
            FROM stock_money_flow
            WHERE trade_date >= (SELECT MAX(trade_date) FROM stock_money_flow)
              - INTERVAL :d DAY
            GROUP BY trade_date ORDER BY trade_date
        """), {"d": days}).mappings().all()
        td = daily[-1]["trade_date"] if daily else None
        tops = []
        if td:
            tops = conn.execute(text("""
                SELECT f.ts_code, b.name, f.main_net, f.main_pct,
                       q.close, q.pct_chg
                FROM stock_money_flow f
                LEFT JOIN stock_basic b ON b.ts_code=f.ts_code
                LEFT JOIN stock_daily_quote q
                  ON q.ts_code=f.ts_code AND q.trade_date=f.trade_date
                WHERE f.trade_date=:td AND f.main_net IS NOT NULL
                ORDER BY f.main_net DESC
            """), {"td": td}).mappings().all()
    daily_out = [{"trade_date": str(r["trade_date"]),
                  "total_net_yi": round(float(r["total_net"]) / 1e8, 1) if r["total_net"] else None,
                  "inflow_cnt": int(r["inflow_cnt"] or 0),
                  "outflow_cnt": int(r["outflow_cnt"] or 0)} for r in daily]
    top_out = [{"ts_code": r["ts_code"], "name": r["name"],
                "main_net_yi": round(float(r["main_net"]) / 1e8, 2),
                "main_pct": float(r["main_pct"]) if r["main_pct"] is not None else None,
                "close": float(r["close"]) if r["close"] is not None else None,
                "pct_chg": float(r["pct_chg"]) if r["pct_chg"] is not None else None}
               for r in tops]
    return {"count": len(daily_out), "trade_date": str(td) if td else None,
            "daily": daily_out, "top_in": top_out[:top],
            "top_out": list(reversed(top_out[-top:]))}


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api.main:app", host=config.API_HOST, port=config.API_PORT, reload=False)
