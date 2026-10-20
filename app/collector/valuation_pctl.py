# -*- coding: utf-8 -*-
"""估值历史分位落库: 把 VALUE_PCTL 策略的 6 分钟全量计算挪到采集链路日度执行

链路位置: daily_update 中 valsnap -> valtopup(估值日度顶格) 之后、pick 之前。
口径与 app/strategy/value_pctl.py 完全一致:
  近 lookback_years 年窗口, pb_pctl/pe_pctl = groupby(ts_code).rank(pct=True) 取最新行。
  科创板(688/689)一并落库(策略侧自行剔除, 表保持全量不做业务过滤)。
幂等: uk(股+日) UPSERT, 可重跑。
性能: 读近5年约600万行 ~4-6分钟(MySQL 5.5 无窗口函数, pandas 计算)。
"""
import time

import pandas as pd
from sqlalchemy import text

from app.db import engine

LOOKBACK_YEARS = 5


def refresh_valuation_pctl(trade_date: str = None,
                           lookback_years: int = LOOKBACK_YEARS) -> dict:
    """重算指定交易日(默认最新)的 PB/PE 历史分位并落库"""
    t0 = time.time()
    days = int(lookback_years * 365)
    with engine.connect() as conn:
        td = trade_date or conn.execute(
            text("SELECT MAX(trade_date) FROM stock_valuation")).scalar()
        if not td:
            return {"trade_date": None, "rows": 0, "elapsed": 0}
        df = pd.read_sql(text("""
            SELECT ts_code, trade_date, pb, pe_ttm, total_mv,
                   close_price, pct_chg
            FROM stock_valuation
            WHERE trade_date >= :since
        """), conn, params={"since": pd.Timestamp(td) - pd.Timedelta(days=days)})
    if df.empty:
        return {"trade_date": str(td), "rows": 0, "elapsed": int(time.time() - t0)}

    # read_sql 的 DATE 列是 datetime.date 对象, 须转 Timestamp 才能与 td 做等值比较
    # (datetime.date == pd.Timestamp 恒 False, 会导致当日行筛选为空)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.dropna(subset=["pb"]).sort_values(["ts_code", "trade_date"])
    df["pb_pctl"] = df.groupby("ts_code")["pb"].rank(pct=True)
    df["pe_pctl"] = df.groupby("ts_code")["pe_ttm"].rank(pct=True)
    hist = df.groupby("ts_code").size()
    last = df[df["trade_date"] == pd.Timestamp(td)].copy()
    last["hist_days"] = last["ts_code"].map(hist)

    rows = [{
        "ts": r.ts_code, "td": td,
        "close": None if pd.isna(r.close_price) else float(r.close_price),
        "pct": None if pd.isna(r.pct_chg) else float(r.pct_chg),
        "mv": None if pd.isna(r.total_mv) else float(r.total_mv),
        "pe": None if pd.isna(r.pe_ttm) else float(r.pe_ttm),
        "pb": float(r.pb),
        "pbp": round(float(r.pb_pctl), 5),
        "pep": None if pd.isna(r.pe_pctl) else round(float(r.pe_pctl), 5),
        "hd": int(r.hist_days),
    } for r in last.itertuples()]

    if not rows:
        return {"trade_date": str(td), "rows": 0, "elapsed": int(time.time() - t0)}
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO stock_valuation_pctl
                (ts_code, trade_date, close_price, pct_chg, total_mv,
                 pe_ttm, pb, pb_pctl, pe_pctl, hist_days)
            VALUES (:ts, :td, :close, :pct, :mv, :pe, :pb, :pbp, :pep, :hd)
            ON DUPLICATE KEY UPDATE
                close_price=VALUES(close_price), pct_chg=VALUES(pct_chg),
                total_mv=VALUES(total_mv), pe_ttm=VALUES(pe_ttm), pb=VALUES(pb),
                pb_pctl=VALUES(pb_pctl), pe_pctl=VALUES(pe_pctl),
                hist_days=VALUES(hist_days), etl_update_time=NOW()
        """), rows)
    return {"trade_date": str(td), "rows": len(rows),
            "elapsed": int(time.time() - t0)}
