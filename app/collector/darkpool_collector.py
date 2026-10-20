# -*- coding: utf-8 -*-
"""暗盘数据采集（A股盘后暗线 = 大宗交易 + 龙虎榜, 需求#20, 口径见 docs/06 v1.1 已确认）
数据源: 东财 datacenter-web.eastmoney.com（报表型接口, 与被封的 push2his 不同域, 已实测可用）
    RPT_DATA_BLOCKTRADE             大宗交易逐笔（含折溢价率/买卖方营业部）
    RPT_DAILYBILLBOARD_DETAILSNEW   龙虎榜明细（买/卖/净额/成交占比/上榜原因）
    RPT_BILLBOARD_DAILYDETAILSBUY   龙虎榜买方席位（机构专用/游资识别）
    RPT_BILLBOARD_DAILYDETAILSSELL  龙虎榜卖方席位
口径说明:
    - PREMIUM_RATIO 接口返回小数（-0.0092 = -0.92%）, 落表统一 ×100 存百分比
    - 仅保留本地股票池（stock_basic）内的标的, 自动剔除 REITs/基金等 FDO 类型
    - inst_net_amt  = 席位名='机构专用' 的净买入合计（个股/日期级）
    - hot_money_net = 席位名含'拉萨' 的净买入合计（游资集散地代理口径, 已在 docs/06 注明）
频控: 请求间隔 >=0.5s（报表接口风控, docs/06 B3）
频度: 每日盘后 1 次, 默认回看 7 天增量; 首次 --darkpool --days 30 回填。
"""
import datetime as dt
import time

import requests
from sqlalchemy import text

from app import config
from app.db import engine

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HEADERS = {"User-Agent": "Mozilla/5.0",
           "Referer": "https://data.eastmoney.com/"}
PAGE_SIZE = 500
REQ_INTERVAL = 0.5      # 报表接口风控间隔（docs/06 B3）

_session = requests.Session()
_session.trust_env = False


def _fetch_report(report_name: str, start: str, end: str,
                  date_field: str = "TRADE_DATE") -> list[dict]:
    """分页拉取报表 [start, end] 区间全部行（低频, 间隔 >=0.5s）"""
    rows, page = [], 1
    while True:
        params = {
            "reportName": report_name, "columns": "ALL",
            "pageNumber": page, "pageSize": PAGE_SIZE,
            "sortColumns": date_field, "sortTypes": -1,
            "source": "DataCenter", "client": "WEB",
            "filter": f"({date_field}>='{start}')({date_field}<='{end}')",
        }
        last = None
        for _ in range(config.RETRY_TIMES):
            try:
                r = _session.get(DC_URL, params=params, headers=HEADERS, timeout=20)
                payload = r.json()
                break
            except Exception as e:
                last = e
                time.sleep(2)
        else:
            raise last
        result = payload.get("result") or {}
        data = result.get("data") or []
        rows.extend(data)
        total_pages = result.get("pages") or 1
        if page >= total_pages or not data:
            break
        page += 1
        time.sleep(REQ_INTERVAL)
    return rows


def _date_range(days: int, start_date: str = None, end_date: str = None):
    if start_date:
        return start_date, (end_date or start_date)
    end = dt.date.today()
    start = end - dt.timedelta(days=days * 2)   # 自然日放大, 覆盖非交易日
    return str(start), str(end)


def _load_ts_codes() -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT ts_code FROM stock_basic WHERE is_active=1")).fetchall()}


def _norm_date(v: str) -> str:
    return str(v)[:10]


def sync_block_trade(days: int = 7, start_date: str = None,
                     end_date: str = None) -> dict:
    """大宗交易逐笔采集（幂等, 唯一键 股+日+价+量）"""
    start, end = _date_range(days, start_date, end_date)
    raw = _fetch_report("RPT_DATA_BLOCKTRADE", start, end)
    valid = _load_ts_codes()
    rows, skipped = [], 0
    for x in raw:
        tc = str(x.get("SECUCODE") or "")
        if tc not in valid:
            skipped += 1
            continue
        prem = x.get("PREMIUM_RATIO")
        rows.append({
            "ts_code": tc, "trade_date": _norm_date(x.get("TRADE_DATE")),
            "deal_price": x.get("DEAL_PRICE"),
            "deal_volume": x.get("DEAL_VOLUME"),
            "deal_amt": x.get("DEAL_AMT"),
            "premium_ratio": round(prem * 100, 4) if prem is not None else None,
            "close_price": x.get("CLOSE_PRICE"),
            "buyer_name": (x.get("BUYER_NAME") or "")[:128] or None,
            "seller_name": (x.get("SELLER_NAME") or "")[:128] or None,
        })
    with engine.begin() as conn:
        B = 1000
        for i in range(0, len(rows), B):
            conn.execute(text("""
                INSERT IGNORE INTO stock_block_trade
                    (ts_code, trade_date, deal_price, deal_volume, deal_amt,
                     premium_ratio, close_price, buyer_name, seller_name)
                VALUES (:ts_code, :trade_date, :deal_price, :deal_volume,
                        :deal_amt, :premium_ratio, :close_price, :buyer_name,
                        :seller_name)
            """), rows[i:i + B])
    print(f"[darkpool] 大宗交易 [{start}~{end}] 原始 {len(raw)} 行, "
          f"落表 {len(rows)} 行, 非本地池剔除 {skipped}", flush=True)
    return {"raw": len(raw), "rows": len(rows), "skipped": skipped,
            "range": f"{start}~{end}"}


def sync_billboard(days: int = 7, start_date: str = None,
                   end_date: str = None) -> dict:
    """龙虎榜明细 + 席位聚合（机构专用/拉萨游资）"""
    start, end = _date_range(days, start_date, end_date)
    raw = _fetch_report("RPT_DAILYBILLBOARD_DETAILSNEW", start, end)
    valid = _load_ts_codes()
    rows, skipped = [], 0
    for x in raw:
        tc = str(x.get("SECUCODE") or "")
        if tc not in valid:
            skipped += 1
            continue
        rows.append({
            "ts_code": tc, "trade_date": _norm_date(x.get("TRADE_DATE")),
            "reason": (x.get("EXPLANATION") or "")[:255] or None,
            "buy_amt": x.get("BILLBOARD_BUY_AMT"),
            "sell_amt": x.get("BILLBOARD_SELL_AMT"),
            "net_amt": x.get("BILLBOARD_NET_AMT"),
            "deal_amt_ratio": x.get("DEAL_AMOUNT_RATIO"),
            "inst_net_amt": None, "hot_money_net": None,
        })
    with engine.begin() as conn:
        B = 1000
        for i in range(0, len(rows), B):
            conn.execute(text("""
                INSERT IGNORE INTO stock_billboard
                    (ts_code, trade_date, reason, buy_amt, sell_amt, net_amt,
                     deal_amt_ratio, inst_net_amt, hot_money_net)
                VALUES (:ts_code, :trade_date, :reason, :buy_amt, :sell_amt,
                        :net_amt, :deal_amt_ratio, :inst_net_amt,
                        :hot_money_net)
            """), rows[i:i + B])
    print(f"[darkpool] 龙虎榜 [{start}~{end}] 原始 {len(raw)} 行, "
          f"落表 {len(rows)} 行, 非本地池剔除 {skipped}", flush=True)

    # 席位聚合: 买方+卖方席位报表, 按 股+日 汇总 机构专用 / 拉萨系 净额
    seat_stats: dict[tuple, dict] = {}
    for rn in ("RPT_BILLBOARD_DAILYDETAILSBUY", "RPT_BILLBOARD_DAILYDETAILSSELL"):
        seats = _fetch_report(rn, start, end)
        time.sleep(REQ_INTERVAL)
        for x in seats:
            tc = str(x.get("SECUCODE") or "")
            if tc not in valid:
                continue
            key = (tc, _norm_date(x.get("TRADE_DATE")))
            st = seat_stats.setdefault(key, {"inst": 0.0, "hot": 0.0})
            net = float(x.get("NET") or 0)
            name = str(x.get("OPERATEDEPT_NAME") or "")
            if name == "机构专用":
                st["inst"] += net
            if "拉萨" in name:
                st["hot"] += net
    with engine.begin() as conn:
        upd = [{"inst": round(v["inst"], 2), "hot": round(v["hot"], 2),
                "tc": k[0], "td": k[1]}
               for k, v in seat_stats.items()]
        conn.execute(text("""
            UPDATE stock_billboard SET inst_net_amt=:inst, hot_money_net=:hot
            WHERE ts_code=:tc AND trade_date=:td
        """), upd)
    print(f"[darkpool] 席位聚合完成: {len(seat_stats)} 个 股+日 组合", flush=True)
    return {"raw": len(raw), "rows": len(rows), "skipped": skipped,
            "seat_pairs": len(seat_stats), "range": f"{start}~{end}"}


# ---------------------------------------------------------------- 派生指标

def _zscores(values: dict[str, float]) -> dict[str, float]:
    """横截面 z-score（样本 <5 时全体记 0, 防小样本失真）"""
    n = len(values)
    if n < 5:
        return {k: 0.0 for k in values}
    mean = sum(values.values()) / n
    var = sum((v - mean) ** 2 for v in values.values()) / n
    sd = var ** 0.5
    if sd == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / sd for k, v in values.items()}


def refresh_dark_indicator(trade_date: str = None, window_days: int = 10,
                           full: bool = False) -> dict:
    """按 B6 口径重算暗盘派生指标。

    window_days: 重算最近 N 个"有数据的交易日"（日常增量用, 默认 10）；
    full=True: 重算库内**全部**有数据的交易日（历史回填后用）。
    实现说明: 原实现按 (日期 × 个股 × 全表行) 三重扫描, 回填 1 年后数据量放大
    一个数量级会退化成不可用; 这里改为**按个股建立时间索引**后只扫各自的近 5 日窗口。
    """
    with engine.connect() as conn:
        dates = [str(r[0]) for r in conn.execute(text("""
            SELECT DISTINCT trade_date FROM (
                SELECT trade_date FROM stock_block_trade
                UNION SELECT trade_date FROM stock_billboard
            ) t ORDER BY 1 DESC
        """)).fetchall()]
        if trade_date:
            dates = [d for d in dates if d <= trade_date]
        if not full:
            dates = dates[:window_days]
        if not dates:
            print("[darkind] 无大宗/龙虎榜数据, 跳过")
            return {"dates": 0, "rows": 0}
        min_d, max_d = dates[-1], dates[0]
        # 往前多取 8 个自然日, 让区间首日的"近5日"窗口不被截断
        q_start = (dt.date.fromisoformat(min_d) - dt.timedelta(days=8)).isoformat()

        block_rows = conn.execute(text("""
            SELECT ts_code, trade_date, deal_amt, premium_ratio
            FROM stock_block_trade WHERE trade_date BETWEEN :a AND :b
        """), {"a": q_start, "b": max_d}).mappings().all()
        bb_rows = conn.execute(text("""
            SELECT ts_code, trade_date, net_amt,
                   MAX(inst_net_amt) inst_net, MAX(hot_money_net) hot_net,
                   MAX(deal_amt_ratio) ratio
            FROM stock_billboard WHERE trade_date BETWEEN :a AND :b
            GROUP BY ts_code, trade_date
        """), {"a": q_start, "b": max_d}).mappings().all()
        quote_rows = conn.execute(text("""
            SELECT ts_code, trade_date, amount FROM stock_daily_quote
            WHERE trade_date BETWEEN :a AND :b
        """), {"a": q_start, "b": max_d}).mappings().all()
        mv_rows = conn.execute(text(
            "SELECT ts_code, circ_mv FROM stock_basic WHERE is_active=1")).fetchall()

    circ_mv = {tc: float(mv) for tc, mv in mv_rows if mv}
    day_amt = {(r["ts_code"], str(r["trade_date"])): float(r["amount"])
               for r in quote_rows if r["amount"]}

    # ---- 按 (股, 日) 聚合, 并建立"按个股"的时间索引 ----
    block_day: dict[tuple, dict] = {}
    for r in block_rows:
        k = (r["ts_code"], str(r["trade_date"]))
        v = block_day.setdefault(k, {"amt": 0.0, "wacc": 0.0})
        amt = float(r["deal_amt"] or 0)
        v["amt"] += amt
        if r["premium_ratio"] is not None:
            v["wacc"] += amt * float(r["premium_ratio"])
    bb_day: dict[tuple, dict] = {
        (r["ts_code"], str(r["trade_date"])): {
            "net": float(r["net_amt"] or 0),
            "inst": float(r["inst_net"] or 0),
            "hot": float(r["hot_net"] or 0),
            "ratio": float(r["ratio"] or 0),
        } for r in bb_rows}

    blk_by_code: dict[str, list] = {}
    for (tc, dd), v in block_day.items():
        blk_by_code.setdefault(tc, []).append((dd, v))
    for lst in blk_by_code.values():
        lst.sort()
    bb_by_code: dict[str, dict] = {}
    for (tc, dd), v in bb_day.items():
        bb_by_code.setdefault(tc, {})[dd] = v
    codes_by_date: dict[str, set] = {}
    for tc, dd in block_day:
        codes_by_date.setdefault(dd, set()).add(tc)
    for tc, dd in bb_day:
        codes_by_date.setdefault(dd, set()).add(tc)

    out = []
    for d in dates:
        win_start = (dt.date.fromisoformat(d) - dt.timedelta(days=8)).isoformat()
        recs = {}
        for tc in codes_by_date.get(d, ()):
            b0 = block_day.get((tc, d), {"amt": 0.0, "wacc": 0.0})
            w5 = [(dd, v) for dd, v in blk_by_code.get(tc, [])
                  if win_start <= dd <= d][-5:]
            b_amt5 = sum(v["amt"] for _, v in w5)
            b_wacc5 = sum(v["wacc"] for _, v in w5)
            bb0 = bb_day.get((tc, d))
            bb5 = sum(x["net"] for dd, x in bb_by_code.get(tc, {}).items()
                      if win_start <= dd <= d)
            recs[tc] = {
                "block_amt": b0["amt"],
                "block_discount": (b0["wacc"] / b0["amt"]) if b0["amt"] else None,
                "block_days5": len([1 for _, v in w5 if v["amt"] > 0]),
                "block_discount5": (b_wacc5 / b_amt5) if b_amt5 else None,
                "bb_net_amt": bb0["net"] if bb0 else None,
                "bb_net_amt5": bb5 or None,
                "_inst": bb0["inst"] if bb0 else 0.0,
                "_hot": bb0["hot"] if bb0 else 0.0,
                "_ratio": bb0["ratio"] if bb0 else 0.0,
                "_amt_ratio": (b0["amt"] / day_amt[(tc, d)]
                               if day_amt.get((tc, d)) else 0.0),
                "_bbmv": (bb5 / circ_mv[tc] * 100
                          if tc in circ_mv and circ_mv[tc] else 0.0),
            }
        if not recs:
            continue
        z1 = _zscores({k: v["block_discount5"] or 0.0 for k, v in recs.items()})
        z2 = _zscores({k: v["_bbmv"] for k, v in recs.items()})
        z3 = _zscores({k: v["_inst"] for k, v in recs.items()})
        z4 = _zscores({k: v["_amt_ratio"] * 100 for k, v in recs.items()})
        for tc, v in recs.items():
            score = round(0.35 * z1[tc] + 0.25 * z2[tc]
                          + 0.20 * z3[tc] + 0.20 * z4[tc], 4)
            disc = v["block_discount"]
            # 2026-09-15 口径修订：原「溢价>2% 且机构净买>0」全区间 0 命中
            # （大宗溢价仅占 4.7%，与龙虎榜同日同股几乎不共存）。改为
            # 「大宗折价成交(<0) 且当日机构席位净买>0」：折价出让 + 机构承接
            # = 机构多渠道吸筹。实测 103 行 / 85 只。
            if disc is not None and disc < 0 and v["_inst"] > 0:
                sig = "机构吸筹"
            elif disc is not None and disc < -8 and v["block_days5"] >= 2:
                sig = "减持出货"
            elif v["_hot"] > 0 and v["_ratio"] >= 20:
                sig = "游资活跃"
            else:
                sig = "无"
            out.append({
                "ts_code": tc, "trade_date": d,
                "block_amt": v["block_amt"] or None,
                "block_discount": (round(v["block_discount"], 4)
                                   if v["block_discount"] is not None else None),
                "block_days5": v["block_days5"],
                "block_discount5": (round(v["block_discount5"], 4)
                                    if v["block_discount5"] is not None else None),
                "bb_net_amt": v["bb_net_amt"],
                "bb_net_amt5": (round(v["bb_net_amt5"], 2)
                                if v["bb_net_amt5"] is not None else None),
                "dark_score": score, "signal_tag": sig,
            })

    with engine.begin() as conn:
        B = 1000
        for i in range(0, len(out), B):
            conn.execute(text("""
                INSERT INTO stock_dark_pool_indicator
                    (ts_code, trade_date, block_amt, block_discount, block_days5,
                     block_discount5, bb_net_amt, bb_net_amt5, dark_score,
                     signal_tag)
                VALUES (:ts_code, :trade_date, :block_amt, :block_discount,
                        :block_days5, :block_discount5, :bb_net_amt,
                        :bb_net_amt5, :dark_score, :signal_tag)
                ON DUPLICATE KEY UPDATE
                    block_amt=VALUES(block_amt),
                    block_discount=VALUES(block_discount),
                    block_days5=VALUES(block_days5),
                    block_discount5=VALUES(block_discount5),
                    bb_net_amt=VALUES(bb_net_amt),
                    bb_net_amt5=VALUES(bb_net_amt5),
                    dark_score=VALUES(dark_score), signal_tag=VALUES(signal_tag)
            """), out[i:i + B])
    sig_cnt = {}
    for r in out:
        sig_cnt[r["signal_tag"]] = sig_cnt.get(r["signal_tag"], 0) + 1
    print(f"[darkind] 重算 {len(dates)} 个交易日, 落表 {len(out)} 行, "
          f"信号分布 {sig_cnt}", flush=True)
    return {"dates": len(dates), "rows": len(out), "signals": sig_cnt}
