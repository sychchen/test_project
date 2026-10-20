# -*- coding: utf-8 -*-
"""概念板块采集（东方财富）
数据源: 东方财富 push2delay.eastmoney.com clist（已实测可用）
    板块列表: fs=m:90+t:3+f:!50  概念板块约500个（含 PCB/CPO概念/液冷服务器/算力概念 等热点）
    板块成分: fs=b:BKxxxx        单页上限100，分页
写入: stock_basic.concepts（与新浪概念合并去重，东财概念排前，上限 MAX_CONCEPTS 个）
背景: 新浪 getHQNodes 概念体系陈旧、覆盖差（如中际旭创仅命中"黄河三角"），
    缺 PCB/CPO 等热点概念；东财 push2 断连、push2his 并发封IP，
    唯 push2delay 延时通道可用（与 moneyflow_collector 同通道）。
频度: 板块构成低频变化，建议每周或在 --tags 之后执行；--all 中置于 tags 之后。
"""
import time
from collections import defaultdict

import requests
from sqlalchemy import text

from app import config
from app.db import engine

EM_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
HEADERS = {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
BOARD_FS = "m:90+t:3+f:!50"   # 东财概念板块全集
PAGE_SIZE = 100               # 该接口单页上限100（传更大值会被截断，已实测）
MAX_CONCEPTS = 40             # 每股概念合并上限（防 VARCHAR(1024) 溢出）


def _is_stable_board(name: str) -> bool:
    """过滤动态情绪/榜单类板块（每日变化，会过期且挤占概念名额）"""
    return not (name.startswith("昨日") or "热股" in name or name.endswith("表现"))

_session = requests.Session()
_session.trust_env = False    # 直连，绕过系统代理


def _get(params: dict) -> dict:
    last = None
    for _ in range(config.RETRY_TIMES):
        try:
            r = _session.get(EM_URL, params=params, headers=HEADERS, timeout=15)
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _fetch_boards() -> list[tuple[str, str]]:
    """概念板块列表 [(板块名, BK代码)]"""
    boards, page = [], 1
    while True:
        payload = _get({"pn": page, "pz": PAGE_SIZE, "po": 1, "np": 1, "fltt": 2,
                        "invt": 2, "fid": "f3", "fs": BOARD_FS, "fields": "f12,f14"})
        data = payload.get("data") if isinstance(payload, dict) else None
        diff = (data or {}).get("diff") or []
        boards += [(x["f14"], x["f12"]) for x in diff if x.get("f12") and x.get("f14")]
        if len(diff) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.2)
    return boards


def _fetch_members(bk_code: str) -> list[str]:
    """某概念板块全部成分股代码（6位数字）"""
    codes, page = [], 1
    while True:
        payload = _get({"pn": page, "pz": PAGE_SIZE, "po": 1, "np": 1, "fltt": 2,
                        "invt": 2, "fid": "f3", "fs": f"b:{bk_code}", "fields": "f12"})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            break
        diff = data.get("diff") or []
        codes += [str(x["f12"]) for x in diff if x.get("f12")]
        if not diff or page * PAGE_SIZE >= data.get("total", 0):
            break
        page += 1
        time.sleep(0.15)
    return codes


def sync_em_concepts(limit_boards: int = None) -> dict:
    """全量刷新东财概念标签（与新浪概念合并）。limit_boards: 调试用，只采前 N 个板块。"""
    boards = _fetch_boards()
    boards = [(n, c) for n, c in boards if _is_stable_board(n)]
    if limit_boards:
        boards = boards[:limit_boards]
    print(f"[emconcepts] 概念板块 {len(boards)} 个（已过滤动态情绪/榜单板块）", flush=True)

    em_map: dict[str, list] = defaultdict(list)
    for i, (name, bk) in enumerate(boards, 1):
        for sym in _fetch_members(bk):
            em_map[sym].append(name)
        if i % 50 == 0:
            print(f"[emconcepts] 板块进度 {i}/{len(boards)}", flush=True)
        time.sleep(0.15)
    print(f"[emconcepts] 成分映射完成: {len(em_map)} 只有东财概念", flush=True)

    with engine.connect() as conn:
        stocks = conn.execute(text(
            "SELECT ts_code, symbol, concepts FROM stock_basic WHERE is_active=1"
        )).fetchall()

    # 东财概念在前（热点辨识度高），旧概念（新浪+历史残留）过滤动态板块后去重追加
    rows = []
    for ts_code, symbol, old in stocks:
        em_list = em_map.get(symbol, [])
        merged = em_list + [c for c in (old or "").split(",")
                            if c and c not in em_list and _is_stable_board(c)]
        rows.append({"ts_code": ts_code,
                     "concepts": ",".join(merged[:MAX_CONCEPTS]) or None})

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE stock_basic SET concepts=:concepts, etl_update_time=NOW()
            WHERE ts_code=:ts_code
        """), rows)

    covered = sum(1 for r in rows if r["concepts"])
    return {"boards": len(boards), "em_stocks": len(em_map),
            "total": len(rows), "concept_covered": covered}
