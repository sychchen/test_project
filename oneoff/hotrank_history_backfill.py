# -*- coding: utf-8 -*-
"""人气榜历史回填: 东财 emappdata getHisList(个股历史人气排名, 366 天/只)。
背景: stock_hot_rank 原实现为 push2ex Top100 榜单快照(仅当前时刻, 无历史),
9/18-9/21 快照永久缺失。本脚本用个股历史排名接口重建:
    候选池 = 近 60 交易日日均成交额前 800 只
    逐股拉 366 天排名 -> 只保留 rank<=100 的交易日记录 -> upsert
表语义保持不变(表内每行=某日某股处于人气榜前 100)。
口径校验: 先对库内已有快照日(9/17、9/21、9/22)做抽样比对, 排名不一致率>5% 则中止。
幂等: upsert + 断点文件 oneoff/hotrank_hist_progress.txt, 重跑跳过已处理股。
"""
import os
import sys
import time

import pymysql
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config  # noqa: E402

URL = "https://emappdata.eastmoney.com/stockrank/getHisList"
HEADERS = {"User-Agent": "Mozilla/5.0"}
POOL_N = 800
POOL_DAYS = 60
REQ_INTERVAL = 0.2
PROGRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "hotrank_hist_progress.txt")
RANK_KEEP = 100


def _conn():
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT,
                           user=config.DB_USER, password=config.DB_PASSWORD,
                           database=config.DB_NAME, charset="utf8mb4",
                           autocommit=True)


def _load_pool():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ts_code, symbol, AVG(amount) amt
            FROM stock_daily_quote q JOIN stock_basic b USING (ts_code)
            WHERE trade_date >= (SELECT MAX(trade_date) FROM stock_daily_quote)
              - INTERVAL 100 DAY AND b.is_active=1
            GROUP BY ts_code, symbol ORDER BY amt DESC LIMIT %s
        """, (POOL_N,))
        pool = cur.fetchall()
        cur.execute("SELECT DISTINCT trade_date FROM stock_daily_quote")
        trade_dates = {str(r[0]) for r in cur.fetchall()}
        cur.execute("SHOW COLUMNS FROM stock_hot_rank")
        cols = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT ts_code, trade_date, rank_num FROM stock_hot_rank")
        existing = {(a, str(b)): c for a, b, c in cur.fetchall()}
    return pool, trade_dates, cols, existing


def _em_code(ts_code, symbol):
    mkt = ts_code.split(".")[-1] if "." in ts_code else "SH"
    return f"{mkt}{symbol}"


def _fetch_ranks(em_code, session):
    r = session.post(URL, json={
        "appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "", "srcSecurityCode": em_code, "yearType": "5"},
        headers=HEADERS, timeout=15)
    j = r.json()
    if j.get("status") != 0:
        raise RuntimeError(f"emappdata status={j.get('status')}")
    return j.get("data") or []


def _validate(session, pool, existing):
    """抽样 9/21/9/22 快照日: API 历史排名 vs 库内快照排名。"""
    samples = [(a, str(b), c) for (a, b), c in existing.items()
               if str(b) >= "2026-09-17"][:400]
    pool_codes = {t for t, _, _ in pool}
    checked = match = 0
    by_code = {}
    for a, b, c in samples:
        by_code.setdefault(a, []).append((b, c))
    for tc, lst in list(by_code.items()):
        if tc not in pool_codes:
            continue
        sym = tc[:6]
        try:
            rows = _fetch_ranks(_em_code(tc, sym), session)
        except Exception as e:
            print(f"[validate] {tc} 拉取失败: {e}")
            continue
        api = {x["calcTime"]: x["rank"] for x in rows}
        time.sleep(REQ_INTERVAL)
        for d, snap_rank in lst:
            if d in api:
                checked += 1
                if api[d] == snap_rank:
                    match += 1
        if checked >= 150:
            break
    rate = match / checked if checked else 0
    print(f"[validate] 样本 {checked} 条, 排名一致 {match} ({rate:.1%})")
    return checked >= 50 and rate >= 0.95


def main():
    pool, trade_dates, cols, existing = _load_pool()
    done = set()
    if os.path.exists(PROGRESS):
        done = {l.strip() for l in open(PROGRESS, encoding="utf-8") if l.strip()}
    todo = [(t, s, a) for t, s, a in pool if t not in done]
    print(f"池 {len(pool)} 只, 已断点 {len(done)}, 待处理 {len(todo)}", flush=True)
    if not todo:
        print("全部完成")
        return

    # 行情映射: (ts_code, date) -> (close, pct_chg)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT ts_code, trade_date, close, pct_chg FROM stock_daily_quote
            WHERE trade_date >= (SELECT MAX(trade_date) FROM stock_daily_quote)
              - INTERVAL 400 DAY""")
        qmap = {(a, str(b)): (c, d) for a, b, c, d in cur.fetchall()}

    session = requests.Session()
    session.trust_env = False

    if done:  # 已有断点 -> 跳过首验(此前已验过)
        pass
    else:
        if not _validate(session, pool, existing):
            print("口径校验未通过, 中止(未写入任何数据)")
            return

    has_price = "price" in cols and "pct_chg" in cols
    col_list = ["ts_code", "trade_date", "rank_num"]
    if has_price:
        col_list += ["price", "pct_chg"]
    sql = ("INSERT INTO stock_hot_rank (" + ",".join(col_list) +
           ") VALUES (%s,%s,%s" + (",%s,%s" if has_price else "") + ") " +
           "ON DUPLICATE KEY UPDATE " +
           ",".join(f"{c}=VALUES({c})" for c in col_list[2:]))

    ok = failed = 0
    rows_total = 0
    conn = _conn()
    cur = conn.cursor()
    for idx, (ts_code, symbol, _) in enumerate(todo, 1):
        try:
            hist = _fetch_ranks(_em_code(ts_code, symbol), session)
        except Exception as e:
            print(f"[{idx}/{len(todo)}] {ts_code} 失败: {type(e).__name__} {e}",
                  flush=True)
            failed += 1
            time.sleep(60)
            try:
                hist = _fetch_ranks(_em_code(ts_code, symbol), session)
            except Exception:
                print(f"[{idx}/{len(todo)}] {ts_code} 重试仍失败 -> 中止本轮"
                      f"(断点已存 {len(done)} 只)", flush=True)
                break
        rows = []
        for x in hist:
            d = str(x.get("calcTime") or "")
            rk = x.get("rank")
            if not d or rk is None or int(rk) > RANK_KEEP or d not in trade_dates:
                continue
            row = [ts_code, d, int(rk)]
            if has_price:
                q = qmap.get((ts_code, d))
                row += [float(q[0]) if q and q[0] is not None else None,
                        float(q[1]) if q and q[1] is not None else None]
            rows.append(row)
        if rows:
            cur.executemany(sql, rows)
        rows_total += len(rows)
        ok += 1
        done.add(ts_code)
        with open(PROGRESS, "a", encoding="utf-8") as f:
            f.write(ts_code + "\n")
        time.sleep(REQ_INTERVAL)
        if idx % 100 == 0:
            print(f"[{idx}/{len(todo)}] 累计入库 {rows_total} 行", flush=True)
    cur.close()
    conn.close()
    print(f"完成: ok={ok} failed={failed} 新增/更新 {rows_total} 行", flush=True)


if __name__ == "__main__":
    main()
