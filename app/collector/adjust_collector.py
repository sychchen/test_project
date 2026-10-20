# -*- coding: utf-8 -*-
"""复权因子事件采集（2026-09-20, 性价比梯队1: 修复不复权 K 线的回测口径问题）

背景:
    stock_daily_quote 为不复权口径且 pre_close=close.shift(1) 派生 —— 除权日
    当日的 pct_chg 会把分红/送转缺口算成跌幅。本采集器只落"除权变化点"事件表
    stock_adjust_factor（~1-2 事件/股/年, 空间极小）, 供回测/策略在跨除权日时
    换算复权收益:
        adjusted_return(t) = naive_return(t) / factor_after(t) - 1  (t=事件日)
        factor_after(t) = [raw(t)/raw(t-1)] / [qfq(t)/qfq(t-1)], 非事件日=1。
    口径修正(2026-09-20 实测): 腾讯 qfq 为【加法前复权】, raw/qfq 比值段内
    不恒定(随价格漂移), 旧"比值跳变"检测 84% 是漂移噪声 —— 已改为自适应
    signal(qfq收益率-raw收益率)跳变检测, 阈值 max(6×p50漂移, 3×舍入界, 0.1%)。
    注意: 腾讯 qfqday 单次最多返回 ~640 根, 事件覆盖最近 ~2.6 年。

两条路径:
    1) seed(一次性, --adjseed): 腾讯 fqkline 前复权(qfqday) vs 库内不复权 close,
       逐股 1 请求覆盖最近 ~1023 根(与日线历史同深度), 检测因子跳变点。
       单线程 0.15s/股, 全市场约 20-25 分钟, 建议后台任务。
    2) daily(日常, --adjdaily, 零请求预警): 库内派生涨跌幅 vs 同花顺官方涨跌幅
       (stock_money_flow_ths.pct_chg, --all 每日已采)偏差 > 0.2pp 的股票, 只对
       这些候选(~0-50只/日)逐股 1 请求 qfq 验证确认跳变后落库, source='daily'。

腾讯 qfq 实测(2026-09-20): param 末段 'qfq' 返回 key='qfqday', 与不复权 'day'
同构; 茅台 2025-06 分红除权后 raw/qfq 比值跳变清晰可检。
"""
import datetime as dt
import time
from pathlib import Path

from sqlalchemy import text

from app.db import engine
from app.collector.quote_collector import (
    _session, TX_KLINE_URL, TX_HEADERS, TxWafError, _sina_code)

REQ_INTERVAL = 0.15     # 腾讯 fqkline 保守间隔(高于 quote_collector 的节奏, seed 是长任务)
REL_PRE_THRESHOLD = 0.005  # 隐含昨收 vs 库内昨收 相对偏差预警阈值(0.5%, 消 THS 舍入噪声)

# seed 断点进度文件: 记录已完整处理(含零事件)的 ts_code, 使后台任务被杀后
# 重启能直接跳到断点, 不重扫零事件股票(它们不会出现在 stock_adjust_factor 表里)
SEED_PROGRESS_FILE = Path(__file__).resolve().parents[2] / "oneoff" / "adjseed_progress.txt"


def _fetch_tx_qfq(symbol: str, count: int) -> list:
    """腾讯 fqkline 前复权日K, 返回 [[日期, 开, 收, 高, 低, 量], ...]"""
    sc = _sina_code(symbol)
    r = _session().get(TX_KLINE_URL, params={
        "param": f"{sc},day,,,{count},qfq",
    }, headers=TX_HEADERS, timeout=10)
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        raise TxWafError(f"waf status={r.status_code}")
    data = (r.json().get("data") or {}).get(sc) or {}
    return data.get("qfqday") or data.get("day") or []


def _detect_events(pairs: list) -> list:
    """pairs: [(date, raw_close, qfq_close), ...] 按日期升序, 有效行。
    返回 [(ex_date, factor_before, factor_after, adj_pct), ...]

    口径(2026-09-20 实测): 腾讯 qfq 为【加法前复权】, gap = raw - qfq = 未来累计
    现金分红, 除权日之间段内恒定(茅台实测 0→121元), 价格漂移不进入 gap。

    检测: 残差 r(t) = [gap(t) - gap(t-1)] / qfq(t-1)
      - 纯现金分红股: 非除权日 r≈0(仅量化噪声 ~0.0055/qfq), 除权日 r=股息率
      - 含送转股: gap 段内含 raw(1-1/S) 项会随价格漂移 -> 自适应兜底
        thr = max(4×0.0055/qfq, 0.1%); 若 |r|<thr 的天数占比 <95%(漂移股),
        阈值提升为 max(8×p50(|r|), 1%)(只抓大事件, 宁缺勿滥)

    factor 口径(回测换算): adjusted_ret(t) = naive_ret(t) / factor_after(t)
        factor_after(t) = [raw(t)/raw(t-1)] / [qfq(t)/qfq(t-1)]
        对现金分红与送转均精确(乘法修正系数), 非事件日=1。
    """
    valid = [(str(d), float(c), float(q)) for d, c, q in pairs if c and q]
    if len(valid) < 2:      # daily 路径只传除权日+前一日共2点, 1个signal即可判
        return []
    recs = []             # (idx, r, thr_base)
    for i in range(1, len(valid)):
        _, raw1, q1 = valid[i - 1]
        _, raw0, q0 = valid[i]
        r = (raw0 - q0 - (raw1 - q1)) / q1
        recs.append((i, r, 4 * 0.0055 / q1))
    abs_r = sorted(abs(r) for _, r, _ in recs)
    p50 = abs_r[len(abs_r) // 2]
    calm_ratio = sum(1 for x in abs_r if x < 0.001) / len(abs_r)
    # 样本太少(如 daily 路径仅1个signal)时中位数不稳健, 禁用自适应(靠舍入上界+1%下限)
    adaptive = calm_ratio < 0.95 and len(recs) >= 20
    events = []
    for i, r, thr_base in recs:
        thr = max(8 * p50, 0.01) if adaptive else max(thr_base, 0.001)
        if abs(r) > thr:
            d0 = valid[i][0]
            _, raw1, q1 = valid[i - 1]
            _, raw0, q0 = valid[i]
            factor_after = round((raw0 / raw1) / (q0 / q1), 8)
            events.append((d0, 1.0, factor_after, round(r * 100, 4)))
    return events


def _upsert_events(ts_code: str, events: list, source: str) -> int:
    if not events:
        return 0
    rows = [{"tc": ts_code, "ex": e[0], "fb": e[1], "fa": e[2],
             "ap": e[3], "src": source} for e in events]
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO stock_adjust_factor
                (ts_code, ex_date, factor_before, factor_after, adj_pct, source)
            VALUES (:tc, :ex, :fb, :fa, :ap, :src)
            ON DUPLICATE KEY UPDATE
                factor_before=VALUES(factor_before),
                factor_after=VALUES(factor_after),
                adj_pct=VALUES(adj_pct), source=VALUES(source),
                etl_update_time=NOW()
        """), rows)
    return len(rows)


def _waf_cooldown(seconds: int = 300):
    """腾讯 WAF 熔断冷却(经验: 拦截 30min+ 但 300s 后常已解除)。
    倒计时逐秒输出: 纯 sleep 会让窗口 5 分钟无输出, 双击 bat 的用户会误以为卡死而关窗,
    导致流水线后半段步骤全部没跑(2026-09-23 19:53 实踩)。"""
    print(f"[adjfactor] 腾讯WAF拦截, 冷却 {seconds}s 后重试(请勿关闭窗口)...", flush=True)
    for remain in range(seconds, 0, -10):
        print(f"[adjfactor] 冷却中... 剩余 {remain}s", flush=True)
        time.sleep(min(10, remain))


def seed_adjust_factors(limit: int = None) -> dict:
    """一次性回填: 逐股拉 qfq 与库内不复权 close 比对, 检测除权变化点。

    幂等: 已有 seed 事件的股票默认跳过(加 --adjseed-force 可强制刷新, 此处不实现,
    删表重跑即可)。单线程长任务, 建议后台执行。
    断点: oneoff/adjseed_progress.txt 记录已处理股票(含零事件), 后台任务被杀后
    重启直接跳到断点, 不重扫。强制重跑需删除该进度文件(及可选清表)。
    WAF 策略: 拦截 -> 冷却 300s 重试一次 -> 仍拦截则中止本轮(已入库进度保留, 重跑续传)。
    """
    with engine.connect() as conn:
        stocks = conn.execute(text(
            "SELECT symbol, ts_code FROM stock_basic WHERE is_active=1 "
            "ORDER BY ts_code")).fetchall()
        seeded = {r[0] for r in conn.execute(text(
            "SELECT DISTINCT ts_code FROM stock_adjust_factor")).fetchall()}

    done = set()
    if SEED_PROGRESS_FILE.exists():
        done = {ln.strip() for ln in
                SEED_PROGRESS_FILE.read_text(encoding="utf-8").splitlines()
                if ln.strip()}

    def _mark_done(tc: str) -> None:
        with SEED_PROGRESS_FILE.open("a", encoding="utf-8") as pf:
            pf.write(tc + "\n")

    if limit:
        stocks = stocks[:limit]
    ok = skip = empty = failed = ev_total = 0
    failed_list = []
    t0 = time.time()
    aborted = False
    for idx, (sym, tc) in enumerate(stocks, 1):
        if tc in seeded or tc in done:
            skip += 1
            continue
        if aborted:
            break
        # 库内不复权 close 序列(与日线历史同深度即可, 逐股查询避免 320 万行整表进内存)
        with engine.connect() as conn:
            raw_seq = [(str(d), float(c)) for d, c in conn.execute(text(
                "SELECT trade_date, close FROM stock_daily_quote "
                "WHERE ts_code=:tc AND close IS NOT NULL ORDER BY trade_date"),
                {"tc": tc}).fetchall()]
        qfq = None
        for attempt in (1, 2):
            try:
                qfq = _fetch_tx_qfq(sym, 1023)
                break
            except TxWafError as e:
                if attempt == 1:
                    _waf_cooldown()
                else:
                    failed += 1
                    failed_list.append(f"{tc}:{e}")
                    aborted = True   # 仍在拦截: 中止本轮, 保护配额, 重跑续传
            except Exception as e:
                failed += 1
                failed_list.append(f"{tc}:{type(e).__name__}")
                time.sleep(REQ_INTERVAL)
                break
        if qfq is None:
            continue
        if not qfq:
            # 腾讯偶发返回 200+合法JSON 但空列表(风控软限流), 重试一次
            time.sleep(2)
            try:
                qfq = _fetch_tx_qfq(sym, 1023)
            except Exception:
                pass
            time.sleep(REQ_INTERVAL)
        if not qfq:
            empty += 1
            _mark_done(tc)   # 请求成功但无数据: 也视为已处理, 避免重启重扫
            continue
        qfq_map = {str(row[0]): float(row[2]) for row in qfq if len(row) > 2}
        pairs = [(d, c, qfq_map.get(d)) for d, c in raw_seq]
        pairs = [p for p in pairs if p[2]]
        events = _detect_events(pairs)
        ev_total += _upsert_events(tc, events, "seed")
        _mark_done(tc)
        ok += 1
        if idx % 200 == 0:
            print(f"[adjseed] progress {idx}/{len(stocks)} ok={ok} "
                  f"events={ev_total} failed={failed} "
                  f"elapsed={int(time.time() - t0)}s", flush=True)

    print(f"[adjseed] 完成: 池={len(stocks)} ok={ok} skip(已seed)={skip} "
          f"empty={empty} failed={failed} events={ev_total} "
          f"用时={int(time.time() - t0)}s", flush=True)
    if failed_list:
        print(f"[adjseed] 失败样例: {failed_list[:5]}", flush=True)
    return {"total": len(stocks), "ok": ok, "skip": skip, "empty": empty,
            "failed": failed, "events": ev_total, "failed_list": failed_list}


def refresh_adjust_events(days: int = 2, max_verify: int = 50) -> dict:
    """日常增量: 零请求预警(隐含昨收 vs 库内昨收) -> 候选逐股 qfq 验证。

    预警口径: THS 官方 close 与 pct_chg 反推除权后昨收 implied_pre = close/(1+pct/100),
    与库内派生昨收(dq.pre_close = 前日不复权 close)的相对偏差 > 0.5% 判为候选。
    相对偏差口径可消掉 THS 两位小数舍入在低价股上的噪声(绝对涨跌幅差口径会误报 500+/日)。
    days: 回看最近 N 个交易日(默认2, 容忍 thsmoney 与 quotes 跑批先后差)。
    max_verify: 单轮最多验证候选数(防 WAF), 按偏差降序取; 超出留给下一轮。
    WAF 策略: 拦截 -> 冷却 300s 重试一次 -> 仍拦截则中止本轮(候选天然会进下一轮)。
    """
    with engine.connect() as conn:
        cands = conn.execute(text("""
            SELECT dq.ts_code, dq.trade_date, dq.pre_close,
                   ths.close / (1 + ths.pct_chg / 100) AS implied_pre,
                   ABS(dq.pre_close - ths.close / (1 + ths.pct_chg / 100))
                       / NULLIF(ths.close / (1 + ths.pct_chg / 100), 0) AS rel_diff
            FROM stock_daily_quote dq
            JOIN stock_money_flow_ths ths
              ON ths.ts_code = dq.ts_code AND ths.trade_date = dq.trade_date
            WHERE dq.pre_close IS NOT NULL AND dq.pre_close > 0
              AND ths.pct_chg IS NOT NULL AND ths.pct_chg > -99
              AND ths.close IS NOT NULL AND ths.close > 0
              AND dq.trade_date >= (
                    SELECT MAX(trade_date) FROM (
                        SELECT DISTINCT trade_date FROM stock_daily_quote
                        ORDER BY trade_date DESC LIMIT :ndays) t)
              AND ABS(dq.pre_close - ths.close / (1 + ths.pct_chg / 100))
                  / NULLIF(ths.close / (1 + ths.pct_chg / 100), 0) > :thr
            ORDER BY rel_diff DESC
            LIMIT :maxv
        """), {"ndays": days, "thr": REL_PRE_THRESHOLD, "maxv": max_verify}).fetchall()
        symbols = dict(conn.execute(text(
            "SELECT ts_code, symbol FROM stock_basic WHERE is_active=1")).fetchall())
    if not cands:
        print(f"[adjdaily] 最近{days}个交易日无隐含昨收偏差预警, 0 候选", flush=True)
        return {"candidates": 0, "events": 0, "checked": 0, "failed": 0}

    events_total = checked = failed = 0
    aborted = False
    for tc, ex_date, pre_close, implied_pre, rel_diff in cands:
        if aborted:
            break
        sym = symbols.get(tc)
        if not sym:
            continue
        with engine.connect() as conn:
            seq = [(str(d), float(c)) for d, c in conn.execute(text(
                "SELECT trade_date, close FROM stock_daily_quote "
                "WHERE ts_code=:tc AND close IS NOT NULL "
                "ORDER BY trade_date DESC LIMIT 10"), {"tc": tc}).fetchall()]
        if len(seq) < 2:
            continue
        qfq = None
        for attempt in (1, 2):
            try:
                qfq = _fetch_tx_qfq(sym, 15)
                break
            except TxWafError as e:
                if attempt == 1:
                    _waf_cooldown()
                else:
                    failed += 1
                    aborted = True   # 仍在拦截: 中止本轮保护配额, 剩余候选下一轮再验
            except Exception as e:
                failed += 1
                print(f"[adjdaily] {tc} qfq 拉取失败: {type(e).__name__} {e}", flush=True)
                time.sleep(REQ_INTERVAL)
                break
        if qfq is None:
            continue
        time.sleep(REQ_INTERVAL)
        qfq_map = {str(r[0]): float(r[2]) for r in qfq if len(r) > 2}
        raw_map = dict(seq)
        dates = sorted(raw_map, reverse=True)            # 降序: [0]=最新日
        if str(ex_date) not in dates:
            continue
        i = dates.index(str(ex_date))
        if i + 1 >= len(dates):
            continue
        d0, d1 = dates[i], dates[i + 1]          # 除权日 / 前一交易日
        if d0 not in qfq_map or d1 not in qfq_map:
            continue
        raw0, raw1 = raw_map[d0], raw_map[d1]
        # _detect_events 要求升序(变化点落在后一日期=除权日 d0)
        pairs = [(d1, raw1, qfq_map[d1]), (d0, raw0, qfq_map[d0])]
        ev = _detect_events(pairs)
        # 只认"当日跳变": 若两日比值一致说明非除权(数据噪声), 不落库
        if ev and ev[0][0] == d0:
            events_total += _upsert_events(tc, ev, "daily")
            print(f"[adjdaily] {tc} {d0} 除权因子跳变 {ev[0][1]} -> {ev[0][2]} "
                  f"({ev[0][3]}%)", flush=True)
        checked += 1

    print(f"[adjdaily] 候选={len(cands)} 验证={checked} 新事件={events_total} "
          f"failed={failed}", flush=True)
    return {"candidates": len(cands), "checked": checked,
            "events": events_total, "failed": failed}
