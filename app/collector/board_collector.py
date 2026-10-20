# -*- coding: utf-8 -*-
"""板块基础信息采集（新浪行业板块体系, 需求#19, 口径见 docs/06 v1.1 已确认）
数据源:
    板块列表+快照: 新浪 vip.stock.finance.sina.com.cn/q/view/newSinaHy.php（已实测可用）
        value 为 CSV: code,name,member_cnt,avg_price,avg_chg,pct_chg,vol,amount,
                      lead_symbol,lead_pct,lead_price,lead_chg,lead_name
    板块成分股:  新浪 Market_Center.getHQNodeData（node=newSinaHy 板块代码, 分页）
    个股人气:    同花顺热榜 dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock
写入:
    stock_board_member  成分股映射（全量覆盖式刷新, 含市值/近20日涨幅快照）
    stock_board_basic   板块基础快照（每日一行, 本地聚合+新浪快照+人气）
频度: 每日盘后 1 次（--all 中置于 tags/quotes/valsnap 之后）;
    板块成分低频变化, 可用 --board --skip-members 跳过成分刷新只更快照。
"""
import json
import re
import time

import requests
from sqlalchemy import text

from app import config
from app.db import engine

HY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
NODE_DATA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                 "json_v2.php/Market_Center.getHQNodeData")
THS_HOT_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
HEADERS_SINA = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
HEADERS_THS = {"User-Agent": "Mozilla/5.0"}

_session = requests.Session()
_session.trust_env = False    # 直连，绕过系统代理（与全项目一致）


def _get(url, params=None, headers=None, tries=config.RETRY_TIMES):
    last = None
    for _ in range(tries):
        try:
            return _session.get(url, params=params, headers=headers or HEADERS_SINA,
                                timeout=15)
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _fetch_board_list() -> list[dict]:
    """新浪行业板块列表+快照 [{code,name,member_cnt,pct_chg,amount,lead_stock}]"""
    r = _get(HY_URL)
    r.encoding = "gbk"
    m = re.search(r"=\s*(\{.*\})\s*;?\s*$", r.text, re.S)
    if not m:
        raise RuntimeError("[board] newSinaHy 返回格式异常，未找到 JSON 体")
    data = json.loads(m.group(1))
    boards = []
    for key, val in data.items():
        f = str(val).split(",")
        if len(f) < 13:
            continue
        boards.append({
            "code": f[0].strip(), "name": f[1].strip(),
            "member_cnt_sina": _to_int(f[2]),
            "pct_chg": _to_float(f[5]),
            "amount": _to_float(f[7]),
            "lead_stock": f[12].strip(),
        })
    return boards


def _fetch_members(node_code: str) -> list[str]:
    """某板块节点全部成分股 6 位代码（与 tag_collector 同接口同分页口径）"""
    codes, page = [], 1
    while True:
        data = None
        for attempt in range(config.RETRY_TIMES):
            r = _get(NODE_DATA_URL, params={
                "page": page, "num": 100, "sort": "symbol", "asc": 1,
                "node": node_code, "symbol": "", "_s_r_a": "page",
            })
            if not r.text.strip():
                data = []
                break
            try:
                data = json.loads(r.text)
                break
            except json.JSONDecodeError:
                time.sleep(2 + attempt)
        if data is None:
            raise RuntimeError(f"[board] 节点 {node_code} 第 {page} 页连续返回非 JSON")
        if not data:
            break
        codes.extend(d["code"] for d in data if d.get("code"))
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.1)
    return codes


def _fetch_ths_hot() -> dict[str, dict]:
    """同花顺人气热榜 {symbol: {rate, rank}}（单次请求）。

    实测（2026-09-15）：该端点**硬性上限 100 只**（`page`/`size`/`list_type` 均无法扩大，
    `list_type=all` 与 `stock_type=all` 直接返回空）。故 49 个行业里只有约 26 个
    "有成分股在榜" → 板块人气排名只在这 26 个板块内产生名次，其余 hot_score=0 / hot_rank=NULL，
    前端按覆盖数如实标注。
    """
    r = _get(THS_HOT_URL, params={"stock_type": "a", "type": "hour",
                                  "list_type": "normal"}, headers=HEADERS_THS)
    payload = r.json()
    items = ((payload.get("data") or {}).get("stock_list")) or []
    out = {}
    for x in items:
        code = str(x.get("code") or "")
        if code:
            out[code] = {"rate": _to_float(x.get("rate")) or 0.0,
                         "rank": _to_int(x.get("order"))}
    return out


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _load_stock_map() -> dict[str, dict]:
    """symbol -> {ts_code, name, total_mv, circ_mv, list_date}（仅在市）"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT symbol, ts_code, name, total_mv, circ_mv, list_date
            FROM stock_basic WHERE is_active=1
        """)).mappings().all()
    return {r["symbol"]: dict(r) for r in rows}


def _latest_trade_date() -> str | None:
    with engine.connect() as conn:
        td = conn.execute(text("SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
    return str(td) if td else None


def _load_latest_quotes(trade_date: str) -> dict[str, dict]:
    """最新交易日行情 {ts_code: {pct_chg, amount, close}}"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ts_code, pct_chg, amount, close FROM stock_daily_quote
            WHERE trade_date=:td
        """), {"td": trade_date}).mappings().all()
    return {r["ts_code"]: dict(r) for r in rows}


def _load_chg20(trade_date: str) -> dict[str, float]:
    """近20日涨幅% {ts_code: chg20}（最新收盘 / 20个交易日前收盘 - 1）"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ts_code, trade_date, close FROM stock_daily_quote
            WHERE trade_date <= :td ORDER BY ts_code, trade_date DESC
        """), {"td": trade_date}).mappings().all()
    # MySQL 5.5 无窗口函数, Python 侧取每票最近 21 根
    from collections import defaultdict
    closes = defaultdict(list)
    for r in rows:
        if len(closes[r["ts_code"]]) < 21:
            closes[r["ts_code"]].append(r)
    out = {}
    for tc, lst in closes.items():
        if len(lst) >= 21 and lst[-1]["close"]:
            out[tc] = round((lst[0]["close"] / lst[-1]["close"] - 1) * 100, 4)
    return out


def sync_board_basic(skip_members: bool = False, limit_boards: int = None) -> dict:
    """刷新板块成分映射 + 板块基础快照。limit_boards: 调试用, 只处理前 N 个板块。"""
    boards = _fetch_board_list()
    if limit_boards:
        boards = boards[:limit_boards]
    print(f"[board] 新浪行业板块 {len(boards)} 个", flush=True)

    stock_map = _load_stock_map()
    td = _latest_trade_date()
    if not td:
        raise RuntimeError("[board] stock_daily_quote 为空, 请先跑 --quotes")
    quotes = _load_latest_quotes(td)
    chg20_map = _load_chg20(td)
    print(f"[board] 基准交易日 {td}, 行情 {len(quotes)} 只", flush=True)

    # 1) 成分股映射（全量覆盖式刷新）
    member_rows = []
    if not skip_members:
        for i, b in enumerate(boards, 1):
            syms = _fetch_members(b["code"])
            for sym in syms:
                st = stock_map.get(sym)
                if not st:
                    continue    # 已退市/不在本地池
                q = quotes.get(st["ts_code"], {})
                member_rows.append({
                    "board_code": b["code"], "ts_code": st["ts_code"],
                    "corr60": None, "rel_tag": None, "leader_tag": None,
                    "leader_score": None,
                    "total_mv": st["total_mv"], "circ_mv": st["circ_mv"],
                    "chg20": chg20_map.get(st["ts_code"]),
                })
            if i % 20 == 0:
                print(f"[board] 成分映射进度 {i}/{len(boards)}", flush=True)
            time.sleep(0.1)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM stock_board_member"))
            if member_rows:
                conn.execute(text("""
                    INSERT INTO stock_board_member
                        (board_code, ts_code, corr60, rel_tag, leader_tag,
                         leader_score, total_mv, circ_mv, chg20)
                    VALUES (:board_code, :ts_code, :corr60, :rel_tag, :leader_tag,
                            :leader_score, :total_mv, :circ_mv, :chg20)
                """), member_rows)
        print(f"[board] 成分映射刷新完成: {len(member_rows)} 条", flush=True)
    else:
        with engine.connect() as conn:
            member_rows = conn.execute(text("""
                SELECT board_code, ts_code, total_mv, circ_mv FROM stock_board_member
            """)).mappings().all()
        # 快照列跟随最新值
        with engine.begin() as conn:
            upd = []
            for r in member_rows:
                st = next((s for s in stock_map.values()
                           if s["ts_code"] == r["ts_code"]), None)
                if st:
                    upd.append({"board_code": r["board_code"], "ts_code": r["ts_code"],
                                "total_mv": st["total_mv"], "circ_mv": st["circ_mv"],
                                "chg20": chg20_map.get(r["ts_code"])})
            conn.execute(text("""
                UPDATE stock_board_member SET total_mv=:total_mv, circ_mv=:circ_mv,
                    chg20=:chg20, etl_update_time=NOW()
                WHERE board_code=:board_code AND ts_code=:ts_code
            """), upd)

    # 成员按板块聚合
    from collections import defaultdict
    members_by_board: dict[str, list] = defaultdict(list)
    for r in member_rows:
        members_by_board[r["board_code"]].append(r)

    # 2) 同花顺人气（板块人气 = 在榜成分股人气值前10均值, 不在榜计 0）
    try:
        ths = _fetch_ths_hot()
        print(f"[board] 同花顺热榜在榜 {len(ths)} 只", flush=True)
    except Exception as e:
        ths = {}
        print(f"[board] 同花顺热榜获取失败(降级为无人气): {e}", flush=True)

    tc2sym = {s["ts_code"]: s["symbol"] for s in stock_map.values()}

    basic_rows = []
    for b in boards:
        mem = members_by_board.get(b["code"], [])
        mvs = [m for m in mem if m.get("circ_mv")]
        total_mv = sum(float(m["total_mv"] or 0) for m in mem) or None
        circ_mv = sum(float(m["circ_mv"] or 0) for m in mvs) or None
        list_dates = [stock_map[tc2sym[m["ts_code"]]]["list_date"]
                      for m in mem if tc2sym.get(m["ts_code"])
                      and stock_map[tc2sym[m["ts_code"]]].get("list_date")]
        list_proxy = min(list_dates) if list_dates else None
        up_cnt = down_cnt = 0
        for m in mem:
            q = quotes.get(m["ts_code"])
            if not q or q["pct_chg"] is None:
                continue
            if q["pct_chg"] > 0:
                up_cnt += 1
            elif q["pct_chg"] < 0:
                down_cnt += 1
        rates = sorted((ths[tc2sym[m["ts_code"]]]["rate"]
                        for m in mem
                        if tc2sym.get(m["ts_code"]) in ths), reverse=True)
        hot_score = round(sum(rates[:10]) / len(rates[:10]), 4) if rates else 0.0
        basic_rows.append({
            "board_code": b["code"], "board_name": b["name"],
            "board_type": "industry", "src": "sina+local",
            "member_cnt": len(mem) or b["member_cnt_sina"],
            "total_mv": total_mv, "circ_mv": circ_mv,
            "list_proxy_date": list_proxy, "hot_rank": None,
            "hot_score": hot_score,
            "up_cnt": up_cnt, "down_cnt": down_cnt,
            "lead_stock": b["lead_stock"], "pct_chg": b["pct_chg"],
            "amount": b["amount"], "summary": None, "trade_date": td,
        })

    # 板块人气排名 = hot_score 横截面降序名次（0 分并列末位）
    scored = sorted((r for r in basic_rows if r["hot_score"] > 0),
                    key=lambda r: r["hot_score"], reverse=True)
    for rank, r in enumerate(scored, 1):
        r["hot_rank"] = rank

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO stock_board_basic
                (board_code, board_name, board_type, src, member_cnt, total_mv,
                 circ_mv, list_proxy_date, hot_rank, hot_score, up_cnt, down_cnt,
                 lead_stock, pct_chg, amount, summary, trade_date)
            VALUES (:board_code, :board_name, :board_type, :src, :member_cnt,
                    :total_mv, :circ_mv, :list_proxy_date, :hot_rank, :hot_score,
                    :up_cnt, :down_cnt, :lead_stock, :pct_chg, :amount, :summary,
                    :trade_date)
            ON DUPLICATE KEY UPDATE
                board_name=VALUES(board_name), member_cnt=VALUES(member_cnt),
                total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv),
                list_proxy_date=VALUES(list_proxy_date), hot_rank=VALUES(hot_rank),
                hot_score=VALUES(hot_score), up_cnt=VALUES(up_cnt),
                down_cnt=VALUES(down_cnt), lead_stock=VALUES(lead_stock),
                pct_chg=VALUES(pct_chg), amount=VALUES(amount),
                etl_update_time=NOW()
        """), basic_rows)

    print(f"[board] 板块快照落表 {len(basic_rows)} 行 (trade_date={td})", flush=True)
    return {"boards": len(boards), "members": len(member_rows),
            "ths_hot": len(ths), "trade_date": td}
