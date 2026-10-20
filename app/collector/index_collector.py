# -*- coding: utf-8 -*-
"""指数日线行情采集
数据源: 新浪财经 CN_MarketData.getKLineData (与个股日K同一接口, 实测可用)
用途: 大盘环境判断(如 上证指数 MA5/MA20 死叉)、指数涨跌幅, 供策略直接查库使用
"""
import math
import time

import pandas as pd
import requests
from sqlalchemy import text

from app import config
from app.db import engine

SINA_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/CN_MarketData.getKLineData")
HEADERS = {"Referer": "https://finance.sina.com.cn"}

# 腾讯备用源(2026-09-16 加): 新浪单源一旦反爬即整体失败, 而旧兜底 baostock 已不可用
TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TX_HEADERS = {"Referer": "https://gu.qq.com/"}
TX_MAX_BARS = 1500   # 实测: 单次 N<=1500 正常; N=3000 返回空

# 关注的主流指数（新浪指数代码 -> 展示名）
INDEXES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000016", "上证50"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000688", "科创50"),
]

_session = requests.Session()
_session.trust_env = False  # 直连, 绕过系统代理(代理间歇断连)

UPSERT_SQL = text("""
    INSERT INTO stock_index_daily
        (index_code, index_name, trade_date, open, high, low, close,
         pre_close, pct_chg, vol, ma5, ma10, ma20)
    VALUES
        (:index_code, :index_name, :trade_date, :open, :high, :low, :close,
         :pre_close, :pct_chg, :vol, :ma5, :ma10, :ma20)
    ON DUPLICATE KEY UPDATE
        index_name=VALUES(index_name),
        open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close),
        pre_close=VALUES(pre_close), pct_chg=VALUES(pct_chg), vol=VALUES(vol),
        ma5=VALUES(ma5), ma10=VALUES(ma10), ma20=VALUES(ma20),
        etl_update_time=NOW()
""")


def _clean(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _fetch_index_sina(code: str, fetch_n: int) -> list:
    """新浪 CN_MarketData 指数日K -> dict 列表; 失败返回 []"""
    for _ in range(config.RETRY_TIMES):
        try:
            r = _session.get(SINA_KLINE_URL, params={
                "symbol": code, "scale": 240, "ma": "no", "datalen": fetch_n,
            }, headers=HEADERS, timeout=10)
            return r.json() or []
        except Exception:
            time.sleep(2)
    return []


def _fetch_index_tx(code: str, fetch_n: int) -> list:
    """腾讯 fqkline 指数日K -> 定长数组列表 [日期,开,收,高,低,量]; 失败返回 []。

    实测(2026-09-16): `param=sh000001,day,,,N,` 返回 `data.sh000001.day`;
    N<=1500 可用(1030 -> 4.2 年), **N=3000 返回空**(单次上限 ~1500);
    科创50(sh000688) 腾讯也有数据(新浪侧偶发缺该指数)。
    """
    if fetch_n > TX_MAX_BARS:
        return []
    try:
        r = _session.get(TX_KLINE_URL, params={"param": f"{code},day,,,{fetch_n},"},
                         headers=TX_HEADERS, timeout=10)
    except Exception:
        return []
    if r.status_code != 200 or not r.text.lstrip().startswith("{"):
        return []
    try:
        data = (r.json().get("data") or {}).get(code) or {}
    except Exception:
        return []
    return data.get("day") or data.get("qfqday") or []


def _finalize_index_df(df: pd.DataFrame, count: int, vol_div100: bool) -> pd.DataFrame:
    """统一收尾: 数值化 -> 排序 -> 涨跌幅 -> 均线 -> 丢预热行 -> 只留最后 count 根。

    vol_div100: 新浪返回单位为股(需 /100 转手, 与个股口径一致);
                腾讯指数 vol 与库内既有值**逐位相同**, 不能再除(2026-09-16 实测)。
    """
    if df.empty:
        return df
    for c in ["open", "close", "high", "low", "vol"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if vol_div100:
        df["vol"] = df["vol"] / 100
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)
    if df.empty:
        return df
    df["pre_close"] = df["close"].shift(1)
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1) * 100
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    # 丢预热行: 保证写库的每一行均线都有值(不会用 NULL 覆盖正确值);
    # 若返回行数不足 20 根则 df 为空 -> 不写库, 也不会误伤已有数据
    df = df[df["ma20"].notna()]
    if count:
        df = df.tail(int(count))
    return df.reset_index(drop=True)


def _fetch_index_with_source(code: str, count: int, warmup: int = 25):
    """拉取单指数日K(**新浪主源 -> 腾讯兜底**), 返回 (含均线的 DataFrame, 源标记)。

    均线预热(2026-09-16 修): 额外多拉 warmup 根仅用于滚动窗口预热, 只返回最后 count 根。
    旧实现只在 count 根窗口内算 rolling(5/10/20), 窗口头部 (window-1) 根必为 NaN,
    却又把这些 NaN 一并 UPSERT 进库 —— 等于用 NULL 覆盖掉库里原本正确的均线值;
    且采集窗口逐日前移, 旧行永不回补 ⇒ NULL 永久留存。实测 `--index`(days=25) 使
    stock_index_daily.ma20 出现连续 33 个交易日(2026-07-21~09-08)整段空洞,
    大盘环境门(上证 MA5>=MA20)因此在那些交易日静默失效。

    双源: 新浪反爬(456/挑战页/空体)时自动改用腾讯 fqkline(科创50 亦有数据),
    两源 vol 单位已分别归一(新浪 /100, 腾讯原值) —— 指数此前只有新浪单源,
    新浪一挂就整体失败(旧代码靠 baostock 兜底, 而 baostock 已整体不可用)。
    """
    fetch_n = int(count) + int(warmup)

    kl = _fetch_index_sina(code, fetch_n)
    if kl:
        df = pd.DataFrame(kl).rename(columns={"day": "trade_date", "volume": "vol"})
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        return _finalize_index_df(df, count, vol_div100=True), "sina"

    kl = _fetch_index_tx(code, fetch_n)
    if kl:
        df = pd.DataFrame([k[:6] for k in kl],
                          columns=["trade_date", "open", "close", "high", "low", "vol"])
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date
        return _finalize_index_df(df, count, vol_div100=False), "tx"

    return pd.DataFrame(), None


def _fetch_index(code: str, count: int, warmup: int = 25) -> pd.DataFrame:
    """兼容旧签名: 拉取单指数并返回含均线的 DataFrame。"""
    return _fetch_index_with_source(code, count, warmup)[0]


def sync_index_daily(days: int = 40) -> dict:
    """采集全部主流指数日线, 写入最后 days 个交易日(每行均线均已预热, 不会写 NULL)。
    均线预热另多拉 25 根, 不计入 days。
    源: 新浪主 + **腾讯兜底**(新浪反爬时自动切换; 腾讯单次上限 ~1500 根)。
    days 可放大做历史回填: 新浪 scale=240 datalen 无 1023 上限(实测 1030 -> ~4.2 年),
    腾讯 N<=1500 可用(1030 -> 4.2 年, 1500 -> 2020-07 起)。
    返回统计信息(含各指数实际使用的数据源 sources)。"""
    days = max(int(days or 40), 25)
    total_rows, ok, failed = 0, 0, []
    sources: dict[str, str] = {}
    for code, name in INDEXES:
        try:
            df, src = _fetch_index_with_source(code, days)
            if df.empty:
                failed.append({"index_code": code, "reason": "empty(新浪+腾讯均无数据)"})
                continue
            df["index_code"], df["index_name"] = code, name
            rows = df[["index_code", "index_name", "trade_date", "open", "high",
                       "low", "close", "pre_close", "pct_chg", "vol",
                       "ma5", "ma10", "ma20"]].to_dict(orient="records")
            rows = [{k: _clean(v) for k, v in row.items()} for row in rows]
            with engine.begin() as conn:
                conn.execute(UPSERT_SQL, rows)
            ok += 1
            total_rows += len(rows)
            sources[code] = src
            if src != "sina":
                print(f"[index] {code}({name}) 新浪不可用 -> 腾讯兜底, rows={len(rows)}",
                      flush=True)
        except Exception as e:
            failed.append({"index_code": code, "reason": str(e)[:200]})
        time.sleep(0.3)
    return {"total": len(INDEXES), "ok": ok, "failed_count": len(failed),
            "rows": total_rows, "failed_list": failed,
            "sources": sources,
            "tx_fallback": [c for c, s in sources.items() if s == "tx"]}
