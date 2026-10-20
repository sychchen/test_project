# -*- coding: utf-8 -*-
"""采集入口 CLI
用法:
  python run_collect.py --init-db                      建表
  python run_collect.py --basic                        同步股票列表+基本面
  python run_collect.py --listdate [--limit N]         补缺上市日期(首次全量, 后续仅补新股)
  python run_collect.py --quotes [--days 2]            增量采集日线(默认最近2个交易日)
  python run_collect.py --quotes --start-date 2026-08-01 --end-date 2026-08-31
                                                       采集指定时间段日线
  python run_collect.py --valsnap                      估值快照入日线表(PE/PB/市值, 须在quotes后)
  python run_collect.py --indicators [--days N]        重算衍生指标(连涨天数/连涨涨幅/量比/换手率,
                                                       须在 quotes+valsnap 之后; 空=全表)
  python run_collect.py --index [--days 40]            采集主流指数日线
  python run_collect.py --moneyflow [--days 40]        采集个股资金流向(日常增量传 --days 3)
  python run_collect.py --minutes [--scale 5] [--days 1]
                                                       采集分时(分钟)行情
  python run_collect.py --minbackfill-sina [--min-days 30]
                                                       历史分时回填(新浪, 默认最近30个交易日)
  python run_collect.py --histbackfill-sina [--hist-sina-count 1030]
                                                       日线历史向前扩展(新浪, 约4.2年/1030根)
  python run_collect.py --tags                         刷新行业/概念/指数标签(新浪)
  python run_collect.py --emconcepts                   刷新东财概念板块(PCB/CPO等, 合并入concepts)
  python run_collect.py --ztpool [--days 3]            涨停池+强势股池(东财, 默认最新交易日)
  python run_collect.py --valuation [--val-days N] [--full]
                                                       估值历史(东财, 默认增量仅补新行; --full 首次全量, 建议后台)
  python run_collect.py --all                          上述全部(除init-db/valuation)
  调试: --limit N 只处理前 N 只
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import init_schema
from app.collector.basic_collector import (
    sync_stock_basic, sync_list_dates, snapshot_valuation)
from app.collector.quote_collector import sync_daily_quotes, backfill_history
from app.collector.quote_collector import backfill_history_sina
from app.collector.minute_collector import (
    sync_minute_quotes, backfill_minutes_sina, MIN_BACKFILL_BARS)
from app.collector.tag_collector import sync_tags
from app.collector.em_concept_collector import sync_em_concepts
from app.collector.index_collector import sync_index_daily
from app.collector.moneyflow_collector import sync_money_flow
from app.collector.indicator_collector import refresh as refresh_indicators
from app.collector.board_collector import sync_board_basic
from app.collector.board_synth import synth_board_daily, synth_board_minutes
from app.collector.darkpool_collector import (
    sync_block_trade, sync_billboard, refresh_dark_indicator)
from app.collector.akshare_collector import (
    sync_zt_pool, sync_valuation, sync_money_flow_ths,
    sync_sw_industry_member, sync_fin_report,
    sync_hot_rank, sync_margin_detail)
from app.collector.adjust_collector import (
    seed_adjust_factors, refresh_adjust_events)
from app.collector.valuation_pctl import refresh_valuation_pctl
from app.collector.share_change_collector import sync_share_change

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description="选股系统-数据采集")
    ap.add_argument("--init-db", action="store_true", help="初始化建表")
    ap.add_argument("--basic", action="store_true", help="同步股票列表+基本面")
    ap.add_argument("--listdate", action="store_true", help="补缺上市日期(F10)")
    ap.add_argument("--quotes", action="store_true", help="采集日线行情")
    ap.add_argument("--valsnap", action="store_true",
                    help="估值快照入日线表(PE/PB/市值, 同源拷贝自 stock_basic)")
    ap.add_argument("--histbackfill", action="store_true",
                    help="向前扩展日线历史(腾讯, 约4年/1023根; 只写早于库内最早日的行, 之后需重跑 --boardsynth)")
    ap.add_argument("--hist-count", type=int, default=None,
                    help="配合 --histbackfill: 每股拉取根数(默认1023, 腾讯上限)")
    ap.add_argument("--hist-workers", type=int, default=None,
                    help="配合 --histbackfill: 并发线程数(默认6; 提高易触发腾讯WAF)")
    ap.add_argument("--index", action="store_true", help="采集指数日线行情")
    ap.add_argument("--moneyflow", action="store_true", help="采集个股资金流向")
    ap.add_argument("--indicators", action="store_true",
                    help="重算日频衍生指标(连涨天数/连涨涨幅/量比/换手率; 须在 quotes+valsnap 之后)")
    ap.add_argument("--minutes", action="store_true", help="采集分时(分钟)行情")
    ap.add_argument("--minbackfill-sina", action="store_true",
                    help="历史分时回填(新浪, 覆盖最近 --min-days 个交易日; 双域名轮询, 可反复重跑)")
    ap.add_argument("--min-datalen", type=int, default=None,
                    help="配合 --minbackfill-sina: 单票拉取根数(默认1500≈32个交易日)")
    ap.add_argument("--min-days", type=int, default=30,
                    help="配合 --minbackfill-sina: 目标覆盖的交易日数(默认30)")
    ap.add_argument("--min-interval", type=float, default=None,
                    help="配合 --minbackfill-sina: 单域名请求最小间隔秒(默认1.2≈0.83req/s; 调小更快但更易吃456)")
    ap.add_argument("--min-host", default="both", choices=["both", "vip", "money"],
                    help="配合 --minbackfill-sina: 限定新浪域名(默认both; 与 --hist-host 分工可并行)")
    ap.add_argument("--min-members-only", action="store_true",
                    help="配合 --minbackfill-sina: 只回填有板块归属的股票"
                         "(板块分时合成只需这些, 池 2563 只 vs 全市场 5218 只)")
    ap.add_argument("--histbackfill-sina", action="store_true",
                    help="日线历史向前扩展(新浪, 约4.2年; 只写早于库内最早日的行, 之后需重跑 --boardsynth)")
    ap.add_argument("--hist-sina-count", type=int, default=None,
                    help="配合 --histbackfill-sina: 单票拉取根数(默认1030≈4.2年)")
    ap.add_argument("--hist-host", default="both", choices=["both", "vip", "money"],
                    help="配合 --histbackfill-sina: 限定新浪域名(默认both)")
    ap.add_argument("--tags", action="store_true", help="刷新行业/概念/指数标签(新浪)")
    ap.add_argument("--emconcepts", action="store_true",
                    help="刷新东财概念板块(合入concepts列; --limit 调试=只采前N个板块)")
    ap.add_argument("--board", action="store_true",
                    help="板块基础采集(新浪行业快照+成分映射+人气; --limit 调试=前N个板块)")
    ap.add_argument("--skip-members", action="store_true",
                    help="配合 --board: 跳过成分映射刷新, 只更快照")
    ap.add_argument("--boardsynth", action="store_true",
                    help="板块日线合成(流通市值加权全量覆盖, 须在 --board 之后)")
    ap.add_argument("--boardmin", action="store_true",
                    help="板块分时合成(默认库内最近30个有分时的交易日, 保留近30交易日)")
    ap.add_argument("--boardmin-one", action="store_true",
                    help="配合 --boardmin: 只合成最新一个分时日")
    ap.add_argument("--darkpool", action="store_true",
                    help="暗盘采集(大宗交易+龙虎榜+席位; --days N 回看天数, 默认7)")
    ap.add_argument("--darkind", action="store_true",
                    help="暗盘派生指标重算(默认最近10个有数据的交易日; 须在 --darkpool 之后)")
    ap.add_argument("--darkind-all", action="store_true",
                    help="配合/替代 --darkind: 重算库内**全部**有数据的交易日(历史回填后用)")
    ap.add_argument("--ztpool", action="store_true",
                    help="涨停池+强势股池(东财, akshare; --days N 回看最近N个交易日, 默认1)")
    ap.add_argument("--valuation", action="store_true",
                    help="估值历史(东财, akshare; 默认增量仅补新行, 不进 --all; "
                         "--val-days N 只写最近N行; --full 首次全量, 建议后台+分片)")
    ap.add_argument("--val-days", type=int, default=None,
                    help="配合 --valuation: 每股只写最近N行(覆盖写)")
    ap.add_argument("--valtopup", action="store_true",
                    help="估值日度顶格: daily quote 估值快照列 UPSERT 进 stock_valuation(须在 valsnap 后)")
    ap.add_argument("--valpctl", action="store_true",
                    help="估值分位重算落库 stock_valuation_pctl(近5年窗口, ~5分钟, 须在 valtopup 后)")
    ap.add_argument("--thsmoney", action="store_true",
                    help="同花顺个股资金流日度快照(全市场~12s, 盘后采, 与新浪资金流交叉验证)")
    ap.add_argument("--swmember", action="store_true",
                    help="申万一级行业成分全量刷新(31行业, 破新浪2563只成分上限; 慢变量建议周度)")
    ap.add_argument("--finreport", action="store_true",
                    help="业绩报表(东财按报告期批量页翻, ~23页/季6s; 默认最新报告期, "
                         "--quarters N 首次回填N季)")
    ap.add_argument("--quarters", type=int, default=1,
                    help="配合 --finreport: 回看最近N个报告期(默认1)")
    ap.add_argument("--adjseed", action="store_true",
                    help="复权因子一次性回填(腾讯qfq vs 库内不复权, ~20-25分钟, 建议后台; "
                         "已seed股票自动跳过, 可断点续跑)")
    ap.add_argument("--adjdaily", action="store_true",
                    help="复权因子日常增量(零请求预警: 派生vs THS官方涨跌幅偏差>0.2pp的"
                         "候选股逐股qfq验证; 须在 quotes+thsmoney 之后)")
    ap.add_argument("--hotrank", action="store_true",
                    help="东财个股人气榜Top100日度快照(1请求, 无历史可回填, 逐日累积)")
    ap.add_argument("--margin", action="store_true",
                    help="个股融资融券明细(交易所官网, 各1请求/日; 历史回填走后台)")
    ap.add_argument("--sharechange", action="store_true",
                    help="董监高持股变动(东财 datacenter; 默认增量, --full 首次全量)")
    ap.add_argument("--all", action="store_true",
                    help="basic+listdate+tags+emconcepts+quotes+valsnap+index+moneyflow"
                         "+board+boardsynth+boardmin+darkpool+darkind+ztpool+minutes"
                         "+thsmoney+finreport+adjdaily+hotrank+margin 全采; "
                         "valtopup/valpctl 由 daily_update 串接, 不进 --all")
    ap.add_argument("--days", type=int, default=None,
                    help="日K: 最近N个交易日(默认2); 分时: 最近N天(默认3, 自愈漏跑); "
                         "指数: 最近N根(默认40); 资金流: 每股最近N天(--all时默认3)")
    ap.add_argument("--scale", type=int, default=5, help="分时周期(分钟): 5/15/30/60")
    ap.add_argument("--full", action="store_true", help="配合 --valuation: 首次全量覆盖(非仅补新行)")
    ap.add_argument("--shard", type=int, default=0, help="配合 --valuation: 分片序号(0起)")
    ap.add_argument("--nshards", type=int, default=1, help="配合 --valuation: 总分片数(并行进程数)")
    ap.add_argument("--start-date", default=None, help="区间采集起始日 YYYY-MM-DD 或 YYYYMMDD")
    ap.add_argument("--end-date", default=None, help="区间采集结束日, 默认同起始日")
    ap.add_argument("--limit", type=int, default=None, help="调试用: 只采前N只")
    args = ap.parse_args()

    if args.init_db:
        init_schema(os.path.join(BASE_DIR, "sql", "schema.sql"))
        print("[init-db] schema 初始化完成")

    if args.basic or args.all:
        r = sync_stock_basic()
        print(f"[basic] 同步 {r['synced']} 只股票", flush=True)

    if args.listdate or args.all:
        r = sync_list_dates(limit=args.limit)
        print(f"[listdate] pending={r['pending']} ok={r['ok']} failed={r['failed_count']}",
              flush=True)
        if r["failed_list"]:
            print(f"[listdate] 失败样例: {r['failed_list'][:5]}", flush=True)

    if args.tags or args.all:
        r = sync_tags()
        print(f"[tags] {r}", flush=True)

    if args.emconcepts or args.all:
        # 东财概念合入 concepts 列, 须在 --tags 之后(否则会被新浪概念覆盖后丢失热点概念)
        r = sync_em_concepts(limit_boards=args.limit)
        print(f"[emconcepts] boards={r['boards']} em_stocks={r['em_stocks']} "
              f"covered={r['concept_covered']}/{r['total']}", flush=True)

    if args.quotes or args.all:
        if args.start_date:
            r = sync_daily_quotes(limit=args.limit,
                                  start_date=args.start_date, end_date=args.end_date)
        else:
            r = sync_daily_quotes(days=args.days or 2, limit=args.limit)
        print(f"[quotes] total={r['total']} ok={r['ok']} failed={r['failed_count']} "
              f"rows={r['rows']} mode={r['mode']}", flush=True)
        if r["failed_list"]:
            print(f"[quotes] 失败样例: {r['failed_list'][:5]}", flush=True)

    if args.valsnap or args.all:
        # 估值日频快照: 须在 quotes 之后(当日日线行已存在), basic 已在前面刷新
        r = snapshot_valuation()
        print(f"[valsnap] trade_date={r['trade_date']} rows={r['rows']} "
              f"换手率={r.get('turnover_rows', 0)}", flush=True)

    if args.indicators or args.all:
        # 衍生指标: 换手率依赖 circ_mv, 须在 valsnap 之后
        r = refresh_indicators(days=args.days or (3 if args.all else None))
        print(f"[indicators] rows={r['rows']} mode={r['mode']} "
              f"elapsed={r['elapsed']}s coverage={r['coverage']}", flush=True)

    if args.histbackfill:
        # 一次性(可反复重跑): 把 stock_daily_quote 往前扩到约 4 年（腾讯单票最多 1023 根）。
        # 按每只股票自己的最早交易日判定新增范围, 不碰已有行; 之后需重跑 --boardsynth。
        r = backfill_history(count=args.hist_count or 1023, limit=args.limit,
                             workers=args.hist_workers or 6)
        print(f"[histbackfill] 新增 {r['rows']} 行, ok={r['ok']} "
              f"无事可做={r['skipped']} failed={r['failed_count']}", flush=True)
        if r["failed_list"]:
            print(f"[histbackfill] 失败样例: {r['failed_list'][:5]}", flush=True)

    if args.index or args.all:
        r = sync_index_daily(days=args.days or 40)
        print(f"[index] total={r['total']} ok={r['ok']} failed={r['failed_count']} "
              f"rows={r['rows']}", flush=True)
        if r["failed_list"]:
            print(f"[index] 失败样例: {r['failed_list'][:5]}", flush=True)

    if args.moneyflow or args.all:
        # 东财批量快照: 全市场一次采完按最近交易日记账(需先跑 quotes), 顺带回填日线成交额
        r = sync_money_flow(limit=args.limit)
        print(f"[moneyflow] trade_date={r['trade_date']} remote={r['remote_total']} "
              f"rows={r['rows']} pages={r['pages']} amount回填={r['amount_rows']} "
              f"listdate补缺={r.get('listdate_rows', 0)}", flush=True)

    # 新浪域名分工: both=双域名轮询; 指定单域名可与另一条回填并行(配额独立)
    _SINA_HOST_MAP = {"both": None,
                      "vip": ["https://vip.stock.finance.sina.com.cn"],
                      "money": ["https://money.finance.sina.com.cn"]}

    if args.histbackfill_sina:
        # 日线历史向前扩展(新浪): 腾讯 WAF 封禁时的替代通道; 幂等, 可反复重跑
        from app.collector.quote_collector import HIST_SINA_BARS as _HSB
        r = backfill_history_sina(count=args.hist_sina_count or _HSB, limit=args.limit,
                                  hosts=_SINA_HOST_MAP[args.hist_host])
        print(f"[hist-sina] reach={r.get('reach')} 池={r.get('universe')} "
              f"跳过={r.get('skipped')} 待扩展={r.get('pending')} ok={r.get('ok')} "
              f"empty={r.get('empty')} failed={r.get('failed')} rows={r.get('rows')} "
              f"剩余={r.get('remaining')} 用时={r.get('elapsed_sec')}s", flush=True)

    if args.minbackfill_sina:
        # 历史分时回填: 不进 --all(工作量大, 按需跑); 窗口自动取最近 min_days 个交易日,
        # 幂等 UPSERT, WAF/456 中断后同命令重跑即可续传
        r = backfill_minutes_sina(target_days=args.min_days, scale=args.scale,
                                  datalen=args.min_datalen or MIN_BACKFILL_BARS,
                                  limit=args.limit,
                                  members_only=args.min_members_only,
                                  hosts=_SINA_HOST_MAP[args.min_host],
                                  **({"interval": args.min_interval} if args.min_interval else {}))
        print(f"[minbackfill-sina] 窗口={r.get('start_day')}~{r.get('end_day')} "
              f"池={r.get('universe')} 目标{r.get('target_days')}日 "
              f"ok={r.get('ok')} empty={r.get('empty')} failed={r.get('failed')} "
              f"rows={r.get('rows')} 跳过={r.get('skipped')} 剩余={r.get('remaining')} "
              f"用时={r.get('elapsed_sec')}s", flush=True)

    if args.minutes or args.all:
        # 默认回溯 3 天: 新浪单票 1 请求即取回整段, 多发无额外成本,
        # 且能让"漏跑一两天"自动自愈(仅 days=1 时漏跑的那天会成为永久空洞)
        r = sync_minute_quotes(scale=args.scale, days=args.days or 3, limit=args.limit)
        print(f"[minutes] total={r['total']} ok={r['ok']} failed={r['failed_count']} "
              f"rows={r['rows']} scale={r['scale']}", flush=True)
        if r["failed_list"]:
            print(f"[minutes] 失败样例: {r['failed_list'][:5]}", flush=True)

    if args.board or args.all:
        # 板块基础(新浪行业): 快照+成分映射+人气; 须在 tags/quotes 之后
        r = sync_board_basic(skip_members=args.skip_members and not args.all,
                             limit_boards=args.limit if not args.all else None)
        print(f"[board] boards={r['boards']} members={r['members']} "
              f"ths_hot={r['ths_hot']} trade_date={r['trade_date']}", flush=True)

    if args.boardsynth or args.all:
        # 板块日线合成(流通市值加权全量覆盖) + 成员标签(corr60/龙头) + 板块解读
        r = synth_board_daily()
        print(f"[boardsynth] boards={r['boards']} rows={r['rows']}", flush=True)

    if args.boardmin or args.all:
        # 板块分时合成: 须在 minutes 之后(当日成分股分时已落库)
        # 单独 --boardmin = 全量重算最近30个有效分时日; --all 日常链路只补当日
        full = args.boardmin and not args.all and not args.boardmin_one
        r = synth_board_minutes(all_days=full)
        print(f"[boardmin] rows={r['rows']} days={r['days']} "
              f"trade_date={r['trade_date']}", flush=True)

    if args.darkpool or args.all:
        # 暗盘: 大宗交易+龙虎榜+席位(机构/拉萨游资), 报表接口低频采集
        r1 = sync_block_trade(days=args.days or 7)
        print(f"[darkpool] 大宗 rows={r1['rows']} skipped={r1['skipped']} "
              f"range={r1['range']}", flush=True)
        r2 = sync_billboard(days=args.days or 7)
        print(f"[darkpool] 龙虎榜 rows={r2['rows']} skipped={r2['skipped']} "
              f"seats={r2['seat_pairs']}", flush=True)

    if args.darkind or args.darkind_all or args.all:
        # 暗盘派生指标: 须在 darkpool 之后; --darkind-all = 全历史重算(回填1年后用)
        r = refresh_dark_indicator(full=args.darkind_all)
        print(f"[darkind] dates={r['dates']} rows={r['rows']} "
              f"signals={r['signals']} full={args.darkind_all}", flush=True)

    if args.ztpool or args.all:
        # 涨停池+强势股池(东财, akshare): 每日盘后 1 次; --days N 可回填最近N个交易日
        r = sync_zt_pool(days=args.days or 1)
        print(f"[ztpool] dates={r['dates']} 涨停={r['zt_rows']} "
              f"强势={r['strong_rows']} 剔除={r['missed']}", flush=True)

    if args.thsmoney or args.all:
        # 同花顺资金流快照(akshare): 进 --all, 与新浪 stock_money_flow 交叉验证
        r = sync_money_flow_ths()
        print(f"[thsmoney] date={r['trade_date']} rows={r['rows']} "
              f"剔除={r['missed']}", flush=True)

    if args.finreport or args.all:
        # 业绩报表(东财按报告期批量): 进 --all(仅最新报告期, ~6s); 首次回填 --quarters 8
        r = sync_fin_report(quarters=args.quarters)
        print(f"[finreport] 报告期={r['report_dates']} rows={r['rows']}", flush=True)

    if args.adjdaily or args.all:
        # 复权因子日常增量: 零请求预警 + 候选验证, 须在 quotes+thsmoney 之后
        r = refresh_adjust_events()
        print(f"[adjdaily] 候选={r['candidates']} 验证={r['checked']} "
              f"事件={r['events']} failed={r['failed']}", flush=True)

    if args.adjseed:
        # 复权因子一次性回填(腾讯qfq): 单线程长任务 ~20-25分钟, 建议后台; 断点续跑幂等
        r = seed_adjust_factors(limit=args.limit)
        print(f"[adjseed] 池={r['total']} ok={r['ok']} skip={r['skip']} "
              f"empty={r['empty']} failed={r['failed']} events={r['events']}", flush=True)

    if args.hotrank or args.all:
        # 东财人气榜Top100快照: 进 --all, 1 请求, 逐日累积(无历史)
        r = sync_hot_rank()
        print(f"[hotrank] date={r['trade_date']} rows={r['rows']} "
              f"剔除={r['missed']}", flush=True)

    if args.margin or args.all:
        # 两融明细(交易所官网): 进 --all(默认最新1日); 历史回填 --margin --days N
        r = sync_margin_detail(days=args.days or 1)
        print(f"[margin] dates={r['dates']} rows={r['rows']} "
              f"剔除={r['missed']}", flush=True)

    if args.sharechange or args.all:
        # 董监高持股变动(东财 datacenter): 进 --all(增量); 首次全量 --sharechange --full
        r = sync_share_change(full=args.full)
        print(f"[sharechange] mode={r['mode']} remote={r['remote']} rows={r['rows']}",
              flush=True)

    if args.swmember:
        # 申万行业成分(akshare): 慢变量全量刷新, 不进 --all
        r = sync_sw_industry_member()
        print(f"[swmember] boards={r['boards']} rows={r['rows']} "
              f"剔除={r['missed']}", flush=True)

    if args.valtopup:
        # 估值日度顶格: daily quote 快照列 -> stock_valuation(零API成本, 须在 valsnap 后)
        from app.collector.basic_collector import topup_valuation_from_quote
        r = topup_valuation_from_quote()
        print(f"[valtopup] date={r['trade_date']} rows={r['rows']}", flush=True)

    if args.valpctl:
        # 估值分位重算落库(近5年窗口, 全量pandas计算, 须在 valtopup 后)
        r = refresh_valuation_pctl()
        print(f"[valpctl] date={r['trade_date']} rows={r['rows']} "
              f"elapsed={r['elapsed']}s", flush=True)

    if args.valuation:
        # 估值历史(东财, akshare): 不进 --all(单股全历史接口, 全市场一轮约1小时);
        # 日常增量仅补新行; 首次 --valuation --full 全量回填建议后台任务, --shard/--nshards 分片
        r = sync_valuation(days=args.val_days, full=args.full, limit=args.limit,
                           shard=args.shard, n_shards=args.nshards)
        print(f"[valuation] 池={r['total']} ok={r['ok']} empty={r['empty']} "
              f"无新行={r['skip']} failed={r['failed']} rows={r['rows']}", flush=True)


if __name__ == "__main__":
    main()
