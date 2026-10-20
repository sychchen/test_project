# -*- coding: utf-8 -*-
"""股票基础信息 + 基本面快照采集
数据源: 新浪财经 Market_Center.getHQNodeData (hs_a 全市场节点, 股票列表/名称)
       估值四字段(PE/PB/市值): 腾讯 qt.gtimg.cn 88字段快照(真TTM口径, 2026-09-08起;
       三方仲裁发现新浪 per 为静态口径对银行股偏差达8%, 新浪值仅作腾讯缺失时兜底)
       上市日期: 新浪 F10 公司资料页(HTML 解析, 仅补缺, 一次性回填后自然增量)
说明: 东方财富接口在当前网络被阻断, 新浪/腾讯接口稳定可用(已实测)
"""
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from sqlalchemy import text

from app import config
from app.db import engine

SINA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
            "json_v2.php/Market_Center.getHQNodeData")
F10_URL = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
           "vCI_CorpInfo/stockid/{symbol}.phtml")
LIST_DATE_RE = re.compile(r"上市日期.{0,200}?(\d{4})-(\d{1,2})-(\d{1,2})", re.S)
HEADERS = {"Referer": "https://finance.sina.com.cn"}

# 直连会话（绕过系统代理，东财经验：代理间歇断连）
_session = requests.Session()
_session.trust_env = False

# 每线程独立会话（F10 并发抓取用）
_tls = threading.local()


def _tls_session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        _tls.session = s
    return s


def _market_of(symbol: str) -> str:
    if symbol.startswith(("60", "68")):
        return "SH"
    return "SZ"


# ---------------------------------------------------------------
# 估值四字段(pe_ttm/pb/total_mv/circ_mv)腾讯快照覆盖层
# 背景: 2026-09-08 三方仲裁发现新浪 per 为静态口径(平安银行 5.69 vs
# 腾讯/baostock TTM 5.26, 偏差8%), 腾讯 qt.gtimg.cn 88字段快照的
# f39=PE-TTM/f44=流通市值(亿)/f45=总市值(亿)/f46=PB 与 baostock 完全一致,
# 且支持 60 只/批 -> 估值四字段统一改用腾讯 TTM 口径, 新浪仅作兜底。
# ---------------------------------------------------------------
TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
TENCENT_HEADERS = {"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"}
T_BATCH = 60
_LINE_RE = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')


def _fetch_tencent_valuation(symbols: list[str]) -> dict:
    """批量抓取腾讯估值快照。返回 {symbol: {pe_ttm, pb, total_mv, circ_mv}}（元）。"""
    out = {}
    for i in range(0, len(symbols), T_BATCH):
        batch = symbols[i:i + T_BATCH]
        codes = ",".join(("sh" if s.startswith(("60", "68")) else "sz") + s
                         for s in batch)
        last_err = None
        for _ in range(config.RETRY_TIMES):
            try:
                r = _session.get(TENCENT_URL.format(codes=codes),
                                 headers=TENCENT_HEADERS, timeout=10)
                r.encoding = "gbk"
                for m in _LINE_RE.finditer(r.text):
                    q = m.group(1)
                    f = m.group(2).split("~")
                    if len(f) < 47:
                        continue
                    sym = q[2:]

                    def _num(x, scale=1.0):
                        try:
                            v = float(x)
                            return None if math.isnan(v) else v * scale
                        except (TypeError, ValueError):
                            return None
                    out[sym] = {
                        "pe_ttm": _num(f[39]),
                        "pb": _num(f[46]),
                        "total_mv": _num(f[45], 1e8),   # 亿 -> 元
                        "circ_mv": _num(f[44], 1e8),    # 亿 -> 元
                    }
                last_err = None
                break
            except Exception as e:
                last_err = e
                time.sleep(1)
        if last_err:
            print(f"[basic] 腾讯估值批次失败({batch[0]}..): {last_err}", flush=True)
        time.sleep(0.2)
    return out


def _fetch_all_hs_a() -> list[dict]:
    """分页拉取全市场 A 股快照"""
    rows: list[dict] = []
    page = 1
    while True:
        for attempt in range(config.RETRY_TIMES):
            try:
                r = _session.get(SINA_URL, params={
                    "page": page, "num": 100, "sort": "symbol", "asc": 1,
                    "node": "hs_a", "symbol": "", "_s_r_a": "page",
                }, headers=HEADERS, timeout=10)
                data = r.json()
                break
            except Exception:
                if attempt == config.RETRY_TIMES - 1:
                    raise
                # 新浪限流时返回非 JSON，1s 间隔不足以度过限流窗口，递增退避
                time.sleep(3 * (attempt + 1))
        if not data:
            break
        rows.extend(data)
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.3)
    return rows


def sync_stock_basic() -> dict:
    """全量同步 A 股股票列表 + PE/PB/市值快照，返回统计信息"""
    raw = _fetch_all_hs_a()
    df = pd.DataFrame(raw)
    # 仅保留沪深主板/创业板/科创板（剔除北交所 bj 开头）
    df = df[df["code"].str.match(r"^(60|68|00|30)\d{4}$")]
    out = pd.DataFrame({
        "symbol": df["code"].astype(str),
        "name": df["name"].astype(str),
        "pe_ttm": pd.to_numeric(df.get("per"), errors="coerce"),
        "pb": pd.to_numeric(df.get("pb"), errors="coerce"),
        # 新浪市值单位为万元 -> 元
        "total_mv": pd.to_numeric(df.get("mktcap"), errors="coerce") * 10000,
        "circ_mv": pd.to_numeric(df.get("nmc"), errors="coerce") * 10000,
    })
    out["market"] = out["symbol"].map(_market_of)
    out["ts_code"] = out["symbol"] + "." + out["market"]
    out = out.drop_duplicates(subset=["ts_code"])

    # 估值四字段覆盖为腾讯 TTM 口径(新浪 per 为静态口径, 偏差实证见模块注释);
    # 腾讯缺失的行保留新浪值兜底
    tx = _fetch_tencent_valuation(out["symbol"].tolist())
    covered = 0
    for col in ("pe_ttm", "pb", "total_mv", "circ_mv"):
        tx_col = out["symbol"].map(lambda s: (tx.get(s) or {}).get(col))
        out[col] = tx_col.where(pd.notna(tx_col), out[col])
    covered = sum(1 for s in out["symbol"] if s in tx)
    print(f"[basic] 腾讯估值覆盖 {covered}/{len(out)}", flush=True)

    out = out.where(pd.notna(out), None)

    upsert_sql = text("""
        INSERT INTO stock_basic (ts_code, symbol, name, market, pe_ttm, pb, total_mv, circ_mv, is_active)
        VALUES (:ts_code, :symbol, :name, :market, :pe_ttm, :pb, :total_mv, :circ_mv, 1)
        ON DUPLICATE KEY UPDATE
            name=VALUES(name), market=VALUES(market), pe_ttm=VALUES(pe_ttm),
            pb=VALUES(pb), total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv), is_active=1,
            etl_update_time=NOW()
    """)
    rows = out.to_dict(orient="records")
    with engine.begin() as conn:
        conn.execute(upsert_sql, rows)

    return {"synced": len(rows), "sample": rows[:3]}


def _ensure_list_date_column() -> None:
    """幂等加列: stock_basic.list_date"""
    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='stock_basic'"
        ))}
        if "list_date" not in existing:
            conn.execute(text(
                "ALTER TABLE stock_basic ADD COLUMN list_date DATE NULL "
                "COMMENT '上市日期' AFTER market"))


# ---------------------------------------------------------------
# 估值日频快照: stock_basic 只存最新值, 日频历史存入 stock_daily_quote
# (同源拷贝, 零网络请求; 自上线日起前滚积累, 与 stock_money_flow 一致)
# ---------------------------------------------------------------
_VALUATION_DDL = {
    "pe_ttm":   "DECIMAL(12,4) NULL COMMENT '市盈率TTM(当日快照)'",
    "pb":       "DECIMAL(12,4) NULL COMMENT '市净率(当日快照)'",
    "total_mv": "DECIMAL(20,2) NULL COMMENT '总市值(元,当日快照)'",
    "circ_mv":  "DECIMAL(20,2) NULL COMMENT '流通市值(元,当日快照)'",
}


def _ensure_valuation_columns() -> None:
    """幂等加列: stock_daily_quote.pe_ttm/pb/total_mv/circ_mv"""
    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='stock_daily_quote'"
        ))}
        for col, ddl in _VALUATION_DDL.items():
            if col not in existing:
                conn.execute(text(
                    f"ALTER TABLE stock_daily_quote ADD COLUMN {col} {ddl}"))


def snapshot_valuation(trade_date: str = None) -> dict:
    """把 stock_basic 最新估值快照拷贝到 stock_daily_quote 指定交易日行。

    同源(新浪)同日拷贝: --all 中 basic 先于 quotes 执行, 估值与当日收盘口径一致。
    仅 UPDATE 已存在的日线行(停牌股当日无行则不写), 幂等可重跑。
    """
    _ensure_valuation_columns()
    with engine.begin() as conn:
        td = trade_date or conn.execute(
            text("SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
        if not td:
            raise RuntimeError("stock_daily_quote 无数据, 请先执行 --quotes")
        r = conn.execute(text("""
            UPDATE stock_daily_quote q
            JOIN stock_basic b ON b.ts_code = q.ts_code
            SET q.pe_ttm=b.pe_ttm, q.pb=b.pb,
                q.total_mv=b.total_mv, q.circ_mv=b.circ_mv,
                q.etl_update_time=NOW()
            WHERE q.trade_date=:td
        """), {"td": td})
    # 换手率依赖 circ_mv, 必须在估值落库之后计算(纯SQL, 单次UPDATE)
    from app.collector.indicator_collector import sync_turnover_rate
    tr = sync_turnover_rate(trade_date=td)
    return {"trade_date": str(td), "rows": r.rowcount,
            "turnover_rows": tr["rows"]}


def topup_valuation_from_quote(trade_date: str = None) -> dict:
    """估值日度顶格: 把 stock_daily_quote 当日估值快照列 UPSERT 进 stock_valuation。

    背景: stock_valuation 全历史来自东财单股接口(--valuation), 不适合日跑;
    而 stock_daily_quote 的 pe_ttm/pb/total_mv/circ_mv 由 valsnap 每日维护(新浪快照),
    直接顶格即可让估值表保持日鲜, 零额外 API 成本。
    代价: 顶格行的 pe_static/peg/pcf/ps 为 NULL(仅东财接口有), 分位计算只用 pb/pe_ttm 不受影响。
    幂等可重跑(uk 股+日)。须在 valsnap 之后执行(当日估值列已就位)。
    """
    with engine.begin() as conn:
        td = trade_date or conn.execute(
            text("SELECT MAX(trade_date) FROM stock_daily_quote")).scalar()
        if not td:
            raise RuntimeError("stock_daily_quote 无数据, 请先执行 --quotes")
        r = conn.execute(text("""
            INSERT INTO stock_valuation
                (ts_code, trade_date, close_price, pct_chg,
                 total_mv, circ_mv, pe_ttm, pb)
            SELECT q.ts_code, q.trade_date, q.close, q.pct_chg,
                   q.total_mv, q.circ_mv, q.pe_ttm, q.pb
            FROM stock_daily_quote q
            WHERE q.trade_date=:td AND (q.pe_ttm IS NOT NULL OR q.pb IS NOT NULL)
            ON DUPLICATE KEY UPDATE
                close_price=VALUES(close_price), pct_chg=VALUES(pct_chg),
                total_mv=VALUES(total_mv), circ_mv=VALUES(circ_mv),
                pe_ttm=VALUES(pe_ttm), pb=VALUES(pb)
        """), {"td": td})
    return {"trade_date": str(td), "rows": r.rowcount}


def _fetch_list_date(ts_code: str, symbol: str) -> tuple[str, str | None, str | None]:
    """抓 F10 公司资料页解析上市日期。返回 (ts_code, list_date|None, 失败原因)。"""
    last_err = None
    for _ in range(config.RETRY_TIMES):
        try:
            r = _tls_session().get(F10_URL.format(symbol=symbol),
                                   headers=HEADERS, timeout=15)
            r.encoding = "gbk"
            m = LIST_DATE_RE.search(r.text)
            if not m:
                return ts_code, None, "parse_miss"
            y, mo, d = m.groups()
            return ts_code, f"{y}-{int(mo):02d}-{int(d):02d}", None
        except Exception as e:
            last_err = e
            time.sleep(2)
    return ts_code, None, str(last_err)[:200]


def sync_list_dates(limit: int = None) -> dict:
    """补缺上市日期: 只抓 stock_basic.list_date 为 NULL 的在市股票(8线程)。
    首次全量回填后, 后续运行自动退化为仅补新上市股票(近零开销)。"""
    _ensure_list_date_column()
    with engine.connect() as conn:
        stocks = conn.execute(text(
            "SELECT ts_code, symbol FROM stock_basic "
            "WHERE is_active=1 AND list_date IS NULL ORDER BY ts_code"
        )).fetchall()
    if limit:
        stocks = stocks[:limit]
    if not stocks:
        return {"pending": 0, "ok": 0, "failed_count": 0, "failed_list": []}

    ok, failed, done = 0, [], 0
    updates = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_list_date, ts_code, symbol): ts_code
                   for ts_code, symbol in stocks}
        for fut in as_completed(futures):
            ts_code, list_date, err = fut.result()
            done += 1
            if err:
                failed.append({"ts_code": ts_code, "reason": err})
            else:
                ok += 1
                updates.append({"ts_code": ts_code, "list_date": list_date})
            if done % 500 == 0:
                print(f"[listdate] progress {done}/{len(stocks)} ok={ok} "
                      f"failed={len(failed)}", flush=True)

    if updates:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE stock_basic SET list_date=:list_date, etl_update_time=NOW() "
                "WHERE ts_code=:ts_code"), updates)
    return {"pending": len(stocks), "ok": ok, "failed_count": len(failed),
            "failed_list": failed[:50]}
