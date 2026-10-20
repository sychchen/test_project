# -*- coding: utf-8 -*-
"""板块标签采集：申万行业 + 概念板块 + 指数成分
数据源: 新浪财经 Market_Center.getHQNodes（板块节点树）
       + Market_Center.getHQNodeData（节点成分股，分页）
写入: stock_basic.industry / concepts / index_tags
"""
import json
import threading
import time
from collections import defaultdict

import requests
from sqlalchemy import text

from app import config
from app.db import engine

NODES_URL = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/Market_Center.getHQNodes")
DATA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/Market_Center.getHQNodeData")
HEADERS = {"Referer": "https://finance.sina.com.cn"}

# 关注的主流指数（节点名 -> 展示名）；节点代码从节点树动态解析，找不到则跳过
INDEX_WHITELIST = ["上证50", "沪深300", "中证500", "中证1000", "科创50", "创业板指"]

_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        _tls.session = s
    return s


def _get(url, params=None, tries=config.RETRY_TIMES):
    last = None
    for _ in range(tries):
        try:
            r = _session().get(url, params=params, headers=HEADERS, timeout=15)
            return r
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def _find_node(tree, name):
    """在节点树中按名称找节点，返回节点对象。"""
    if isinstance(tree, list) and tree:
        if tree[0] == name:
            return tree
        children = tree[1] if len(tree) > 1 and isinstance(tree[1], list) else []
        for c in children:
            r = _find_node(c, name)
            if r:
                return r
    return None


def _leaf_nodes(node):
    """提取节点下的全部叶子 [(名称, 代码)]；叶子形如 [名称, '', 代码]。"""
    out = []
    if not node or len(node) < 2 or not isinstance(node[1], list):
        return out
    for c in node[1]:
        if isinstance(c, list) and len(c) >= 3 and c[2]:
            out.append((c[0], c[2]))
    return out


def _find_index_codes(tree):
    """在"指数成分"子树中递归找白名单指数的 (名称, 代码)。"""
    result = {}

    def walk(node):
        if isinstance(node, list) and node:
            if (isinstance(node[0], str) and node[0] in INDEX_WHITELIST
                    and len(node) >= 3 and node[2]):
                result[node[0]] = node[2]
            children = node[1] if len(node) > 1 and isinstance(node[1], list) else []
            for c in children:
                walk(c)
    root = _find_node(tree, "指数成分")
    if root:
        walk(root)
    return result


def _fetch_members(node_code: str) -> list[str]:
    """分页拉取某板块节点全部成分股代码（6位数字）。"""
    codes: list[str] = []
    page = 1
    while True:
        data = None
        for attempt in range(config.RETRY_TIMES):
            r = _get(DATA_URL, params={
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
                # 新浪偶发返回非 JSON（限流页/HTML），重试而非直接崩溃
                time.sleep(2 + attempt)
        if data is None:
            raise RuntimeError(
                f"[tags] 节点 {node_code} 第 {page} 页连续返回非 JSON 响应")
        if not data:
            break
        codes.extend(d["code"] for d in data if d.get("code"))
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.1)
    return codes


def _ensure_columns():
    """幂等加列：industry / concepts / index_tags"""
    ddl = {
        "industry": "ALTER TABLE stock_basic ADD COLUMN industry VARCHAR(64) NULL COMMENT '申万一级行业' AFTER market",
        "concepts": "ALTER TABLE stock_basic ADD COLUMN concepts VARCHAR(1024) NULL COMMENT '概念板块(逗号分隔)' AFTER industry",
        "index_tags": "ALTER TABLE stock_basic ADD COLUMN index_tags VARCHAR(255) NULL COMMENT '指数成分(逗号分隔)' AFTER concepts",
    }
    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='stock_basic'"
        ))}
        for col, sql in ddl.items():
            if col not in existing:
                conn.execute(text(sql))


def sync_tags(with_concepts: bool = True) -> dict:
    """全量刷新板块标签。返回统计信息。"""
    _ensure_columns()

    tree = json.loads(_get(NODES_URL).text)
    industry_nodes = _leaf_nodes(_find_node(tree, "申万行业"))
    concept_nodes = _leaf_nodes(_find_node(tree, "概念板块")) if with_concepts else []
    index_nodes = _find_index_codes(tree)

    print(f"[tags] 行业节点 {len(industry_nodes)}, 概念节点 {len(concept_nodes)}, "
          f"指数节点 {len(index_nodes)}", flush=True)

    industry_map: dict[str, str] = {}
    concept_map: dict[str, list] = defaultdict(list)
    index_map: dict[str, list] = defaultdict(list)

    for name, code in industry_nodes:
        for sym in _fetch_members(code):
            industry_map[sym] = name
        time.sleep(0.1)
    print(f"[tags] 行业成分完成: {len(industry_map)} 只", flush=True)

    for i, (name, code) in enumerate(concept_nodes, 1):
        for sym in _fetch_members(code):
            concept_map[sym].append(name)
        if i % 50 == 0:
            print(f"[tags] 概念进度 {i}/{len(concept_nodes)}", flush=True)
        time.sleep(0.1)
    print(f"[tags] 概念成分完成: {len(concept_map)} 只", flush=True)

    for name, code in index_nodes.items():
        for sym in _fetch_members(code):
            index_map[sym].append(name)
        time.sleep(0.1)
    print(f"[tags] 指数成分完成: {len(index_map)} 只", flush=True)

    # 批量回写 stock_basic
    with engine.connect() as conn:
        stocks = conn.execute(
            text("SELECT ts_code, symbol FROM stock_basic WHERE is_active=1")
        ).fetchall()
    rows = []
    for ts_code, symbol in stocks:
        rows.append({
            "ts_code": ts_code,
            "industry": industry_map.get(symbol),
            "concepts": ",".join(concept_map.get(symbol, [])[:20]) or None,
            "index_tags": ",".join(index_map.get(symbol, [])) or None,
        })
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE stock_basic SET industry=:industry, concepts=:concepts,
                index_tags=:index_tags, etl_update_time=NOW()
            WHERE ts_code=:ts_code
        """), rows)

    covered = sum(1 for r in rows if r["industry"])
    return {
        "total": len(rows), "industry_covered": covered,
        "concept_nodes": len(concept_nodes), "index_found": list(index_nodes.keys()),
    }
