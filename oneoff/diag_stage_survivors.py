# -*- coding: utf-8 -*-
"""WEEKLY_MAINLINE 回测窗口逐级候选存活统计
对照组: t0=2026-09-01 (days_ago=5, 已知出 3 票)
实验组: t0=2026-08-18 (days_ago=15, 当前为空)
复刻 select() 全部判定(含估值快照覆盖), 逐级打印存活数与淘汰原因 TopN
"""
import datetime as dt
import pandas as pd
from collections import Counter
from sqlalchemy import text

from app.db import engine
from app.strategy.base import filter_universe
import app.pick_service  # noqa: F401 触发注册
from app.strategy.weekly_mainline import WeeklyMainlineStrategy

VAL_COLS = ["pe_ttm", "pb", "total_mv", "circ_mv"]


def overlay(basics, t0):
    snap = pd.read_sql(
        text("SELECT ts_code, pe_ttm, pb, total_mv, circ_mv "
             "FROM stock_daily_quote WHERE trade_date=:t0"),
        engine, params={"t0": t0})
    if snap.empty:
        return basics
    snap = snap.set_index("ts_code")
    b = basics.set_index("ts_code")
    for c in VAL_COLS:
        if c in b.columns and c in snap.columns:
            b[c] = pd.to_numeric(snap[c], errors="coerce").combine_first(
                pd.to_numeric(b[c], errors="coerce"))
    return b.reset_index()


def run(t0, label):
    t0 = dt.date.fromisoformat(t0)
    print(f"\n{'='*64}\n[{label}] t0={t0}\n{'='*64}")
    st = WeeklyMainlineStrategy()
    p = st.merged_params(None)

    quotes = pd.read_sql(text("SELECT * FROM stock_daily_quote"), engine)
    basics = pd.read_sql(text("SELECT * FROM stock_basic WHERE is_active=1"), engine)
    quotes, basics = filter_universe(quotes, basics)
    hist = quotes[quotes["trade_date"] <= t0]
    basics = overlay(basics, t0)
    print(f"A. 全市场(大A): quotes={len(quotes)}行 basics={len(basics)}只, hist截至{t0}")

    b = basics.copy()
    b["concepts"] = b["concepts"].fillna("")
    if "industry" not in b.columns:
        b["industry"] = ""
    b["industry"] = b["industry"].fillna("")
    b["mcap_yi"] = pd.to_numeric(b["circ_mv"], errors="coerce") / 1e8
    b = b[~b["name"].str.contains("ST", na=False)]
    print(f"B. 剔ST后: {len(b)}只")

    def hit(tags, keywords):
        return tags.apply(lambda c: any(k in c for k in keywords))

    main_board = hit(b["concepts"], p["main_concepts"]) | hit(b["industry"], p["main_industries"])
    rot_board = hit(b["concepts"], p["rotate_concepts"]) | hit(b["industry"], p["rotate_industries"])
    print(f"C. 主线板块匹配(双通道): {main_board.sum()}只; 轮动板块: {rot_board.sum()}只")

    mcap_in = b["mcap_yi"].between(p["main_mcap_min"], p["main_mcap_max"])
    main_set = set(b[main_board & mcap_in]["ts_code"])
    rot_mcap = b["mcap_yi"].between(p["rotate_mcap_min"], p["rotate_mcap_max"])
    rot_set = set(b[rot_board & rot_mcap]["ts_code"])
    print(f"D. +主线市值[{p['main_mcap_min']},{p['main_mcap_max']}]亿: main_set={len(main_set)}只"
          f" (市值带外淘汰 {int((main_board & ~mcap_in).sum())}, 其中mcap为NaN {int((main_board & b['mcap_yi'].isna()).sum())})")
    print(f"   +轮动市值[{p['rotate_mcap_min']},{p['rotate_mcap_max']}]亿: rot_set={len(rot_set)}只")

    latest_date = hist["trade_date"].max()
    assert str(latest_date) == str(t0), f"latest_date={latest_date} != t0={t0}"

    list_dates = {}
    if "list_date" in b.columns:
        list_dates = {k: v for k, v in zip(b["ts_code"], b["list_date"]) if pd.notna(v)}
    flow_map = st._load_money_flow(latest_date)
    umf = p["use_money_flow"]
    if isinstance(umf, str) and umf.lower() == "auto":
        flow_on = flow_map is not None and st._flow_depth() >= p["min_flow_days"]
    else:
        flow_on = bool(umf) and flow_map is not None
    print(f"E. 资金流: flow_map={'None' if flow_map is None else len(flow_map)}, 硬过滤={'ON' if flow_on else 'OFF'}")

    metrics, kill = {}, Counter()
    watch = main_set | rot_set
    for ts_code, g in hist.groupby("ts_code"):
        g = g.sort_values("trade_date")
        last = g.iloc[-1]
        if ts_code not in watch:
            continue
        if last["trade_date"] != latest_date:
            kill["停牌/当日无行情"] += 1
            continue
        ld = list_dates.get(ts_code)
        if ld is not None:
            ld = ld.date() if hasattr(ld, "date") else ld
            if (latest_date - ld).days < p["min_list_days"]:
                kill["新股(min_list_days)"] += 1
                continue
        if len(g) < p["min_hist_days"]:
            kill["历史不足(min_hist_days)"] += 1
            continue
        if pd.isna(last["close"]) or pd.isna(last["pct_chg"]):
            kill["收盘/涨跌幅NaN"] += 1
            continue
        metrics[ts_code] = {
            "close": float(last["close"]), "pct": float(last["pct_chg"]),
            "vr1": st._vol_ratio(g, 1), "vr3": st._vol_ratio(g, 3),
            "chg5": st._chg(g["close"], 5), "chg20": st._chg(g["close"], 20),
            "rsi14": st._rsi14(g["close"]),
            "flow": flow_map.get(ts_code) if flow_map else None,
        }
    print(f"F. 指标计算: 入算 {len(watch)}只 -> 存活 {len(metrics)}只; 淘汰明细: {dict(kill)}")

    idx_ok, idx_desc = st._index_env_ok(latest_date)
    print(f"G. 环境门: {idx_desc} -> {'放行' if idx_ok else '拦截'}")

    fk = Counter()
    main_cands = []
    for code in main_set:
        m = metrics.get(code)
        if not m:
            fk["无指标(见F)"] += 1
            continue
        if None in (m["vr1"], m["vr3"], m["chg5"], m["chg20"], m["rsi14"]):
            fk["指标None"] += 1
            continue
        if not (p["vol_ratio_min"] <= m["vr1"] <= p["vol_ratio_max"]):
            fk[f"量比不在[{p['vol_ratio_min']},{p['vol_ratio_max']}]"] += 1
            continue
        if not (p["main_daily_min"] <= m["pct"] <= p["main_daily_max"]):
            fk[f"当日涨幅不在[{p['main_daily_min']},{p['main_daily_max']}]"] += 1
            continue
        if m["chg5"] <= p["main_chg5_min"]:
            fk["chg5<=0"] += 1
            continue
        if m["chg20"] <= p["main_chg20_min"]:
            fk["chg20<=5"] += 1
            continue
        if m["rsi14"] >= p["rsi14_max"]:
            fk["RSI>=70"] += 1
            continue
        if flow_on and (m["flow"] is None or m["flow"] <= p["main_net_min"]):
            fk["主力净流入不足"] += 1
            continue
        score = round(m["vr3"] * 0.4 + m["vr1"] * 0.3 + m["chg20"] / 10 * 0.3, 3)
        main_cands.append((code, m, score))
    print(f"H. 主线过滤: main_set={len(main_set)} -> 候选 {len(main_cands)}; 淘汰明细: {dict(fk)}")
    for code, m, s in sorted(main_cands, key=lambda x: -x[2])[:8]:
        print(f"   候选 {code}: vr1={m['vr1']} vr3={m['vr3']} pct={m['pct']} "
              f"chg5={m['chg5']} chg20={m['chg20']} rsi={m['rsi14']} score={s}")

    rk = Counter()
    rot_cands = []
    for code in rot_set:
        m = metrics.get(code)
        if not m:
            rk["无指标(见F)"] += 1
            continue
        if None in (m["vr1"], m["vr3"], m["chg20"]):
            rk["指标None"] += 1
            continue
        if m["vr1"] < p["rotate_vol_ratio_min"]:
            rk["量比<1.8"] += 1
            continue
        if not (p["rotate_daily_min"] <= m["pct"] <= p["rotate_daily_max"]):
            rk["当日涨幅越界"] += 1
            continue
        if not (p["rotate_chg20_min"] <= m["chg20"] <= p["rotate_chg20_max"]):
            rk["chg20越界"] += 1
            continue
        score = round(m["vr1"] * 0.5 + m["vr3"] * 0.5, 3)
        rot_cands.append((code, m, score))
    print(f"I. 轮动过滤: rot_set={len(rot_set)} -> 候选 {len(rot_cands)}; 淘汰明细: {dict(rk)}")
    print(f"J. 结论: 主线候选{len(main_cands)} + 轮动候选{len(rot_cands)} "
          f"-> {'有产出' if (main_cands or rot_cands) else '空池(选股为空)'}")


run("2026-09-01", "对照组 days_ago=5")
run("2026-08-18", "实验组 days_ago=15")
