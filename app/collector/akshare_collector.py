# -*- coding: utf-8 -*-
"""akshare 数据源采集（P0: 涨停池 / 强势股池 / 估值历史, 2026-09-17, 方案见 epic/ 方案文档;
2026-09-20 扩容: 跌停池/炸板池/昨涨停表现(同端点+3请求/日) + 业绩报表(按报告期批量页翻)）

akshare 以 vendored 源码接入（config.AKSHARE_PATH 加入 sys.path, 不装 pip 包,
目标站改版可直接改 akshare/akshare/ 下源码修复）。

数据源:
    涨停股池   ak.stock_zt_pool_em           东财 push2ex.eastmoney.com/getTopicZTPool
    强势股池   ak.stock_zt_pool_strong_em    东财 push2ex.eastmoney.com/getTopicQSPool
    跌停股池   ak.stock_zt_pool_dtgc_em      东财 push2ex getTopicDT_POOL(空日返回无列空df, 0行正常)
    炸板股池   ak.stock_zt_pool_zbgc_em      东财 push2ex getTopicZB_POOL
    昨涨停表现 ak.stock_zt_pool_previous_em  东财 push2ex(昨日涨停股今日表现)
    估值历史   ak.stock_value_em             东财 datacenter-web RPT_VALUEANALYSIS_DET
               （与 darkpool 同域, 该域已长期实测稳定; 单股一次返回全历史 pageSize=5000）
    业绩报表   ak.stock_yjbb_em              东财 datacenter RPT_LICO_FN_CPD
               （按报告期批量页翻, 全市场~23页/6s; 避免单股 5000+ 请求, 季度频）

口径说明:
    - 涨停统计 "2/2" = 连板数/涨停天数, 原样落 zt_stat, 连板数另存 cont_boards
    - 封板时间原始 6 位 HHMMSS 字符串落表, 不做时区/类型转换
    - 估值与 stock_daily_quote 的 pe_ttm/pb 快照列互补: 本表存全历史 + PE静/PEG/市现率/市销率,
      供低估值分位策略用; 前向快照仍由 valsnap 负责, 两者不互相写
    - 业绩报表含北交所等非本地池代码, 全量入库, 策略层自行过滤(与 valuation/zt_pool 同哲学);
      ts_code 后缀按代码首位推断(6->SH, 0/3->SZ, 其余->BJ)
频控: push2ex/datacenter 报表接口, 请求间隔 >=0.5s（沿用 darkpool 经验）
频度: 五个池每日盘后 1 次(进 --all); 业绩报表日度增量仅跑最新报告期(~6s, 进 --all);
      估值为单股全历史接口, 首次全量回填以后台任务跑(--valuation --full), 日常增量 --valuation
      (仅补库内最大日之后的新行)。
"""
import datetime as dt
import os
import sys
import time

# akshare 内部用裸 requests.get(默认信任环境代理); 本机若被注入 HTTP(S)_PROXY
# 会导致请求被坏代理断连 —— 本模块内强制直连
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from sqlalchemy import text

from app import config
from app.db import engine

REQ_INTERVAL = 0.5      # 东财报表接口风控间隔（沿用 darkpool 经验）

_ak = None


def _akshare():
    """惰性 import vendored akshare（避免未安装依赖时影响其他采集器）"""
    global _ak
    if _ak is None:
        if config.AKSHARE_PATH not in sys.path:
            sys.path.insert(0, config.AKSHARE_PATH)
        import akshare
        _ak = akshare
    return _ak


def _load_symbol_map() -> dict:
    """symbol(6位) -> ts_code(带后缀), 仅在市股票"""
    with engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text(
            "SELECT symbol, ts_code FROM stock_basic WHERE is_active=1")).fetchall()}


def _recent_trade_dates(n: int) -> list:
    """库内最近 n 个交易日（指数表, 已确认与自然日/周末无关的真实交易日）"""
    with engine.connect() as conn:
        return [str(r[0]) for r in conn.execute(text(
            "SELECT DISTINCT trade_date FROM stock_index_daily "
            "ORDER BY trade_date DESC LIMIT :n"), {"n": n}).fetchall()]


def _num(v):
    """akshare 数值字段: NaN/None/'-' -> None"""
    if v is None:
        return None
    try:
        import math
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return int(f) if f is not None else None


def _upsert_rows(table: str, rows: list, key_cols=("trade_date", "ts_code")):
    """通用批量 UPSERT: 列集由首行 keys 推导, 键列不参与 UPDATE"""
    if not rows:
        return
    cols = list(rows[0].keys())
    col_sql = ", ".join(cols)
    val_sql = ", ".join(f":{c}" for c in cols)
    upd_sql = ", ".join(f"{c}=VALUES({c})" for c in cols if c not in key_cols)
    sql = (f"INSERT INTO {table} ({col_sql}) VALUES ({val_sql}) "
           f"ON DUPLICATE KEY UPDATE {upd_sql}, etl_update_time=NOW()")
    with engine.begin() as conn:
        for i in range(0, len(rows), 500):
            conn.execute(text(sql), rows[i:i + 500])


def code_to_ts(code: str) -> str:
    """6 位代码 -> ts_code(带市场后缀): 6->SH, 0/3->SZ, 其余(4/8)->BJ"""
    c = str(code).zfill(6)
    if c.startswith("6"):
        return f"{c}.SH"
    if c.startswith("0") or c.startswith("3"):
        return f"{c}.SZ"
    return f"{c}.BJ"


# ---------------------------------------------------------------- 涨停池扩展三池映射

def _map_dt(x, tc, d):
    """跌停池行映射（无跌停日接口返回无列空df, 不会进到这里）"""
    return {
        "trade_date": d, "ts_code": tc,
        "name": (x.get("名称") or "")[:32] or None,
        "pct_chg": _num(x.get("涨跌幅")),
        "close_price": _num(x.get("最新价")),
        "amount": _num(x.get("成交额")),
        "circ_mv": _num(x.get("流通市值")),
        "total_mv": _num(x.get("总市值")),
        "turnover_rate": _num(x.get("换手率")),
        "lock_amt": _num(x.get("封板资金")),
        "last_lock_time": (str(x.get("最后封板时间") or "")[:6] or None),
        "break_cnt": _int(x.get("炸板次数")),
        "zt_stat": (str(x.get("涨停统计") or "")[:8] or None),
        "cont_down": _int(x.get("连续跌停")),
        "industry": (x.get("所属行业") or "")[:32] or None,
    }


def _map_zbgc(x, tc, d):
    """炸板池行映射"""
    return {
        "trade_date": d, "ts_code": tc,
        "name": (x.get("名称") or "")[:32] or None,
        "pct_chg": _num(x.get("涨跌幅")),
        "close_price": _num(x.get("最新价")),
        "zt_price": _num(x.get("涨停价")),
        "amount": _num(x.get("成交额")),
        "circ_mv": _num(x.get("流通市值")),
        "total_mv": _num(x.get("总市值")),
        "turnover_rate": _num(x.get("换手率")),
        "speed": _num(x.get("涨速")),
        "first_lock_time": (str(x.get("首次封板时间") or "")[:6] or None),
        "break_cnt": _int(x.get("炸板次数")),
        "zt_stat": (str(x.get("涨停统计") or "")[:8] or None),
        "amplitude": _num(x.get("振幅")),
        "industry": (x.get("所属行业") or "")[:32] or None,
    }


def _map_prev(x, tc, d):
    """昨涨停表现行映射"""
    return {
        "trade_date": d, "ts_code": tc,
        "name": (x.get("名称") or "")[:32] or None,
        "pct_chg": _num(x.get("涨跌幅")),
        "close_price": _num(x.get("最新价")),
        "zt_price": _num(x.get("涨停价")),
        "amount": _num(x.get("成交额")),
        "circ_mv": _num(x.get("流通市值")),
        "total_mv": _num(x.get("总市值")),
        "turnover_rate": _num(x.get("换手率")),
        "speed": _num(x.get("涨速")),
        "amplitude": _num(x.get("振幅")),
        "prev_lock_time": (str(x.get("昨日封板时间") or "")[:6] or None),
        "prev_boards": _int(x.get("昨日连板数")),
        "zt_stat": (str(x.get("涨停统计") or "")[:8] or None),
        "industry": (x.get("所属行业") or "")[:32] or None,
    }


_POOL_SPECS = [
    ("stock_zt_pool_dtgc_em", "stock_zt_pool_dt", _map_dt),
    ("stock_zt_pool_zbgc_em", "stock_zt_pool_zbgc", _map_zbgc),
    ("stock_zt_pool_previous_em", "stock_zt_pool_prev", _map_prev),
]


# ---------------------------------------------------------------- 涨停池/强势股池

def sync_zt_pool(days: int = 1) -> dict:
    """涨停股池 + 强势股池（幂等 UPSERT, 键 日+股）

    days: 回看最近 N 个交易日（默认 1 = 只采最新交易日; 回填传大值）。
    """
    ak = _akshare()
    dates = _recent_trade_dates(days)
    if not dates:
        print("[ztpool] stock_index_daily 无交易日, 跳过", flush=True)
        return {"dates": 0, "zt_rows": 0, "strong_rows": 0,
                "dt_rows": 0, "zbgc_rows": 0, "prev_rows": 0}
    sym_map = _load_symbol_map()

    zt_total = strong_total = miss_total = 0
    pool_total = {"dt": 0, "zbgc": 0, "prev": 0}
    for d in dates:
        ymd = d.replace("-", "")
        zt_day = strong_day = 0
        # ---- 涨停股池 ----
        try:
            zt = ak.stock_zt_pool_em(date=ymd)
        except Exception as e:
            print(f"[ztpool] {d} 涨停池拉取失败: {type(e).__name__} {e}", flush=True)
            zt = None
        time.sleep(REQ_INTERVAL)
        rows = []
        if zt is not None and not zt.empty:
            for x in zt.to_dict(orient="records"):
                tc = sym_map.get(str(x["代码"]).zfill(6))
                if not tc:
                    miss_total += 1
                    continue
                rows.append({
                    "trade_date": d, "ts_code": tc,
                    "name": (x.get("名称") or "")[:32] or None,
                    "pct_chg": _num(x.get("涨跌幅")),
                    "close_price": _num(x.get("最新价")),
                    "amount": _num(x.get("成交额")),
                    "circ_mv": _num(x.get("流通市值")),
                    "total_mv": _num(x.get("总市值")),
                    "turnover_rate": _num(x.get("换手率")),
                    "lock_amt": _num(x.get("封板资金")),
                    "first_lock_time": (str(x.get("首次封板时间") or "")[:6] or None),
                    "last_lock_time": (str(x.get("最后封板时间") or "")[:6] or None),
                    "break_cnt": _int(x.get("炸板次数")),
                    "zt_stat": (str(x.get("涨停统计") or "")[:8] or None),
                    "cont_boards": _int(x.get("连板数")),
                    "industry": (x.get("所属行业") or "")[:32] or None,
                })
        with engine.begin() as conn:
            for i in range(0, len(rows), 500):
                conn.execute(text("""
                    INSERT INTO stock_zt_pool
                        (trade_date, ts_code, name, pct_chg, close_price, amount,
                         circ_mv, total_mv, turnover_rate, lock_amt,
                         first_lock_time, last_lock_time, break_cnt, zt_stat,
                         cont_boards, industry)
                    VALUES (:trade_date, :ts_code, :name, :pct_chg, :close_price,
                            :amount, :circ_mv, :total_mv, :turnover_rate,
                            :lock_amt, :first_lock_time, :last_lock_time,
                            :break_cnt, :zt_stat, :cont_boards, :industry)
                    ON DUPLICATE KEY UPDATE
                        name=VALUES(name), pct_chg=VALUES(pct_chg),
                        close_price=VALUES(close_price), amount=VALUES(amount),
                        circ_mv=VALUES(circ_mv), total_mv=VALUES(total_mv),
                        turnover_rate=VALUES(turnover_rate),
                        lock_amt=VALUES(lock_amt),
                        first_lock_time=VALUES(first_lock_time),
                        last_lock_time=VALUES(last_lock_time),
                        break_cnt=VALUES(break_cnt), zt_stat=VALUES(zt_stat),
                        cont_boards=VALUES(cont_boards),
                        industry=VALUES(industry), etl_update_time=NOW()
                """), rows[i:i + 500])
        zt_day = len(rows)
        zt_total += zt_day

        # ---- 强势股池 ----
        try:
            sg = ak.stock_zt_pool_strong_em(date=ymd)
        except Exception as e:
            print(f"[ztpool] {d} 强势股池拉取失败: {type(e).__name__} {e}", flush=True)
            sg = None
        time.sleep(REQ_INTERVAL)
        rows = []
        if sg is not None and not sg.empty:
            for x in sg.to_dict(orient="records"):
                tc = sym_map.get(str(x["代码"]).zfill(6))
                if not tc:
                    miss_total += 1
                    continue
                rows.append({
                    "trade_date": d, "ts_code": tc,
                    "name": (x.get("名称") or "")[:32] or None,
                    "pct_chg": _num(x.get("涨跌幅")),
                    "close_price": _num(x.get("最新价")),
                    "zt_price": _num(x.get("涨停价")),
                    "amount": _num(x.get("成交额")),
                    "circ_mv": _num(x.get("流通市值")),
                    "total_mv": _num(x.get("总市值")),
                    "turnover_rate": _num(x.get("换手率")),
                    "speed": _num(x.get("涨速")),
                    "is_new_high": (str(x.get("是否新高") or "")[:4] or None),
                    "vol_ratio": _num(x.get("量比")),
                    "zt_stat": (str(x.get("涨停统计") or "")[:8] or None),
                    "reason": (str(x.get("入选理由") or "")[:64] or None),
                    "industry": (x.get("所属行业") or "")[:32] or None,
                })
        with engine.begin() as conn:
            for i in range(0, len(rows), 500):
                conn.execute(text("""
                    INSERT INTO stock_zt_pool_strong
                        (trade_date, ts_code, name, pct_chg, close_price, zt_price,
                         amount, circ_mv, total_mv, turnover_rate, speed,
                         is_new_high, vol_ratio, zt_stat, reason, industry)
                    VALUES (:trade_date, :ts_code, :name, :pct_chg, :close_price,
                            :zt_price, :amount, :circ_mv, :total_mv,
                            :turnover_rate, :speed, :is_new_high, :vol_ratio,
                            :zt_stat, :reason, :industry)
                    ON DUPLICATE KEY UPDATE
                        name=VALUES(name), pct_chg=VALUES(pct_chg),
                        close_price=VALUES(close_price),
                        zt_price=VALUES(zt_price), amount=VALUES(amount),
                        circ_mv=VALUES(circ_mv), total_mv=VALUES(total_mv),
                        turnover_rate=VALUES(turnover_rate), speed=VALUES(speed),
                        is_new_high=VALUES(is_new_high),
                        vol_ratio=VALUES(vol_ratio), zt_stat=VALUES(zt_stat),
                        reason=VALUES(reason), industry=VALUES(industry),
                        etl_update_time=NOW()
                """), rows[i:i + 500])
        strong_day = len(rows)
        strong_total += strong_day

        # ---- 跌停池/炸板池/昨涨停表现(同端点 push2ex, 2026-09-20 扩容) ----
        pool_day = {"dt": 0, "zbgc": 0, "prev": 0}
        for fn_name, table, mapper in _POOL_SPECS:
            try:
                pdf = getattr(ak, fn_name)(date=ymd)
            except Exception as e:
                print(f"[ztpool] {d} {table} 拉取失败: {type(e).__name__} {e}", flush=True)
                pdf = None
            time.sleep(REQ_INTERVAL)
            rows = []
            if pdf is not None and not pdf.empty:
                for x in pdf.to_dict(orient="records"):
                    tc = sym_map.get(str(x.get("代码") or "").zfill(6))
                    if not tc:
                        miss_total += 1
                        continue
                    rows.append(mapper(x, tc, d))
            _upsert_rows(table, rows)
            key = {"stock_zt_pool_dt": "dt",
                   "stock_zt_pool_zbgc": "zbgc",
                   "stock_zt_pool_prev": "prev"}[table]
            pool_day[key] = len(rows)
            pool_total[key] += len(rows)

        print(f"[ztpool] {d} 涨停 {zt_day} / 强势 {strong_day} / "
              f"跌停 {pool_day['dt']} / 炸板 {pool_day['zbgc']} / "
              f"昨涨停 {pool_day['prev']}", flush=True)
        time.sleep(REQ_INTERVAL)

    print(f"[ztpool] 完成 {len(dates)} 日: 涨停 {zt_total} 行, 强势 {strong_total} 行, "
          f"跌停 {pool_total['dt']} 行, 炸板 {pool_total['zbgc']} 行, "
          f"昨涨停 {pool_total['prev']} 行, 非本地池剔除 {miss_total}", flush=True)
    return {"dates": len(dates), "zt_rows": zt_total,
            "strong_rows": strong_total, "missed": miss_total,
            "dt_rows": pool_total["dt"], "zbgc_rows": pool_total["zbgc"],
            "prev_rows": pool_total["prev"]}


# ---------------------------------------------------------------- 估值历史

def sync_valuation(days: int = None, full: bool = False, limit: int = None,
                   shard: int = 0, n_shards: int = 1) -> dict:
    """估值历史采集（单股一次拉全历史, 客户端过滤后 UPSERT）

    增量(默认): 只写 > 库内该股最大 trade_date 的新行（日常每日一股 1 请求）。
    days=N:     只写最近 N 行（回填指定窗口用）。
    full=True:  全量覆盖（首次回填, 约 8 年/股, 建议后台任务 + 分片并行）。
    """
    ak = _akshare()
    with engine.connect() as conn:
        stocks = conn.execute(text(
            "SELECT symbol, ts_code FROM stock_basic WHERE is_active=1 "
            "ORDER BY ts_code")).fetchall()
        max_dates = {r[0]: str(r[1]) for r in conn.execute(text(
            "SELECT ts_code, MAX(trade_date) FROM stock_valuation "
            "GROUP BY ts_code")).fetchall()}

    # 分片: 与 --valbackfill 同款, 多进程并行时各跑一片
    stocks = [s for i, s in enumerate(stocks) if i % n_shards == shard]
    if limit:
        stocks = stocks[:limit]

    ok = empty = failed = rows_total = skip_total = 0
    failed_list = []
    t0 = time.time()
    for idx, (sym, tc) in enumerate(stocks, 1):
        try:
            df = ak.stock_value_em(symbol=sym)
        except Exception as e:
            failed += 1
            failed_list.append(f"{tc}:{type(e).__name__}")
            time.sleep(REQ_INTERVAL)
            continue
        if df is None or df.empty:
            empty += 1
            time.sleep(REQ_INTERVAL)
            continue
        recs = []
        last_d = max_dates.get(tc)
        for x in df.to_dict(orient="records"):
            d = str(x["数据日期"])
            if full:
                pass
            elif days:
                pass  # 下方统一按 tail(days) 截断
            elif last_d and d <= last_d:
                continue  # 增量: 已有行不碰
            recs.append({
                "ts_code": tc, "trade_date": d,
                "close_price": _num(x.get("当日收盘价")),
                "pct_chg": _num(x.get("当日涨跌幅")),
                "total_mv": _num(x.get("总市值")),
                "circ_mv": _num(x.get("流通市值")),
                "total_share": _num(x.get("总股本")),
                "circ_share": _num(x.get("流通股本")),
                "pe_ttm": _num(x.get("PE(TTM)")),
                "pe_static": _num(x.get("PE(静)")),
                "pb": _num(x.get("市净率")),
                "peg": _num(x.get("PEG值")),
                "pcf": _num(x.get("市现率")),
                "ps": _num(x.get("市销率")),
            })
        if days and not full:
            recs = recs[-days:]
        if not recs:
            skip_total += 1
            time.sleep(REQ_INTERVAL)
            continue
        with engine.begin() as conn:
            for i in range(0, len(recs), 1000):
                conn.execute(text("""
                    INSERT INTO stock_valuation
                        (ts_code, trade_date, close_price, pct_chg, total_mv,
                         circ_mv, total_share, circ_share, pe_ttm, pe_static,
                         pb, peg, pcf, ps)
                    VALUES (:ts_code, :trade_date, :close_price, :pct_chg,
                            :total_mv, :circ_mv, :total_share, :circ_share,
                            :pe_ttm, :pe_static, :pb, :peg, :pcf, :ps)
                    ON DUPLICATE KEY UPDATE
                        close_price=VALUES(close_price), pct_chg=VALUES(pct_chg),
                        total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv),
                        total_share=VALUES(total_share),
                        circ_share=VALUES(circ_share), pe_ttm=VALUES(pe_ttm),
                        pe_static=VALUES(pe_static), pb=VALUES(pb),
                        peg=VALUES(peg), pcf=VALUES(pcf), ps=VALUES(ps),
                        etl_update_time=NOW()
                """), recs[i:i + 1000])
        ok += 1
        rows_total += len(recs)
        if idx % 100 == 0:
            print(f"[valuation] progress {idx}/{len(stocks)} ok={ok} "
                  f"rows={rows_total} failed={failed} "
                  f"elapsed={int(time.time() - t0)}s", flush=True)
        time.sleep(REQ_INTERVAL)

    print(f"[valuation] 完成: 池={len(stocks)} ok={ok} empty={empty} "
          f"无新行={skip_total} failed={failed} rows={rows_total} "
          f"用时={int(time.time() - t0)}s", flush=True)
    if failed_list:
        print(f"[valuation] 失败样例: {failed_list[:5]}", flush=True)
    return {"total": len(stocks), "ok": ok, "empty": empty, "skip": skip_total,
            "failed": failed, "rows": rows_total, "failed_list": failed_list}

# ---------------------------------------------------------------- 同花顺资金流(P1)

def _cn_money(v):
    """同花顺中文金额字符串 -> 元: '6.98亿' -> 6.98e8, '-2468.54万' -> -2468.54e4"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s == "-":
        return None
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _pct(v):
    """'45.58%' -> 45.58"""
    if v is None:
        return None
    s = str(v).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def sync_money_flow_ths(trade_date: str = None) -> dict:
    """同花顺个股资金流日度快照(即时全市场 ~5,200只/12s, 盘后采集)

    与新浪 stock_money_flow 同口径互补, 供交叉验证(两源净额背离=数据异常预警)。
    trade_date: 默认库内日线最大交易日(盘后口径对齐; 盘中跑会记到上一交易日, 注意)。
    幂等 UPSERT, 键 日+股。全量入库不过滤科创板(与 valuation 表同哲学, 策略层自行过滤)。
    """
    ak = _akshare()
    if trade_date is None:
        with engine.connect() as conn:
            trade_date = conn.execute(text(
                "SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
        if not trade_date:
            raise RuntimeError("stock_daily_quote 无数据, 请先执行 --quotes")
    df = ak.stock_fund_flow_individual(symbol="即时")
    smap = _load_symbol_map()
    rows, missed = [], 0
    for r in df.itertuples():
        tc = smap.get(str(r.股票代码).zfill(6))   # THS代码列为数值型, 前导零会丢(002717->2717)
        if not tc:
            missed += 1
            continue
        rows.append({
            "tc": tc, "td": str(trade_date),
            "close": _num(r.最新价), "pct": _pct(r.涨跌幅),
            "tor": _pct(r.换手率),
            "inf": _cn_money(r.流入资金), "outf": _cn_money(r.流出资金),
            "net": _cn_money(r.净额), "amt": _cn_money(r.成交额),
        })
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO stock_money_flow_ths
                (ts_code, trade_date, close, pct_chg, turnover_rate,
                 inflow, outflow, net_amt, amount)
            VALUES (:tc, :td, :close, :pct, :tor, :inf, :outf, :net, :amt)
            ON DUPLICATE KEY UPDATE
                close=VALUES(close), pct_chg=VALUES(pct_chg),
                turnover_rate=VALUES(turnover_rate), inflow=VALUES(inflow),
                outflow=VALUES(outflow), net_amt=VALUES(net_amt),
                amount=VALUES(amount), etl_update_time=NOW()
        """), rows)
    print(f"[thsmoney] date={trade_date} rows={len(rows)} 非本地池剔除={missed}",
          flush=True)
    return {"trade_date": str(trade_date), "rows": len(rows), "missed": missed}


# ---------------------------------------------------------------- 申万一级行业成分(P1)

def sync_sw_industry_member() -> dict:
    """申万一级行业成分全量刷新(申万宏源研究官网, 31 行业 ~31 请求)

    破新浪板块成分 2,563 只上限(源侧限制): 申万成分覆盖全 A 股,
    且与现有 49 个申万行业板块/涨停池行业命名同族, 可直接互查。
    行业归属是慢变量(不定期调整), 建议周度或按需跑, 不进 --all。
    全量刷新: 逐板先删后写(同事务), 成分调出自然清理; 非本地池代码跳过计数。
    """
    ak = _akshare()
    boards = ak.sw_index_first_info()
    smap = _load_symbol_map()
    total_rows, total_missed, board_cnt = 0, 0, 0
    for b in boards.itertuples():
        sw_code = str(b.行业代码).split(".")[0]   # '801010.SI' -> '801010'
        sw_name = str(b.行业名称)
        try:
            cons = ak.index_component_sw(symbol=sw_code)
        except Exception as e:
            print(f"[swmember] {sw_code} {sw_name} 成分获取失败: {e}", flush=True)
            continue
        rows, missed = [], 0
        for r in cons.itertuples():
            tc = smap.get(str(r.证券代码).zfill(6))   # 防御前导零丢失
            if not tc:
                missed += 1
                continue
            rows.append({
                "tc": tc, "sc": sw_code, "sn": sw_name,
                "w": _num(r.最新权重),
                "ind": (str(r.计入日期) if r.计入日期 is not None
                        and str(r.计入日期) != "NaT" else None),
            })
        with engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM stock_sw_industry_member WHERE sw_code=:sc"),
                {"sc": sw_code})
            if rows:
                conn.execute(text("""
                    INSERT INTO stock_sw_industry_member
                        (ts_code, sw_code, sw_name, weight, in_date)
                    VALUES (:tc, :sc, :sn, :w, :ind)
                """), rows)
        total_rows += len(rows)
        total_missed += missed
        board_cnt += 1
        print(f"[swmember] {sw_code} {sw_name}: {len(rows)} 只"
              + (f" (剔除{missed})" if missed else ""), flush=True)
        time.sleep(REQ_INTERVAL)
    print(f"[swmember] 完成: {board_cnt} 行业, {total_rows} 条映射, "
          f"非本地池剔除 {total_missed}", flush=True)
    return {"boards": board_cnt, "rows": total_rows, "missed": total_missed}


# ---------------------------------------------------------------- 业绩报表(P1, 2026-09-20)

_FIN_COL_MAP = {
    "股票简称": "name",
    "每股收益": "eps",
    "营业总收入-营业总收入": "revenue",
    "营业总收入-同比增长": "revenue_yoy",
    "营业总收入-季度环比增长": "revenue_qoq",
    "净利润-净利润": "net_profit",
    "净利润-同比增长": "net_profit_yoy",
    "净利润-季度环比增长": "net_profit_qoq",
    "每股净资产": "bps",
    "净资产收益率": "roe",
    "每股经营现金流量": "ocfps",
    "销售毛利率": "gross_margin",
    "所处行业": "industry",
}


def _recent_quarter_ends(n: int) -> list:
    """最近 n 个已结束的报告期(季末), 按时间升序返回 'YYYY-MM-DD' 列表"""
    today = dt.date.today()
    ends = []
    y = today.year
    for md in ("12-31", "09-30", "06-30", "03-31"):
        for yy in (y, y - 1, y - 2, y - 3, y - 4):
            ends.append(dt.date.fromisoformat(f"{yy}-{md}"))
    ends = sorted(set(e for e in ends if e < today), reverse=True)
    return [e.isoformat() for e in ends[:n]]


def sync_fin_report(quarters: int = 1, report_dates: list = None) -> dict:
    """业绩报表采集(东财 datacenter 按报告期批量页翻, akshare stock_yjbb_em)

    性价比要点: 每个报告期全市场仅 ~23 页(6s), 避免单股全历史通道的 5000+ 请求;
    日常增量 quarters=1 只跑最新报告期, 幂等 UPSERT(披露口径后续修正自动覆盖)。
    quarters: 回看最近 n 个已结束报告期(首次回填建议 8 = 两年)。
    report_dates: 显式指定报告期列表 'YYYY-MM-DD'(优先于 quarters)。
    """
    ak = _akshare()
    if report_dates:
        rdates = sorted(set(report_dates))
    else:
        rdates = _recent_quarter_ends(quarters)
    smap = _load_symbol_map()
    total_rows = 0
    for rd in rdates:
        ymd = rd.replace("-", "")
        try:
            df = ak.stock_yjbb_em(date=ymd)
        except Exception as e:
            print(f"[finreport] {rd} 拉取失败: {type(e).__name__} {e}", flush=True)
            continue
        if df is None or df.empty:
            print(f"[finreport] {rd} 无数据(报告期未到或未披露)", flush=True)
            continue
        rows = []
        for x in df.to_dict(orient="records"):
            code = str(x.get("股票代码") or "").zfill(6)
            if not code or code == "nan":
                continue
            tc = smap.get(code) or code_to_ts(code)   # 非本地池(BJ等)按首位推断后缀
            adv = x.get("最新公告日期")
            adv = str(adv)[:10] if adv is not None and str(adv)[:4].isdigit() else None
            row = {"ts_code": tc, "report_date": rd}
            for src, dst in _FIN_COL_MAP.items():
                row[dst] = _num(x.get(src))
            row["name"] = (str(x.get("股票简称") or "")[:32] or None)
            row["industry"] = (str(x.get("所处行业") or "")[:32] or None)
            row["announce_date"] = adv
            rows.append(row)
        # 通用 _upsert_rows 的 UPDATE 排除键是 (trade_date, ts_code), 本表键不同, 单独拼
        cols = list(rows[0].keys())
        upd = ", ".join(f"{c}=VALUES({c})" for c in cols
                        if c not in ("ts_code", "report_date"))
        sql = (f"INSERT INTO stock_fin_report ({', '.join(cols)}) "
               f"VALUES ({', '.join(':' + c for c in cols)}) "
               f"ON DUPLICATE KEY UPDATE {upd}, etl_update_time=NOW()")
        with engine.begin() as conn:
            for i in range(0, len(rows), 1000):
                conn.execute(text(sql), rows[i:i + 1000])
        total_rows += len(rows)
        print(f"[finreport] {rd}: {len(rows)} 行", flush=True)
    print(f"[finreport] 完成 {len(rdates)} 个报告期, 共 {total_rows} 行", flush=True)
    return {"report_dates": rdates, "rows": total_rows}

def _hot_rank_top100() -> list:
    """东财个股人气榜 Top100(排名接口 emappdata.eastmoney.com, 与 push2* 不同域,
    push2 被 WAF 拦截期间仍可用)。返回 [{code: 'SZ002119', rank: 1}];
    行情字段(最新价/涨跌幅)由调用方从库内 stock_daily_quote 关联补齐
    (akshare 原实现第二步走 push2 ulist, 2026-09-23 起被拦)。"""
    import json as _json
    import urllib.request
    url = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
    payload = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
               "marketType": "", "pageNo": 1, "pageSize": 100}
    req = urllib.request.Request(
        url, data=_json.dumps(payload).encode("utf-8"),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = _json.load(resp).get("data") or []
    return [{"code": str(x["sc"]), "rank": int(x["rk"])} for x in data]


def sync_hot_rank(trade_date: str = None) -> dict:
    """东财个股人气榜 Top100 日度快照(1 请求, 无历史可回填, 逐日累积)。
    HOT_RANK_BREAK 策略的数据前置: 人气排名快速上升需多日历史。
    行情字段从库内日线关联(见 _hot_rank_top100 注释)。"""
    with engine.connect() as conn:
        d = trade_date or conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_index_daily")).scalar()
    if d is None:
        return {"trade_date": None, "rows": 0, "missed": 0}
    try:
        ranking = _hot_rank_top100()
    except Exception as e:
        print(f"[hotrank] emappdata 拉取失败: {str(e)[:80]}", flush=True)
        return {"trade_date": str(d), "rows": 0, "missed": 0, "error": str(e)[:80]}
    time.sleep(REQ_INTERVAL)
    sym_map = _load_symbol_map()
    # 库内当日日线 → {6位代码: (close, pct_chg)}
    with engine.connect() as conn:
        qrows = conn.execute(text(
            "SELECT ts_code, close, pct_chg FROM stock_daily_quote "
            "WHERE trade_date = :d"), {"d": d}).fetchall()
    qmap = {ts.split(".")[0]: (c, p) for ts, c, p in qrows}
    rows, missed = [], 0
    for x in ranking:
        code = x["code"].replace("SH", "").replace("SZ", "")
        tc = sym_map.get(code.zfill(6))
        if not tc:
            missed += 1
            continue
        price, pct = qmap.get(code.zfill(6), (None, None))
        rows.append({"ts_code": tc, "trade_date": d,
                     "rank_num": x["rank"],
                     "price": _num(price),
                     "pct_chg": _num(pct)})
    _upsert_rows("stock_hot_rank", rows)
    print(f"[hotrank] {d} rows={len(rows)} 非本地池剔除={missed}", flush=True)
    return {"trade_date": str(d), "rows": len(rows), "missed": missed}


def sync_margin_detail(dates: list = None, days: int = 1) -> dict:
    """个股融资融券明细(沪深交易所官网, 各 1 请求/日, 无频控痛点)。
    SSE 返回含 ETF(51/56/58 开头), 只留股票(60/68); SZSE 只留 00/30。
    dates 传日期列表做历史回填, 否则取最近 days 个交易日。"""
    ak = _akshare()
    if not dates:
        dates = [str(d).replace("-", "") for d in _recent_trade_dates(days)]
    sym_map = _load_symbol_map()
    rows_total = miss_total = 0
    for ymd in dates:
        rows = []
        for fn, keep in [("stock_margin_detail_sse", ("60", "68")),
                         ("stock_margin_detail_szse", ("00", "30"))]:
            try:
                df = getattr(ak, fn)(date=ymd)
            except Exception as e:
                print(f"[margin] {ymd} {fn} 失败: {type(e).__name__} {e}", flush=True)
                continue
            time.sleep(REQ_INTERVAL)
            for x in df.to_dict(orient="records"):
                code = str(x.get("标的证券代码") or x.get("证券代码") or "").zfill(6)
                if not code.startswith(keep):
                    continue
                tc = sym_map.get(code)
                if not tc:
                    miss_total += 1
                    continue
                rows.append({"ts_code": tc, "trade_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                             "rzye": _cn_money(x.get("融资余额")),
                             "rzmre": _cn_money(x.get("融资买入额")),
                             "rqye": _cn_money(x.get("融券余额")),
                             "rqyl": _num(x.get("融券余量")),
                             "rzrqye": _cn_money(x.get("融资融券余额"))})
        _upsert_rows("stock_margin_detail", rows)
        rows_total += len(rows)
        print(f"[margin] {ymd} rows={len(rows)}", flush=True)
    print(f"[margin] 完成 {len(dates)} 日共 {rows_total} 行, 非本地池剔除 {miss_total}", flush=True)
    return {"dates": len(dates), "rows": rows_total, "missed": miss_total}

