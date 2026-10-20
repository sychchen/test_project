# -*- coding: utf-8 -*-
"""资金流历史回填: 东财 push2his fflow/daykline(个股资金流历史日K, ~120 天/只)。
背景: stock_money_flow 原实现为 push2delay clist 当日快照, 9/18 起快照步骤多次
失败导致缺口; 且历史深度不足。本脚本逐股 1 请求拉全历史日度资金流:
    fields2 f51-f65 -> 日期, 主力/小单/中单/大单/超大单净额(元), 占比%, 收盘价, 涨跌幅
    口径校验: 主力 = 超大单 + 大单(逐行断言); 首股 9/17 行 vs 库内快照对照
幂等: upsert + 断点 oneoff/mfhist_progress.txt; WAF 感知: 拦截 -> 冷却 300s 重试
一次 -> 仍拦则本轮中止(exit 2), 断点保留, 下次续跑。
"""
import os
import sys
import time

import pymysql
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config  # noqa: E402

URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
HEADERS = {"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
REQ_INTERVAL = 0.25
PROGRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "mfhist_progress.txt")


def _conn():
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT,
                           user=config.DB_USER, password=config.DB_PASSWORD,
                           database=config.DB_NAME, charset="utf8mb4",
                           autocommit=True)


def _secid(ts_code, symbol):
    return ("1." if ts_code.endswith(".SH") else "0.") + symbol


def _fetch(session, secid):
    # 2026-10-09 实测: requests(trust_env=False) 被 WAF 按 TLS/头特征拦截秒断,
    # urllib 同端点可通 -> 改走 urllib(会话参数仍由 session 参数位保留兼容)
    import json as _json
    import urllib.parse
    import urllib.request
    qs = urllib.parse.urlencode({
        "secid": secid, "klt": "101", "lmt": "0",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    })
    req = urllib.request.Request(
        URL + "?" + qs,
        headers={"Referer": "https://quote.eastmoney.com/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return (_json.load(resp).get("data") or {}).get("klines") or []


def _parse(kline):
    p = kline.split(",")
    d = p[0]
    main, small, medium, large, super_ = (float(p[i]) for i in range(1, 6))
    # 口径断言: 主力 = 超大单 + 大单 (0.5% 容差)
    if abs(main - (super_ + large)) > max(abs(main) * 0.005, 1.0):
        raise ValueError(f"字段序异常 {d}: main={main} vs super+large={super_ + large}")
    main_pct = float(p[6])
    return d, main, super_, large, medium, small, main_pct


def main():
    session = requests.Session()
    session.trust_env = False

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT ts_code, symbol FROM stock_basic WHERE is_active=1 ORDER BY ts_code")
    stocks = cur.fetchall()
    cur.execute("SHOW COLUMNS FROM stock_money_flow")
    cols = [r[0] for r in cur.fetchall()]
    num = {"main_net", "super_net", "large_net", "medium_net", "small_net", "main_pct"}
    ins_cols = ["ts_code", "trade_date"] + sorted(num & set(cols))
    placeholders = ",".join(["%s"] * len(ins_cols))
    sql = (f"INSERT INTO stock_money_flow ({','.join(ins_cols)}) "
           f"VALUES ({placeholders}) "
           f"ON DUPLICATE KEY UPDATE " +
           ",".join(f"{c}=VALUES({c})" for c in ins_cols[2:]))

    done = set()
    if os.path.exists(PROGRESS):
        done = {l.strip() for l in open(PROGRESS, encoding="utf-8") if l.strip()}
    todo = [(t, s) for t, s in stocks if t not in done]
    print(f"池 {len(stocks)} 只, 断点 {len(done)}, 待处理 {len(todo)}", flush=True)
    if not todo:
        print("全部完成")
        return

    # ---- 可用性探测(3 次重试, 间隔 30s; WAF 2026-10-09 实测间歇性放行) + 首股口径校验 ----
    probe_tc, probe_sym = todo[0]
    kl = None
    for attempt in range(3):
        try:
            kl = _fetch(session, _secid(probe_tc, probe_sym))
            break
        except Exception as e:
            print(f"探针第{attempt+1}次被拦({type(e).__name__})"
                  + (", 30s 后重试..." if attempt < 2 else ", 本轮不执行, 稍后重试"),
                  flush=True)
            if attempt < 2:
                time.sleep(30)
    if kl is None:
        sys.exit(2)
    parsed = []
    bad = 0
    for k in kl:
        try:
            parsed.append(_parse(k))
        except ValueError:
            bad += 1
    print(f"探针 {probe_tc}: {len(kl)} 行, 断言失败 {bad}")
    # 与库内最近共有日对照(容差 0.5%)
    # ⚠ 排除库内最新日: 该日可能由 sina fallback(口径差 1.5~3 倍, 见 backfill_money_flow.py
    #   文档头)按"只补缺失日"策略写入, 与东财 push2his 探针天然不一致, 不参与口径校验;
    #   更早的历史日均为东财口径(moneyflow 快照/mfhist 回填), 校验才有意义。
    if bad == 0 and "main_net" in cols:
        cur.execute("SELECT trade_date, main_net FROM stock_money_flow "
                    "WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 4",
                    (probe_tc,))
        _rows = cur.fetchall()
        ref = {str(d): v for d, v in _rows[1:]}   # 丢弃最新一行(可能是新浪口径)
        matched = 0
        for d, main, *_ in parsed:
            if d in ref and ref[d] is not None:
                diff = abs(main - float(ref[d]))
                if diff > max(abs(main) * 0.005, 1.0):
                    print(f"口径校验失败 {d}: API={main} 库内={ref[d]}")
                    sys.exit(3)
                matched += 1
        print(f"口径校验: 与库内对照 {matched} 日全部一致" if matched
              else "口径校验: 无共有日可对照(仅断言校验通过)")

    ok = failed = rows_total = 0
    for idx, (ts_code, symbol) in enumerate(todo, 1):
        try:
            kl = _fetch(session, _secid(ts_code, symbol))
        except Exception as e:
            print(f"[{idx}/{len(todo)}] {ts_code} 被拦({type(e).__name__}), 冷却 300s 重试...",
                  flush=True)
            failed += 1
            time.sleep(300)
            try:
                kl = _fetch(session, _secid(ts_code, symbol))
            except Exception:
                print(f"[{idx}/{len(todo)}] 重试仍拦 -> 中止本轮(断点 {len(done)} 只)", flush=True)
                break
        try:
            rows = []
            for k in kl:
                try:
                    d, main, sp, lg, md, sm, mp = _parse(k)
                except ValueError:
                    continue
                vals = {"main_net": main, "super_net": sp, "large_net": lg,
                        "medium_net": md, "small_net": sm, "main_pct": mp}
                rows.append([ts_code, d] + [vals.get(c) for c in ins_cols[2:]])
        except Exception as e:
            print(f"[{idx}/{len(todo)}] {ts_code} 解析失败: {e}")
            continue
        if rows:
            cur.executemany(sql, rows)
        rows_total += len(rows)
        ok += 1
        done.add(ts_code)
        with open(PROGRESS, "a", encoding="utf-8") as f:
            f.write(ts_code + "\n")
        time.sleep(REQ_INTERVAL)
        if idx % 200 == 0:
            print(f"[{idx}/{len(todo)}] 累计 {rows_total} 行", flush=True)
    cur.close()
    conn.close()
    print(f"完成: ok={ok} failed={failed} 共 {rows_total} 行", flush=True)


if __name__ == "__main__":
    main()
