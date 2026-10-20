# -*- coding: utf-8 -*-
"""板块行情合成器（需求#19, 口径见 docs/06 v1.1 已确认）
- synth_board_daily: 板块日线 = 成分股流通市值加权收益复利累乘（基期=1000, 等权对照列）
    权重口径: 最新流通市值静态权重（stock_daily_quote.circ_mv 快照自 2026-09-08 才前滚,
    更早日期无日频权重, 故用静态权重近似, 已在 docs/06 注明）;
    覆盖 stock_daily_quote 全部历史；**成分覆盖门槛**(2026-09-16 加)：
    仅当当日有效成分股数 >= max(3, 20%×成分股数) 才出行，剔除「1~2 只股票冒充板块指数」
    的早期伪指数日（实测剔除 905 个板块-日，全表 49,603 行 / 1,032 交易日）。
    按最新成分股回溯 = 幸存者偏差口径（未上市成分股因无行情自动不计权重；
    历史成分变更/退市无法还原，需外部数据，决策 A5-3）。
- synth_board_minutes: 板块分时 = 成分股 5 分钟 bar 流通市值加权（相对昨收归一）,
    仅保留最近 30 个交易日。
- refresh_member_tags: corr60 相关系数 / 龙头打分 / core·leader 标签 / 板块解读 summary。
依赖: stock_board_member 已有成分映射（先跑 --board）。
"""
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import bindparam, text

from app.db import engine

BASE_INDEX = 1000.0
CORR_WINDOW = 60          # 相关性窗口（交易日）
LEADER_TOP_N = 3          # 龙头标签数量
CORE_CORR_MIN = 0.7       # 高相关阈值
CORE_TOP_PCT = 0.2        # 或相关性 TOP20%
MINUTE_KEEP_DAYS = 30     # 板块分时保留交易日数
DAILY_MIN_MEMBERS = 3     # 板块日线出行的最小有效成分股数（绝对下限）
DAILY_MIN_COVER_PCT = 0.2  # 且需达到成分股数的该比例（防「1 只股票冒充板块指数」）
MINUTE_DAY_MIN_CODES = 100  # 视为"有效分时日"的最小成分股覆盖数（排除零星残留）


# 板块概念解读知识库（2026-09-15 补）—— 新浪行业分类共 49 个板块。
# 原 summary 仅由统计模板生成（成分股数/市值/龙头/涨跌幅），不构成"概念解读"；
# 此处为每个板块补一句准确的行业范围与关键驱动描述，与统计部分拼接后落库。
BOARD_DESC: dict[str, str] = {
    "new_dzxx": "涵盖半导体、集成电路、消费电子、通信设备与电子元器件，是科技产业链的核心环节，景气度与消费电子周期、国产替代进程密切相关。",
    "new_jxhy": "包含通用/专用设备、工程机械、机床与工业自动化等，处于制造业中游，需求与基建投资、制造业资本开支高度相关。",
    "new_swzz": "覆盖疫苗、血液制品、单抗与生物类似药等，研发周期长、技术与审批壁垒高，受创新药审批节奏与医保谈判影响大。",
    "new_dzqj": "包括面板、LED、被动元件、PCB 与连接器等电子元器件，处于电子产业链中上游，具有明显的周期属性。",
    "new_hghy": "含基础化工原料、精细化工、化纤原料与农化等，产品价格随原油成本与供需周期波动，属典型的强周期行业。",
    "new_fdc": "主营住宅与商业地产的开发、销售及运营，受土地政策、信贷环境与限购政策驱动，现金流与资产负债率是关键指标。",
    "new_qczz": "覆盖整车、汽车零部件与新能源车产业链，受汽车销量周期、新能源渗透率与出口景气影响。",
    "new_stock": "上市时间较短的个股集合，流通盘小、波动大，常为市场情绪与资金活跃度的风向标。",
    "new_sybh": "包含百货、超市、连锁零售与免税等业态，重点关注同店增速、客流变化与线上化渠道变革。",
    "new_jzjc": "含建筑施工、建材、装饰装修与基建工程，与固定资产投资、地产新开工强相关。",
    "new_jtys": "覆盖物流、快递、航运、航空与港口，业绩受运价、油价与国际贸易景气共同驱动。",
    "new_ysjs": "包括铜铝铅锌、稀土、贵金属与小金属的采选冶炼，金属价格主导业绩，强周期属性突出。",
    "new_dlhy": "主营火电、水电、核电与新能源发电运营，关注上网电价、利用小时数与装机结构变化。",
    "new_fdsb": "涵盖风电、光伏、核电与输变电设备，受益于新能源装机放量与电网投资扩张。",
    "new_nlmy": "含种植、养殖（生猪/禽类）、饲料与种业，猪周期是影响板块盈利的核心变量。",
    "new_dqhy": "包含白色家电、厨电与小家电，需求受地产后周期与出口景气影响。",
    "new_sphy": "覆盖调味品、乳制品、休闲食品与食品加工，需求相对刚性，关注提价能力与原材料成本。",
    "new_gthy": "主营长材、板材的冶炼与加工，盈利随钢价与铁矿石成本之间的剪刀差波动。",
    "new_qtxy": "未能归入其他主流行业分类的杂项公司集合，行业属性分散。",
    "new_yqyb": "包括测量、分析与工业自动化仪表，受益于高端制造升级与关键仪器国产替代。",
    "new_jrhy": "覆盖银行、保险、券商与信托等，关注净息差、资产质量与资本市场活跃度。",
    "new_fzxl": "含品牌服饰、运动鞋服与家纺，关注终端零售流水与库存周转。",
    "new_nyhf": "包含化肥、农药与磷化工，受农产品价格与出口配额政策影响。",
    "new_mthy": "主营动力煤、焦煤的开采与洗选，煤炭价格与长协比例主导盈利表现。",
    "new_cmyl": "覆盖影视、游戏、出版与广告营销，受内容监管、版号发放与票房周期影响。",
    "new_fzhy": "含棉纺、毛纺与面料织造，出口需求与棉花价格是核心变量。",
    "new_ljhy": "包含白酒、啤酒与黄酒，高端白酒具备较强品牌壁垒与提价能力。",
    "new_jdhy": "与电器行业相近，覆盖白电、黑电及家电零部件。",
    "new_jdly": "含酒店、景区与旅游服务，受居民出行景气与客流恢复驱动。",
    "new_slzp": "包括塑料管道、改性材料与包装制品，原材料成本随油价波动。",
    "new_ylqx": "覆盖影像设备、体外诊断、高值耗材与家用器械，受集采政策与创新审批影响。",
    "new_zhhy": "业务多元、横跨多个行业的综合类公司集合。",
    "new_hbhy": "涵盖水务、固废处理、烟气治理与环保工程，依赖政策驱动与运营现金流。",
    "new_gsgq": "主营城市供水、燃气与供热，具公用事业属性，现金流相对稳定。",
    "new_snhy": "主营水泥及熟料生产，与基建地产开工需求及错峰限产政策相关。",
    "new_hqhy": "包括涤纶、锦纶与粘胶等化纤原料，受油价与下游纺服需求驱动。",
    "new_zzhy": "覆盖文化纸、包装纸与特种纸，木浆与废纸成本是关键变量。",
    "new_syhy": "含油气勘探开采、油服与炼化，原油价格是板块业绩的核心变量。",
    "new_wzwm": "主营大宗商品贸易与进出口代理，受汇率波动与贸易政策影响。",
    "new_blhy": "主营浮法玻璃与光伏玻璃，受地产竣工需求与光伏装机双轮驱动。",
    "new_glql": "包含高速公路运营与路桥施工，具类债券属性，现金流稳定。",
    "new_ysbz": "覆盖包装印刷与纸包装，需求跟随下游消费品景气波动。",
    "new_jjhy": "含定制家居与成品家具，地产后周期属性明显。",
    "new_fjzz": "主营航空器整机、航空零部件与军工配套，受国防预算与订单节奏驱动。",
    "new_kfq": "主营园区开发与运营，受地方招商引资与土地政策影响。",
    "new_cbzz": "主营船舶与海洋工程装备，订单周期长，受航运运价与船龄更新需求驱动。",
    "new_fzjx": "主营纺织专用设备制造，跟随纺服产能扩张与设备更新周期。",
    "new_mtc": "包含摩托车及零部件，重点关注出口景气与电动化替代。",
    "new_tchy": "主营建筑陶瓷与日用陶瓷，受地产后周期需求与出口驱动。",
}



def _load_members() -> dict[str, list[dict]]:
    """{board_code: [{ts_code, circ_mv, total_mv, chg20}]}"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT board_code, ts_code, circ_mv, total_mv, chg20
            FROM stock_board_member
        """)).mappings().all()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["board_code"]].append(dict(r))
    return dict(out)


def _load_board_names() -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT board_code, board_name FROM stock_board_basic
            WHERE trade_date=(SELECT MAX(trade_date) FROM stock_board_basic)
        """)).fetchall()
    return {c: n for c, n in rows}


def _fetch_member_quotes(ts_codes: list[str]) -> dict[str, list[dict]]:
    """{ts_code: [{trade_date, pct_chg, close, vol, amount}]} 按日期升序"""
    out: dict[str, list[dict]] = defaultdict(list)
    # IN 列表分批, 防 SQL 过长
    B = 500
    with engine.connect() as conn:
        for i in range(0, len(ts_codes), B):
            batch = ts_codes[i:i + B]
            marks = ",".join(f":c{j}" for j in range(len(batch)))
            args = {f"c{j}": c for j, c in enumerate(batch)}
            rows = conn.execute(text(f"""
                SELECT ts_code, trade_date, pct_chg, close, vol, amount
                FROM stock_daily_quote WHERE ts_code IN ({marks})
                ORDER BY ts_code, trade_date
            """), args).mappings().all()
            for r in rows:
                out[r["ts_code"]].append({
                    "trade_date": str(r["trade_date"]),
                    "pct_chg": float(r["pct_chg"]) if r["pct_chg"] is not None else None,
                    "close": float(r["close"]) if r["close"] is not None else None,
                    "vol": float(r["vol"]) if r["vol"] is not None else None,
                    "amount": float(r["amount"]) if r["amount"] is not None else None,
                })
    return dict(out)


def _percentile_ranks(values: dict[str, float | None]) -> dict[str, float | None]:
    """升序分位（0~1），None 保持 None"""
    valid = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(valid)
    out = {k: None for k in values}
    for rank, (v, k) in enumerate(valid):
        out[k] = (rank + 1) / n if n else None
    return out


def synth_board_daily() -> dict:
    """全量重算板块日线（按板块全量替换：先删该板块旧行再写，避免门槛剔除的行残留）。

    覆盖天数 = 成分股在 stock_daily_quote 里的实际日期全集，但需满足**成分覆盖门槛**：
    当日有效成分股数 >= max(DAILY_MIN_MEMBERS, DAILY_MIN_COVER_PCT × 成分股数)，
    否则该日不出行（早期日线深度不足时，少数几只股票不能代表整个板块）。

    口径提示：按**最新成分股名单**回溯计算。未上市成分股在早期交易日无行情，自动不进
    入分子/分母，故不存在「未上市却被计入」的偏差；残余局限是历史成分变更与退市股无法
    还原（需外部时点成分数据），详情见 docs/06。
    """
    members = _load_members()
    if not members:
        raise RuntimeError("[boardsynth] stock_board_member 为空, 请先跑 --board")
    names = _load_board_names()
    print(f"[boardsynth] 板块 {len(members)} 个, 开始合成日线", flush=True)

    all_rows = []
    member_tags = []           # (board_code, ts_code, corr60, rel_tag, leader_tag, leader_score)
    board_stats = {}           # board_code -> {chg20, pct_chg_latest}
    skipped_days = [0]         # 因成分覆盖不足而未出行的 板块-日 数
    t0 = time.time()

    for bi, (bc, mem) in enumerate(sorted(members.items()), 1):
        weights = {m["ts_code"]: float(m["circ_mv"] or 0) for m in mem}
        codes = [m["ts_code"] for m in mem]
        # 覆盖度门槛：少于 max(3, 20%成分股数) 只有行情的交易日不出行
        min_need = max(DAILY_MIN_MEMBERS,
                       int(math.ceil(len(codes) * DAILY_MIN_COVER_PCT)))
        quotes = _fetch_member_quotes(codes)
        # 日期全集（升序）
        dates = sorted({q["trade_date"] for qs in quotes.values() for q in qs})
        if not dates:
            continue
        # 每股 {date: row}
        by_code = {c: {q["trade_date"]: q for q in qs} for c, qs in quotes.items()}

        idx_close, prev = [], None
        day_ret: dict[str, float] = {}          # 板块日收益（加权）
        day_ret_eq: dict[str, float] = {}
        day_vol: dict[str, float] = defaultdict(float)
        day_amt: dict[str, float] = defaultdict(float)
        member_ret: dict[str, dict[str, float]] = defaultdict(dict)

        valid_dates: list[str] = []
        n_skipped = 0
        for d in dates:
            wsum = 0.0
            racc = 0.0
            rets = []
            k_avail = 0
            for c in codes:
                q = by_code.get(c, {}).get(d)
                if not q or q["pct_chg"] is None:
                    continue
                k_avail += 1
                r = q["pct_chg"] / 100.0
                member_ret[c][d] = r
                rets.append(r)
                w = weights.get(c) or 0.0
                if w > 0:
                    racc += w * r
                    wsum += w
                if q["vol"]:
                    day_vol[d] += q["vol"]
                if q["amount"]:
                    day_amt[d] += q["amount"]
            if k_avail < min_need:
                n_skipped += 1
                continue
            rw = racc / wsum if wsum > 0 else (sum(rets) / len(rets) if rets else 0.0)
            req = sum(rets) / len(rets) if rets else 0.0
            day_ret[d] = rw
            day_ret_eq[d] = req
            cur = BASE_INDEX if prev is None else prev * (1 + rw)
            idx_close.append((d, cur))
            valid_dates.append(d)
            prev = cur

        closes = [c for _, c in idx_close]
        n = len(idx_close)
        rows_b = []
        cont = 0
        cont_acc = 0.0
        for i, (d, close) in enumerate(idx_close):
            pc = closes[i - 1] if i > 0 else None
            pchg = day_ret[d] * 100
            pchg_eq = day_ret_eq[d] * 100
            if i == 0:
                cont, cont_acc = 0, 0.0
            elif pchg > 0:
                cont = cont + 1 if cont > 0 else 1
                cont_acc = cont_acc + pchg if cont > 1 else pchg
            elif pchg < 0:
                cont = cont - 1 if cont < 0 else -1
                cont_acc = cont_acc + pchg if cont < -1 else pchg
            else:
                cont, cont_acc = 0, 0.0
            # 均线: 窗口不足 w 根 -> None。原实现用 len(切片) 做分母，i<w-1 时会拿
            # "短窗均值"冒充 MA5/MA10/MA20（如第 2 根就给出 ma20），与
            # stock_daily_quote / stock_index_daily「满窗才有值」的口径不一致（2026-09-16 修）。
            ma = lambda w: (sum(closes[i - w + 1:i + 1]) / w) if i >= w - 1 else None
            rows_b.append({
                "board_code": bc, "trade_date": d,
                "open": pc if pc is not None else close,
                "high": max(pc, close) if pc is not None else close,
                "low": min(pc, close) if pc is not None else close,
                "close": round(close, 3),
                "pre_close": round(pc, 3) if pc is not None else None,
                "pct_chg": round(pchg, 3), "pct_chg_eq": round(pchg_eq, 3),
                "vol": day_vol.get(d), "amount": day_amt.get(d),
                "ma5": round(ma(5), 3) if ma(5) else None,
                "ma10": round(ma(10), 3) if ma(10) else None,
                "ma20": round(ma(20), 3) if ma(20) else None,
                "cont_days": cont, "cont_pct": round(cont_acc, 4),
            })
        if not rows_b:
            print(f"[boardsynth] {bc} 全部交易日成分覆盖不足, 跳过", flush=True)
            continue
        if n_skipped:
            skipped_days[0] += n_skipped
        all_rows.extend(rows_b)
        latest = rows_b[-1]
        chg20 = None
        if n >= 21 and closes[-21]:
            chg20 = round((closes[-1] / closes[-21] - 1) * 100, 4)
        board_stats[bc] = {"chg20": chg20, "pct_chg": latest["pct_chg"]}

        # ---- corr60 + 龙头打分 ----
        bd = valid_dates[-CORR_WINDOW:]
        bret = [day_ret[d] for d in bd]
        mb = sum(bret) / len(bret)
        sb = sum((x - mb) ** 2 for x in bret) ** 0.5
        # 近20日日均成交额（板块内占比打分用）
        d20 = valid_dates[-20:]
        amt20 = {}
        for c in codes:
            qs = by_code.get(c, {})
            amts = [qs[d]["amount"] for d in d20
                    if qs.get(d) and qs[d]["amount"] is not None]
            amt20[c] = sum(amts) / len(amts) if amts else None
            # corr60
            mret = [member_ret[c].get(d) for d in bd]
            pairs = [(a, b) for a, b in zip(mret, bret) if a is not None]
            corr = None
            if len(pairs) >= 20 and sb > 0:
                ma_ = sum(p[0] for p in pairs) / len(pairs)
                mb_ = sum(p[1] for p in pairs) / len(pairs)
                sa = sum((p[0] - ma_) ** 2 for p in pairs) ** 0.5
                sb2 = sum((p[1] - mb_) ** 2 for p in pairs) ** 0.5
                if sa > 0 and sb2 > 0:
                    corr = (sum((p[0] - ma_) * (p[1] - mb_) for p in pairs)
                            / (sa * sb2))
            member_tags.append([bc, c, corr, None, None, None])

        w_mv = _percentile_ranks({c: weights.get(c) or None for c in codes})
        w_chg = _percentile_ranks({m["ts_code"]: m["chg20"] for m in mem})
        w_amt = _percentile_ranks(amt20)
        scores = {}
        for c in codes:
            parts = [w_mv.get(c), w_chg.get(c), w_amt.get(c)]
            if any(p is None for p in parts):
                scores[c] = None
            else:
                scores[c] = round(0.4 * parts[0] + 0.3 * parts[1]
                                  + 0.3 * parts[2], 4)
        ranked = sorted((s, c) for c, s in scores.items() if s is not None)
        leaders = {c for _, c in ranked[-LEADER_TOP_N:]}
        corrs = sorted((t[2] for t in member_tags
                        if t[0] == bc and t[2] is not None), reverse=True)
        cutoff = corrs[int(len(corrs) * CORE_TOP_PCT)] if corrs else None
        for t in member_tags:
            if t[0] != bc:
                continue
            t[5] = scores.get(t[1])
            t[4] = "leader" if t[1] in leaders else "normal"
            if t[2] is not None and (t[2] >= CORE_CORR_MIN
                                     or (cutoff is not None and t[2] >= cutoff)):
                t[3] = "core"
            else:
                t[3] = "normal"

        if bi % 10 == 0:
            print(f"[boardsynth] 板块进度 {bi}/{len(members)}", flush=True)

    print(f"[boardsynth] 日线行数 {len(all_rows)}, 落表...", flush=True)
    with engine.begin() as conn:
        # 按板块全量替换：先删本次重算到的板块旧行，避免被「成分覆盖门槛」
        # 剔除的早期行残留在表里（原实现只有 UPSERT，不会删）。
        bcs = sorted({r["board_code"] for r in all_rows})
        if bcs:
            conn.execute(
                text("DELETE FROM stock_board_daily_quote WHERE board_code IN :bcs")
                .bindparams(bindparam("bcs", expanding=True)),
                {"bcs": bcs})
            print(f"[boardsynth] 已清理 {len(bcs)} 个板块旧行, 重新写入",
                  flush=True)
        B = 2000
        for i in range(0, len(all_rows), B):
            conn.execute(text("""
                INSERT INTO stock_board_daily_quote
                    (board_code, trade_date, open, high, low, close, pre_close,
                     pct_chg, pct_chg_eq, vol, amount, ma5, ma10, ma20,
                     cont_days, cont_pct)
                VALUES (:board_code, :trade_date, :open, :high, :low, :close,
                        :pre_close, :pct_chg, :pct_chg_eq, :vol, :amount,
                        :ma5, :ma10, :ma20, :cont_days, :cont_pct)
                ON DUPLICATE KEY UPDATE
                    open=VALUES(open), high=VALUES(high), low=VALUES(low),
                    close=VALUES(close), pre_close=VALUES(pre_close),
                    pct_chg=VALUES(pct_chg), pct_chg_eq=VALUES(pct_chg_eq),
                    vol=VALUES(vol), amount=VALUES(amount), ma5=VALUES(ma5),
                    ma10=VALUES(ma10), ma20=VALUES(ma20),
                    cont_days=VALUES(cont_days), cont_pct=VALUES(cont_pct),
                    etl_update_time=NOW()
            """), all_rows[i:i + B])
        # 成员标签回写
        B2 = 2000
        for i in range(0, len(member_tags), B2):
            conn.execute(text("""
                UPDATE stock_board_member
                SET corr60=:corr, rel_tag=:rel, leader_tag=:leader,
                    leader_score=:score, etl_update_time=NOW()
                WHERE board_code=:bc AND ts_code=:tc
            """), [{"bc": t[0], "tc": t[1], "corr": (round(t[2], 4)
                    if t[2] is not None else None), "rel": t[3],
                    "leader": t[4], "score": t[5]}
                   for t in member_tags[i:i + B2]])

    _update_summaries(members, board_stats, names)
    if skipped_days[0]:
        print(f"[boardsynth] 成分覆盖不足未出行 {skipped_days[0]} 个板块-日 "
              f"(门槛 max({DAILY_MIN_MEMBERS}, {DAILY_MIN_COVER_PCT:.0%}×成分股数))", flush=True)
    print(f"[boardsynth] 完成, 耗时 {time.time() - t0:.1f}s", flush=True)
    return {"boards": len(members), "rows": len(all_rows)}


def _update_summaries(members: dict, board_stats: dict, names: dict) -> None:
    """板块概念解读：BOARD_DESC 行业知识库 + 实时统计（外部抓取不可用，降级为本地生成）"""
    with engine.connect() as conn:
        lead_rows = conn.execute(text("""
            SELECT m.board_code, b.name
            FROM stock_board_member m
            JOIN stock_basic b ON b.ts_code = m.ts_code
            WHERE m.leader_tag='leader'
            ORDER BY m.board_code, m.leader_score DESC
        """)).fetchall()
    from collections import defaultdict
    leads: dict[str, list] = defaultdict(list)
    for bc, nm in lead_rows:
        leads[bc].append(nm)
    td_row = None
    with engine.connect() as conn:
        td_row = conn.execute(text(
            "SELECT MAX(trade_date) FROM stock_board_basic")).scalar()
    if not td_row:
        return
    upd = []
    for bc, mem in members.items():
        n = len(mem)
        tmv = sum(float(m["total_mv"] or 0) for m in mem) / 1e8
        cmv = sum(float(m["circ_mv"] or 0) for m in mem) / 1e8
        st = board_stats.get(bc, {})
        lead_txt = "、".join(leads.get(bc, [])[:3]) or "暂无"
        chg = st.get("chg20")
        chg_txt = f"{chg:+.2f}%" if chg is not None else "数据不足"
        desc = BOARD_DESC.get(bc) or f"{names.get(bc, bc)}行业板块。"
        summary = (f"{desc}"
                   f"（板块共{n}只成分股，总市值约{tmv:.0f}亿元、流通市值约{cmv:.0f}亿元；"
                   f"按流通市值/近20日涨幅/成交额综合打分，当前龙头股为：{lead_txt}；"
                   f"板块指数近20日涨跌幅{chg_txt}。）")
        upd.append({"s": summary[:1000], "bc": bc, "td": str(td_row)})
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE stock_board_basic SET summary=:s, etl_update_time=NOW()
            WHERE board_code=:bc AND trade_date=:td
        """), upd)


def _preclose_map(codes: list[str], trade_date: str) -> dict[str, float]:
    """{ts_code: 昨收} —— trade_date 之前的最近一个日线收盘"""
    out: dict[str, float] = {}
    B = 500
    with engine.connect() as conn:
        for i in range(0, len(codes), B):
            batch = codes[i:i + B]
            marks = ",".join(f":c{j}" for j in range(len(batch)))
            args = {f"c{j}": c for j, c in enumerate(batch)}
            args["td"] = trade_date
            rows = conn.execute(text(f"""
                SELECT q.ts_code, q.close FROM stock_daily_quote q
                JOIN (SELECT ts_code, MAX(trade_date) md FROM stock_daily_quote
                      WHERE trade_date < :td AND ts_code IN ({marks})
                      GROUP BY ts_code) t
                  ON t.ts_code=q.ts_code AND t.md=q.trade_date
            """), args).fetchall()
            for tc, cl in rows:
                if cl:
                    out[tc] = float(cl)
    return out


def _fetch_day_bars(codes: set, trade_date: str) -> dict:
    """单日 5 分钟 bar -> {ts_code: {bar_time_str: row}}
    按 bar_time 范围扫描（走 bar_time 索引），Python 侧只留成分股。
    """
    t0 = f"{trade_date} 00:00:00"
    t1 = (datetime.strptime(trade_date, "%Y-%m-%d")
          + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    out: dict = defaultdict(dict)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT ts_code, bar_time, open, high, low, close, vol
            FROM stock_minute_quote
            WHERE scale=5 AND bar_time>=:t0 AND bar_time<:t1
        """), {"t0": t0, "t1": t1}).mappings().all()
    for r in rows:
        tc = r["ts_code"]
        if tc in codes:
            out[tc][str(r["bar_time"])] = r
    return out


def synth_board_minutes(trade_date: str = None, all_days: bool = False) -> dict:
    """合成板块 5 分钟分时（成分股流通市值加权, 相对昨收归一, 基期=1000）。

    - 默认只合成库内最新分时日；`all_days=True` 合成库内最近 MINUTE_KEEP_DAYS
      个有分时数据的交易日的全部（需求#19-3「分时保留最近 30 个交易日」）。
    - 落库后清理 MINUTE_KEEP_DAYS 个交易日之外的板块分时。
    """
    with engine.connect() as conn:
        # 只取"全市场级"分时日（成分股覆盖足够），排除调试期的零星分时残留
        src_days = [str(r[0]) for r in conn.execute(text("""
            SELECT DATE(bar_time) d FROM stock_minute_quote
            WHERE scale=5
            GROUP BY DATE(bar_time)
            HAVING COUNT(DISTINCT ts_code) >= :min_cov
            ORDER BY d DESC LIMIT :k
        """), {"k": MINUTE_KEEP_DAYS, "min_cov": MINUTE_DAY_MIN_CODES}).fetchall()]
    if not src_days:
        return {"rows": 0, "days": 0, "trade_date": None, "days_done": []}

    if trade_date:
        targets = [str(trade_date)]
    elif all_days:
        targets = sorted(src_days)
    else:
        targets = [src_days[0]]

    members = _load_members()
    if not members:
        raise RuntimeError("[boardmin] stock_board_member 为空, 请先跑 --board")

    weights_by_board = {bc: {m["ts_code"]: float(m["circ_mv"] or 0) for m in mem}
                        for bc, mem in members.items()}
    all_codes = {c for w in weights_by_board.values() for c in w}

    out_rows: list[dict] = []
    days_done: list[str] = []
    t_start = time.time()
    for td in targets:
        preclose = _preclose_map(sorted(all_codes), td)
        day_bars = _fetch_day_bars(all_codes, td)
        if not day_bars:
            print(f"[boardmin] {td} 无个股分时, 跳过", flush=True)
            continue
        n_day = 0
        for bc, weights in sorted(weights_by_board.items()):
            codes = [c for c in weights if c in preclose and c in day_bars]
            if not codes:
                continue
            bar_times = sorted({bt for c in codes for bt in day_bars[c]})

            def wavg(bt: str, field: str):
                a = w2 = 0.0
                for c in codes:
                    row = day_bars[c].get(bt)
                    if row is None:
                        continue
                    w = weights.get(c) or 0.0
                    pc = preclose.get(c)
                    v = row[field]
                    if w > 0 and pc and v is not None:
                        a += w * (float(v) / pc)
                        w2 += w
                return BASE_INDEX * (a / w2) if w2 > 0 else None

            for bt in bar_times:
                close_v = wavg(bt, "close")
                if close_v is None:
                    continue
                vol = 0.0
                for c in codes:
                    row = day_bars[c].get(bt)
                    if row is not None and row["vol"] is not None:
                        vol += float(row["vol"])
                out_rows.append({
                    "board_code": bc, "trade_date": td, "bar_time": bt,
                    "open": wavg(bt, "open") or close_v,
                    "high": wavg(bt, "high") or close_v,
                    "low": wavg(bt, "low") or close_v,
                    "close": round(close_v, 3),
                    "vol": vol or None, "amount": None,
                })
                n_day += 1
        days_done.append(td)
        print(f"[boardmin] {td} 板块分时 {n_day} 行 "
              f"({len(days_done)}/{len(targets)} 天, {time.time()-t_start:.0f}s)",
              flush=True)

    with engine.begin() as conn:
        # 清理保留窗口外的旧板块分时
        marks = ",".join(f":d{j}" for j in range(len(src_days)))
        args = {f"d{j}": d for j, d in enumerate(src_days)}
        conn.execute(text(f"""
            DELETE FROM stock_board_minute_quote
            WHERE trade_date NOT IN ({marks})
        """), args)
        B = 2000
        for i in range(0, len(out_rows), B):
            conn.execute(text("""
                INSERT INTO stock_board_minute_quote
                    (board_code, trade_date, bar_time, open, high, low, close,
                     vol, amount)
                VALUES (:board_code, :trade_date, :bar_time, :open, :high,
                        :low, :close, :vol, :amount)
                ON DUPLICATE KEY UPDATE
                    open=VALUES(open), high=VALUES(high), low=VALUES(low),
                    close=VALUES(close), vol=VALUES(vol)
            """), out_rows[i:i + B])
    print(f"[boardmin] 完成: {len(days_done)} 个交易日 / {len(out_rows)} 行, "
          f"保留窗口 {len(src_days)} 天, 耗时 {time.time()-t_start:.1f}s", flush=True)
    return {"rows": len(out_rows), "days": len(days_done),
            "trade_date": days_done[-1] if days_done else None,
            "days_done": days_done}
