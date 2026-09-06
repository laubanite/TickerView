"""AlphaPrism 命令行入口(finminutes 风格 CLI)。

用法(在 VS Code 终端,项目根目录已加入 PATH):
  alphaprism --help
  alphaprism init-db
  alphaprism daily [symbol ...]
  alphaprism report [YYYY-MM-DD]
  alphaprism status
  alphaprism signal [symbol ...]        # 无参数=全池,多只用空格分隔
  alphaprism backtest [symbol ...]
  alphaprism pool [list|add|remove|clear]
  alphaprism wind <server_type> <tool_name> '<params_json>'
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from alphaprism.backtest import backtest as run_backtest  # noqa: E402
from alphaprism.config import Config, PROJECT_ROOT, WATCHLIST_FILE  # noqa: E402
from alphaprism.db import DEFAULT_DB, connect, init_db  # noqa: E402
from alphaprism.report import build_daily_report, write_report  # noqa: E402
from alphaprism.runner import run_daily  # noqa: E402
from alphaprism.signals.analyze import analyze  # noqa: E402
from alphaprism.signals.signals import display_signal  # noqa: E402
from alphaprism.signals.structure import VOL_ZONE_WINDOW  # noqa: E402
from alphaprism.walkforward import by_year, summarize, walk_forward  # noqa: E402

from alphaprism.pipeline import refresh_sector_crowding  # noqa: E402

from alphaprism.planner.parser import parse_file as parse_battlemap  # noqa: E402
from alphaprism.backtest_map import run_map_backtest  # noqa: E402

WIND_SKILL_DIR = (
    Path(os.environ.get("USERPROFILE", Path.home())) / ".agents" / "skills" / "wind-mcp-skill"
)


# --------------------------------------------------------------------------- 工具

def _f(x, digits: int = 3) -> str:
    return f"{x:.{digits}f}" if isinstance(x, (int, float)) else "-"


def _load_daily(symbol: str) -> pd.DataFrame:
    conn = connect()
    try:
        return pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, volume, turnover FROM etf_kline_daily "
            "WHERE symbol=? ORDER BY trade_date",
            conn, params=(symbol,),
        )
    finally:
        conn.close()


def _resolve_symbols(args) -> list[str]:
    """无参数 → 全池;有参数 → 指定列表(去重保序)。"""
    if getattr(args, "symbol", None):
        return list(dict.fromkeys(args.symbol))
    return [str(w["symbol"]) for w in Config().watchlist]


def _pool_name(symbol: str) -> str:
    return next((w["name"] for w in Config().watchlist if w["symbol"] == symbol), "")


# --------------------------------------------------------------------------- 命令

def cmd_init_db(_args) -> int:
    conn = connect()
    init_db(conn)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    conn.close()
    print(f"数据库已初始化: {DEFAULT_DB}")
    print(f"表({len(tables)}): {', '.join(tables)}")
    return 0


def cmd_daily(args) -> int:
    summary, path = run_daily(symbols=_resolve_symbols(args))
    print("\n=== 抓取摘要 ===")
    for r in summary:
        status = "⚠️ " + "; ".join(r["errors"]) if r["errors"] else "ok"
        print(f"  {r['symbol']} {r['name']}: 日线{r['daily']} 周线{r['weekly']} 30分{r['m30']}  [{status}]")
    print(f"日报: {path}")
    return 0


def cmd_report(args) -> int:
    cfg = Config()
    conn = connect()
    try:
        latest = args.date or conn.execute("SELECT MAX(trade_date) FROM etf_kline_daily").fetchone()[0]
        if latest is None:
            print("数据库中无日线数据,请先运行: alphaprism daily")
            return 1
        md = build_daily_report(conn, cfg, None)
        path = write_report("reports", md, latest)
    finally:
        conn.close()
    print(f"日报已生成: {path}")
    return 0


def cmd_status(_args) -> int:
    cfg = Config()
    conn = connect()
    print("AlphaPrism 状态")
    print(f"数据库 : {DEFAULT_DB}")
    print("数据源 : 腾讯直连(K线)+ 新浪(当日补) | Wind(成交额/换手率全历史 + 行业/宏观 agent 通道) | "
          "同花顺(板块成交占比)")
    print("\n跟踪池:")
    print(f"  {'代码':<8}{'名称':<14}{'日线':>6}{'周线':>6}{'30分':>6}  最新交易日")
    for item in cfg.watchlist:
        sym = str(item["symbol"])
        d = conn.execute("SELECT count(*) n, max(trade_date) mx FROM etf_kline_daily WHERE symbol=?", (sym,)).fetchone()
        w = conn.execute("SELECT count(*) n FROM etf_kline_weekly WHERE symbol=?", (sym,)).fetchone()
        m = conn.execute("SELECT count(*) n FROM etf_kline_30m WHERE symbol=?", (sym,)).fetchone()
        print(f"  {sym:<8}{item.get('name', ''):<14}{d['n']:>6}{w['n']:>6}{m['n']:>6}  {d['mx'] or '-'}")
    print("\n最近抓取日志:")
    for r in conn.execute(
        "SELECT id, run_ts, source, scope, rows, status, error FROM fetch_log ORDER BY id DESC LIMIT 6"
    ):
        err = f" ({r['error'][:70]})" if r["error"] else ""
        print(f"  #{r['id']} [{r['run_ts']}] {r['source']} {r['scope']} → {r['rows'] or 0} 行 [{r['status']}]{err}")
    conn.close()
    wind_ok = (WIND_SKILL_DIR / "scripts" / "cli.mjs").exists()
    print(f"\nWind MCP: {'已安装(agent 通道可用)' if wind_ok else '未安装'}")
    return 0


def cmd_signal(args) -> int:
    symbols = _resolve_symbols(args)
    if len(symbols) == 1:
        return _signal_detail(symbols[0])
    print(f"{'代码':<7}{'名称':<14}{'结构':<6}{'支撑':>8}{'压力':>8}  信号")
    for sym in symbols:
        df = _load_daily(sym)
        if df.empty:
            print(f"{sym:<7}无数据(先 alphaprism daily)")
            continue
        res = analyze(df)
        st = res["structure"]["state"]["state"]
        print(f"{sym:<7}{_pool_name(sym)[:12]:<14}{st:<6}{_f(res.get('support')):>8}"
              f"{_f(res.get('resistance')):>8}  {display_signal(res)[:28]}")
    return 0


def _signal_detail(symbol: str) -> int:
    df = _load_daily(symbol)
    if df.empty:
        print(f"无 {symbol} 日线数据,请先运行: alphaprism daily {symbol}")
        return 1
    res = analyze(df)
    st = res["structure"]["state"]
    close = float(df["close"].iloc[-1])
    near = res.get("near", {})
    ma = near.get("ma", {})
    swing = near.get("swing", {})
    vz = near.get("volume_zone")
    print(f"== {symbol} {_pool_name(symbol)} 结构分析 (最新 {df['trade_date'].iloc[-1]}) ==")
    print(f"结构状态 : {st.get('state')}" + (f" ({st.get('detail')})" if st.get("detail") else ""))

    def _dist(x):
        return f"{(close - x) / x * 100:+.1f}%" if x else "-"

    ssup, sres = res.get("structure_support"), res.get("structure_resistance")
    print(f"结构位   : 支撑 {_f(ssup)} ({_dist(ssup)}) / 压力 {_f(sres)} ({_dist(sres)})  [大级别攻守/失效判据]")
    sw_sup, sw_res = swing.get("support"), swing.get("resistance")
    if sw_sup:
        print(f"近端支撑 : {sw_sup[0]:.3f} ({sw_sup[1]},{_dist(sw_sup[0])})")
    else:
        print("近端支撑 : 无(现价下方近期无已确认低点)")
    if sw_res:
        print(f"近端压力 : {sw_res[0]:.3f} ({sw_res[1]},{_dist(sw_res[0])})")
    else:
        print("近端压力 : 无(现价上方近期无已确认高点)")
    ma_s = "  ".join(f"MA{p}={v:.3f}" for p, v in sorted(ma.items()) if v)
    if ma_s:
        print(f"均线     : {ma_s}")
    if vz:
        print(f"密集区   : {vz['lower']:.3f}~{vz['upper']:.3f} (近{VOL_ZONE_WINDOW}日量价峰,近似)")
    gaps = near.get("gaps") or []
    if gaps:
        g_s = "  ".join(
            f"{'上跳' if g['type'] == 'up' else '下跳'}{g['low']:.3f}~{g['high']:.3f}({g['date']})"
            for g in gaps)
        print(f"缺口     : {g_s}")
    print(f"当前信号 : {res['setup']} | {res.get('note', '')}")
    vp = res.get("vp", {})
    flags = []
    for name, label in (("is_vol_up", "放量"), ("is_vol_down", "缩量"), ("sky_volume", "天量"),
                        ("floor_volume", "地量"), ("volume_stall", "量增价不涨"),
                        ("high_turnover", "高换手"), ("macd_top_div", "MACD顶背离"),
                        ("top_divergence", "缩量新高"), ("bottom_divergence", "底背离"),
                        ("wave_decline", "逐波递减"), ("double_test", "二踩确认"),
                        ("coil", "收敛预备"), ("round_bottom", "圆弧底"), ("round_top", "圆弧顶"),
                        ("range_disorder", "量价震荡"), ("irregular_volume", "不规则放量"),
                        ("m_head_break", "M头破位")):
        if vp.get(name):
            flags.append(label)
    print(f"量价     : 量比 {_f(vp.get('vol_ratio'), 2)}  换手{vp.get('turnover_tier', '-')}  "
          f"反包={'是' if res.get('engulf') else '否'}  {'/'.join(flags)}")
    print(f"结构统计 : 合并{res['merged_count']}根 分型{res['fractal_count']}个 笔{res['stroke_count']}个")
    print()
    print(res["text"])
    return 0


def cmd_walkforward(args) -> int:
    symbols = _resolve_symbols(args)
    print(f"{'代码':<7}{'名称':<14}{'窗口数':>5}{'均胜率':>7}{'胜率区间':>12}{'正收益窗':>8}{'均收益':>8}")
    for sym in symbols:
        df = _load_daily(sym)
        if len(df) < 100:
            print(f"{sym:<7}样本不足(<100根)")
            continue
        df["symbol"] = sym
        rows = walk_forward(df, window=args.window, step=args.step)
        s = summarize(rows)
        if not rows:
            continue
        name = _pool_name(sym)[:12]
        wr_range = f"{s['min_win_rate'] * 100:.0f}~{s['max_win_rate'] * 100:.0f}%"
        print(f"{sym:<7}{name:<14}{s['windows']:>5}{s['mean_win_rate'] * 100:>6.0f}%"
              f"{wr_range:>12}{s['positive_windows']:>8}{s['mean_return_pct']:>7.1f}%")
        if args.detail:
            for r in rows:
                plr = r["pl_ratio"] if r["pl_ratio"] is not None else "-"
                print(f"    {r['start']} ~ {r['end']}: {r['trades']}笔 胜率{r['win_rate']*100:.0f}% "
                      f"盈亏比{plr} 收益{r['return_pct']:+.1f}%")
    return 0


def cmd_backtest(args) -> int:
    symbols = _resolve_symbols(args)
    if len(symbols) == 1:
        return _backtest_detail(symbols[0])
    print(f"{'代码':<7}{'名称':<14}{'交易':>4}{'年均':>5}{'胜率':>7}{'盈亏比':>7}{'累计':>9}{'持仓天':>6}")
    for sym in symbols:
        df = _load_daily(sym)
        if len(df) < 60:
            print(f"{sym:<7}样本不足(<60根)")
            continue
        df["symbol"] = sym
        r = run_backtest(df)
        plr = r["pl_ratio"] if r["pl_ratio"] is not None else "-"
        print(f"{sym:<7}{_pool_name(sym)[:12]:<14}{r['trade_count']:>4}{r['signal_freq_per_year']:>5}"
              f"{r['win_rate'] * 100:>6.0f}%{str(plr):>7}{r['total_return_pct']:>8.1f}%{r['avg_hold_days']:>6.1f}")
    return 0


def _backtest_detail(symbol: str) -> int:
    df = _load_daily(symbol)
    if df.empty:
        print(f"无 {symbol} 日线数据,请先运行: alphaprism daily {symbol}")
        return 1
    df["symbol"] = symbol
    r = run_backtest(df)
    print(f"== {symbol} {_pool_name(symbol)} 回测 ({r['period']}) ==")
    print(f"笔 {r['strokes']} | 买点setup {r['setup_buy_count']} | 确认 {r['confirmed_buy_count']} "
          f"| 交易 {r['trade_count']} 次 (年均 {r['signal_freq_per_year']})")
    plr = r["pl_ratio"] if r["pl_ratio"] is not None else "-"
    print(f"胜率 {r['win_rate'] * 100:.0f}% | 均盈 {r['avg_win']}% | 均亏 {r['avg_loss']}% "
          f"| 盈亏比 {plr} | 累计 {r['total_return_pct']}% | 平均持仓 {r['avg_hold_days']} 天")
    print()
    print("交易明细:")
    for t in r["trades"]:
        print(f"  {t['entry_date']} → {t['exit_date']}  {t['pnl_pct']:+.2f}%  ({t['reason']}, {t['days']}天)")
    return 0


def cmd_sector(_args) -> int:
    """板块成交占比(同花顺):实时抓取 → 入库 → 展示最新交易日。"""
    cfg = Config()
    conn = connect()
    try:
        init_db(conn)  # 确保 sector_turnover 表存在(幂等)
        refresh_sector_crowding(conn, cfg, None)
        conn.commit()  # refresh 内部不提交,独立命令需自行提交
        latest = conn.execute("SELECT MAX(trade_date) FROM sector_turnover").fetchone()[0]
        if latest is None:
            print("无板块成交占比数据(检查 settings.local.yaml fuyao.api_key)")
            return 1
        rows = conn.execute(
            "SELECT symbol, sector, sector_amount, market_amount, ratio_pct, level "
            "FROM sector_turnover WHERE trade_date=? ORDER BY ratio_pct DESC",
            (latest,),
        ).fetchall()
        market = rows[0]["market_amount"] if rows else None
        print(f"板块成交占比(同花顺) {latest}  全市场成交额={_fmt_amt(market)}")
        print(f"{'代码':<7}{'名称':<14}{'板块':<8}{'板块额':>9}{'占比':>7}  拥挤度")
        for r in rows:
            flag = "⚠️ 警戒" if r["level"] == "警戒" else ("🔴 强警示" if r["level"] == "强警示" else "正常")
            print(f"{r['symbol']:<7}{_pool_name(r['symbol'])[:12]:<14}{r['sector']:<8}"
                  f"{_fmt_amt(r['sector_amount']):>9}{_f(r['ratio_pct'], 1):>6}%  {flag}")
    finally:
        conn.close()
    return 0


def cmd_catalyst(args) -> int:
    """催化剂上下文(同花顺异动+热榜):抓取 → 入库 → 按板块归属展示。

    输出即 LLM 解读催化剂的确定性输入(催化剂状态:增强/减弱/新增/未变)。
    """
    from alphaprism.pipeline import refresh_catalyst

    cfg = Config()
    conn = connect()
    try:
        init_db(conn)
        ctx = refresh_catalyst(conn, cfg, args.symbol or None)
        conn.commit()  # refresh 内部不提交
        if ctx is None:
            print("催化剂数据抓取失败(检查 settings.local.yaml fuyao.api_key / 网络)")
            return 1
        latest = conn.execute("SELECT MAX(trade_date) FROM catalyst_anomaly").fetchone()[0]
        print(f"催化剂上下文(同花顺) {latest}  |  全市场异动 {len(ctx['anomalies'])} 条 / 热榜 {len(ctx['hot'])} 条")
        for s in ctx["sectors"]:
            print(f"\n== {s['symbol']} {s['name']}({s['sector']}) ==")
            if s["anomalies"]:
                for a in s["anomalies"]:
                    kw = "/".join(a.get("keyword_list") or [])
                    print(f"  异动[{a.get('tag_name')}] {a.get('stock_name')} {kw} | "
                          f"{(a.get('analysis_content') or '')[:60]}")
            else:
                print("  异动: 无")
            if s["hot"]:
                for h in s["hot"]:
                    print(f"  热榜#{h.get('rank')} {h.get('name')}")
            else:
                print("  热榜: 无")
        top5 = ", ".join(f"{h.get('name')}(#{h.get('rank')})" for h in ctx["hot"][:5])
        print(f"\n全市场热榜 Top5: {top5}")
    finally:
        conn.close()
    return 0


def cmd_log(args) -> int:
    """信号日志(§6.4):list 查看 / result 记录结果 / backfill 重建。"""
    from alphaprism.pipeline import refresh_signal_log

    cfg = Config()
    conn = connect()
    try:
        init_db(conn)
        if args.action == "result":
            if not args.log_id or not args.outcome:
                print("用法: alphaprism log result <信号ID> <止盈/止损/持有/放弃/回避/踏空/其他> [备注]")
                return 1
            note = " ".join(args.note or [])
            conn.execute("UPDATE signal_log SET result=? WHERE id=?", (args.outcome, args.log_id))
            conn.commit()
            print(f"已记录信号 #{args.log_id} 结果: {args.outcome}" + (f"({note})" if note else ""))
            return 0
        if args.action == "backfill":
            conn.execute("DELETE FROM signal_log")
            conn.commit()
            events = refresh_signal_log(conn, cfg, None)
            print(f"信号日志已重建: {len(events)} 条事件")
            return 0
        rows = conn.execute(
            "SELECT id, trade_date, symbol, kind, signal, state, price, note, result "
            "FROM signal_log ORDER BY id DESC LIMIT ?", (args.limit,),
        ).fetchall()
        if not rows:
            print("信号日志为空(运行 alphaprism daily 自动记录,或 alphaprism log backfill 回填历史)")
            return 0
        print(f"{'#':>4} {'日期':<11}{'代码':<7}{'类型':<6}{'信号':<5}{'状态':<5}{'价格':>8}  {'结果':<4} | 备注")
        for r in rows:
            print(f"{r['id']:>4} {r['trade_date']:<11}{r['symbol']:<7}{r['kind']:<6}{r['signal']:<5}"
                  f"{r['state']:<5}{_f(r['price']):>8}  {(r['result'] or '-'):<4} | {(r['note'] or '')[:32]}")
    finally:
        conn.close()
    return 0


def cmd_push(args) -> int:
    """推送测试:push 未启用时 dry-run 打印消息;启用则真实发送。"""
    from alphaprism.push import send

    cfg = Config()
    title, body = "AlphaPrism 推送测试(阶段5)", args.message or "这是一条测试消息"
    r = send(title, body, level="alert", cfg=cfg)
    if r.get("ok"):
        print("推送成功 ✅")
        return 0
    enabled = cfg.get("push", "enabled", default=False)
    print(f"[{'dry-run: push 未启用' if not enabled else '发送失败'}] {r.get('error')}")
    print("\n--- 将推送的内容 ---")
    print(f"{title}\n{body}")
    return 0


def _fmt_amt(x) -> str:
    if x is None:
        return "-"
    x = float(x)
    return f"{x / 1e8:.0f}亿" if x >= 1e8 else f"{x / 1e4:.0f}万"


def cmd_morning(args) -> int:
    """盘前简报(阶段5):新闻+异动 → 催化状态(LLM/规则) → 生成+推送。"""
    from alphaprism.morning import run_morning

    brief = run_morning(push=not args.dry_run)
    if not brief:
        print("盘前简报未生成(非交易日或数据缺失)")
        return 1
    print(brief)
    return 0


def cmd_monitor(_args) -> int:
    """盘中监测(阶段5):跑一轮触发判定,命中推送。非交易时段直接提示。"""
    from alphaprism.monitor import run_monitor

    triggered = run_monitor(push=True)
    if triggered is None:
        return 1
    if not triggered:
        print("本轮无触发(非交易时段/全池观望)")
    else:
        for t in triggered:
            print(f"  {t['symbol']}: {t['trigger']}")
    return 0


def cmd_valuation(_args) -> int:
    """板块估值体检(同花顺):行业成分股中位数 PE/PB vs 沪深300 基准 → 入库 → 展示。"""
    from alphaprism.fetchers.fuyao import VALUATION_COLUMNS, sector_valuation as compute_valuation
    from alphaprism.db import upsert_rows

    cfg = Config()
    conn = connect()
    try:
        init_db(conn)
        rows = compute_valuation(cfg)
        if not rows:
            print("无估值数据(检查 settings.yaml fuyao.sector_map)")
            return 1
        trade_date = conn.execute("SELECT MAX(trade_date) FROM etf_kline_daily").fetchone()[0]
        db_rows = [
            (trade_date, r["symbol"], r["sector"], r["n_stocks"], r["n_pe"], r["n_pb"],
             r["med_pe_ttm"], r["med_pb_mrq"], r["mkt_med_pe"], r["mkt_med_pb"],
             r["rel_pb"], r["reading"])
            for r in rows
        ]
        upsert_rows(conn, "sector_valuation", VALUATION_COLUMNS, db_rows)
        conn.commit()
        bench = rows[0]
        print(f"板块估值体检(同花顺) {trade_date}  |  沪深300 基准: PE {_f(bench['mkt_med_pe'], 1)} / PB {_f(bench['mkt_med_pb'], 2)}")
        print(f"{'代码':<7}{'名称':<12}{'板块':<7}{'股票':>4}{'中位PE':>8}{'中位PB':>7}{'相对PB':>7}  估值读数")
        for r in rows:
            pe_note = f"{_f(r['med_pe_ttm'], 1)}({r['n_pe']}家)" if r["med_pe_ttm"] else "-"
            pb_note = f"{_f(r['med_pb_mrq'], 2)}({r['n_pb']}家)" if r["med_pb_mrq"] else "-"
            rel = f"{r['rel_pb']:.2f}" if r["rel_pb"] is not None else "-"
            print(f"{r['symbol']:<7}{_pool_name(r['symbol'])[:11]:<12}{r['sector']:<7}"
                  f"{r['n_stocks']:>4}{pe_note:>8}{pb_note:>7}{rel:>7}  {r['reading']}")
    finally:
        conn.close()
    return 0


def cmd_battlemap(args) -> int:
    """作战地图 → RuleModel 解析(里程碑1,§4.1)。默认摘要;--json 输出完整模型。"""
    import json as _json

    path = args.path
    if not path:
        # 默认:config 里配的作战地图路径,或 AITrader 目录下最新一份
        from alphaprism.config import Config as _Cfg

        cfg = _Cfg()
        path = cfg.get("battlemap", "path", default="")
        if not path:
            import glob

            candidates = sorted(glob.glob(r"E:\AITrader\七只ETF作战地图_*.md"), reverse=True)
            if not candidates:
                print("未指定作战地图路径,且未找到默认地图。用法: alphaprism battlemap <path>")
                return 1
            path = candidates[0]
    model = parse_battlemap(path)
    if args.json:
        print(model.to_json())
        return 0
    s = model.summary()
    print(f"作战地图解析: {path}")
    print(f"  文档日期 : {s['doc_date'] or '-'} | schema v{s['schema_version']}")
    print(f"  大盘     : 收盘 {s['market_gate']['close']} | {s['market_gate']['conclusion'][:50]}")
    print(f"  标的     : {len(s['instruments'])} 只")
    for i in s["instruments"]:
        print(f"    {i['code']} {i['name']:<14} 价位{len(i['levels'])} 规则{len(i['rules'])} 预警{len(i['alerts'])}")
    print(f"  预算分配 : {len(s['budget_items'])} 项: {'、'.join(s['budget_items'])}")
    print(f"  剧本修正 : {len(s['playbook_rows'])} 行")
    print(f"  盯盘记录 : {s['journal_entries']} 条")
    print(f"  待确认   : {s['unresolved']} 条 | 告警 {s['warnings']} 条")
    if model.unresolved:
        print("\n  ⚠️ 待人工确认(解析器不猜测):")
        for u in model.unresolved:
            print(f"    - {u}")
    if model.warnings:
        print("\n  ⚠️ 告警:")
        for w in model.warnings:
            print(f"    - {w[:80]}")
    return 0


def cmd_backtest_map(args) -> int:
    """作战地图回测:按 RuleModel 整套可计算规则跑历史区间(里程碑2,§4.1)。"""
    path = args.path or _default_battlemap_path()
    model = parse_battlemap(path)
    r = run_map_backtest(model, args.start, args.end, args.capital)
    if "error" in r:
        print(f"回测失败: {r['error']}")
        return 1
    print(f"作战地图回测: {path}")
    print(f"区间      : {r['period']}  ({r['trading_days']} 交易日)")
    print(f"参与标的  : {', '.join(r['instruments'])}")
    print(f"总收益率  : {r['total_return_pct']:+.2f}%   (年化 {r['annualized_pct']:+.2f}%)")
    print(f"最大回撤  : {r['max_drawdown_pct']:.2f}%")
    print(f"买入持有  : {r['benchmark_return_pct']:+.2f}%   (等权基准)")
    print(f"超额 alpha: {r['alpha_pct']:+.2f}%")
    print(f"交易次数  : {r['trade_count']}")
    print(f"说明      : {r['note']}")
    if r["trades"]:
        print("\n交易明细:")
        print(f"  {'日期':<11}{'动作':<4}{'价格':>8}{'股数':>7}  触发依据")
        for t in r["trades"]:
            print(f"  {t['date']:<11}{t['action']:<4}{t['price']:>8.3f}{t['shares']:>7}  {t['reason']}")
    return 0


def _default_battlemap_path() -> str:
    """默认作战地图路径:config 配置或 AITrader 最新一份。"""
    from alphaprism.config import Config as _Cfg

    cfg = _Cfg()
    path = cfg.get("battlemap", "path", default="")
    if path:
        return path
    import glob

    candidates = sorted(glob.glob(r"E:\AITrader\七只ETF作战地图_*.md"), reverse=True)
    return candidates[0] if candidates else ""


def cmd_panel(_args) -> int:
    """TickerView 托盘宿主:自动拉起 Web 服务 + pywebview 悬浮面板 + 系统托盘常驻。"""
    from alphaprism.planner.floatpanel import run_panel

    run_panel()
    return 0


def cmd_web(_args) -> int:
    """启动 Web 行情页(里程碑6):Flask + Vue3 + ECharts,http://127.0.0.1:8765。"""
    from alphaprism.web.app import main as web_main

    web_main()
    return 0


def cmd_close(args) -> int:
    """盘后生成(里程碑4):收盘对账 + 复盘五问 + 纪律评分 → 追加写入作战地图。"""
    path = args.path or _default_battlemap_path()
    if not path:
        print("未找到作战地图。用法: alphaprism close [path]")
        return 1
    model = parse_battlemap(path)
    from alphaprism.planner.close import append_to_journal, build_close_review

    md = build_close_review(model)
    print(md)
    if args.write:
        append_to_journal(md, path)
        print(f"\n✅ 已追加写入: {path}")
    else:
        print("\n(未写入;加 --write 才写入作战地图)")
    return 0


def cmd_counterfactual(args) -> int:
    """反事实推演(盘后·情景分支,2026-08-26):收盘快照 + 确定性迁移表 → LLM 情景推演。

    建议收盘后手动运行;LLM 失败降级为纯迁移表(零幻觉)。输出非投资建议。
    """
    from alphaprism.planner.intraday import build_counterfactual

    for code in args.codes:
        md, ok = build_counterfactual(str(code).split(".")[0], scenario=args.scenario)
        print(f"===== {code} (LLM 情景推演 ok={ok}) =====")
        print(md)
        print()
    return 0


def cmd_playbook(args) -> int:
    """盘前生成(里程碑2):剧本草稿 → 人工确认 → 追加写入作战地图「每日盯盘记录」。"""
    path = args.path or _default_battlemap_path()
    if not path:
        print("未找到作战地图。用法: alphaprism playbook [path]")
        return 1
    model = parse_battlemap(path)
    from alphaprism.planner.playbook import append_to_journal as pb_append
    from alphaprism.planner.playbook import build_draft, format_draft

    draft = build_draft(model)
    md = format_draft(draft, model)
    print(md)
    if args.write:
        pb_append(md, path)
        print(f"\n✅ 已追加写入(盘前草稿): {path}")
    else:
        print("\n(草稿未写入;确认无误后加 --write 才写入作战地图)")
    return 0


def cmd_check(args) -> int:
    """盘中核对(里程碑3):RuleModel × 实时行情 → 每只标的结论词(5档)+ 大盘门控。"""
    path = args.path or _default_battlemap_path()
    if not path:
        print("未找到作战地图。用法: alphaprism check [path]")
        return 1
    model = parse_battlemap(path)
    from alphaprism.planner.live import check_live

    r = check_live(model)
    g = r["gate"]
    gate_state = "开放(可加仓)" if g["open"] else "关闭(不加仓)"
    j = f"{g['j']}" if g["j"] is not None else "-"
    j_date = g.get("date") or ""
    print(f"大盘门控: {gate_state}  J={j}(截至 {j_date} 收盘)  规则[{g['rule']}]")
    print(f"  命中动作: {g['action'] or '-'}")
    if not g["open"]:
        print("  ⚠️ 大盘破位/超卖 → 所有'接近买点'降级'等待·缺条件'(§5.1 大盘灯联动)")
    print()
    print(f"{'代码':<8}{'名称':<14}{'现价':>8}{'涨跌':>8}{'量能':>5}  结论词")
    for v in r["verdicts"]:
        p = f"{v['price']:.3f}" if v["price"] is not None else "-"
        ch = f"{v['change_pct']:+.2f}%" if v["change_pct"] is not None else "-"
        near = f"({v['near']})" if v["near"] else ""
        print(f"{v['code']:<8}{v['name'][:12]:<14}{p:>8}{ch:>8}{v['vol_label']:>5}  "
              f"{v['conclusion']} {near}")
    if args.json:
        import json as _json

        print(_json.dumps(r, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- 跟踪池管理

def _load_pool() -> list[dict]:
    if WATCHLIST_FILE.exists():
        return list(yaml.safe_load(WATCHLIST_FILE.read_text(encoding="utf-8")) or [])
    return []


def _save_pool(items: list[dict]) -> None:
    WATCHLIST_FILE.write_text(
        yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def cmd_pool(args) -> int:
    items = _load_pool()
    if args.action == "add":
        if not args.symbol:
            print("用法: alphaprism pool add <代码> [名称] [分类]")
            return 1
        sym = args.symbol.zfill(6) if args.symbol.isdigit() else args.symbol
        if any(i["symbol"] == sym for i in items):
            print(f"{sym} 已在池中")
        else:
            from alphaprism.fetchers.etf_kline import fetch_name

            name = args.name or fetch_name(sym) or f"ETF{sym}"
            category = args.category or "行业"
            items.append({"symbol": sym, "name": name, "category": category})
            _save_pool(items)
            print(f"已添加 {sym} {name} ({category});运行 alphaprism daily 拉数据")
    elif args.action == "remove":
        if not args.symbol:
            print("用法: alphaprism pool remove <代码>")
            return 1
        sym = args.symbol
        before = len(items)
        items = [i for i in items if i["symbol"] != sym]
        _save_pool(items)
        print(f"已移除 {sym}" if len(items) < before else f"{sym} 不在池中")
    elif args.action == "clear":
        _save_pool([])
        print("已清空跟踪池")

    print(f"\n当前跟踪池 ({len(items)} 只):")
    for i in items:
        print(f"  {i['symbol']}  {i['name']}  ({i.get('category', '')})")
    if not items:
        print("  (空)")
    return 0


# --------------------------------------------------------------------------- Wind

def cmd_wind(args) -> int:
    if not (WIND_SKILL_DIR / "scripts" / "cli.mjs").exists():
        print(f"wind-mcp-skill 未安装: {WIND_SKILL_DIR}")
        return 1

    # 参数形式:key=value 对(推荐,避免 shell 引号问题)或单个 JSON 字符串
    if len(args.params) == 1 and args.params[0].lstrip().startswith("{"):
        params_json = args.params[0]
    else:
        params: dict[str, str] = {}
        for kv in args.params:
            if "=" in kv:
                key, _, value = kv.partition("=")
                params[key] = value
        params_json = json.dumps(params, ensure_ascii=False)

    cmd = ["node", "scripts/cli.mjs", "call", args.server, args.tool, params_json]
    print(f"$ cd {WIND_SKILL_DIR} && node scripts/cli.mjs call {args.server} {args.tool} {params_json}")
    return subprocess.call(cmd, cwd=str(WIND_SKILL_DIR))


# --------------------------------------------------------------------------- 入口

def main() -> int:
    # Windows GBK 控制台无法编码 emoji(⚠️/📈 等),统一重配 stdout/stderr 为 UTF-8 并容错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="tickerview",
        description="TickerView — A股中长线 AI 辅助交易系统(决策辅助,不自动交易)。",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("init-db", help="初始化数据库(建表)")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("daily", help="抓取当日数据并生成日报")
    p.add_argument("symbol", nargs="*", help="可选:只抓指定 ETF 代码(默认全池)")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("report", help="仅读库生成日报(不抓取)")
    p.add_argument("date", nargs="?", help="可选:YYYY-MM-DD,默认最新")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("status", help="数据状态概览(健康检查)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pool", help="查看/修改跟踪池(config/watchlist.yaml)")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove", "clear"], default="list")
    p.add_argument("symbol", nargs="?", help="ETF 代码(add/remove)")
    p.add_argument("name", nargs="?", help="名称(可选,add 时自动获取)")
    p.add_argument("category", nargs="?", help="分类(可选,默认行业)")
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser("signal", help="结构分析 + 当前信号(多只用空格分隔,默认全池)")
    p.add_argument("symbol", nargs="*", help="ETF 代码,如 510300 515050")
    p.set_defaults(func=cmd_signal)

    p = sub.add_parser("backtest", help="回测(频率/胜率/盈亏比;多只用空格分隔,默认全池)")
    p.add_argument("symbol", nargs="*", help="ETF 代码,如 510300 515050")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("sector", help="板块成交占比(同花顺,实时抓取+入库+展示)")
    p.set_defaults(func=cmd_sector)

    p = sub.add_parser("valuation", help="板块估值体检(同花顺,成分股中位PE/PB vs 沪深300)")
    p.set_defaults(func=cmd_valuation)

    p = sub.add_parser("catalyst", help="催化剂上下文(同花顺异动+热榜,按板块归属;LLM 解读用)")
    p.add_argument("symbol", nargs="*", help="可选:只查指定 ETF")
    p.set_defaults(func=cmd_catalyst)

    p = sub.add_parser("log", help="信号日志(§6.4):list/result/backfill")
    p.add_argument("action", nargs="?", choices=["list", "result", "backfill"], default="list")
    p.add_argument("log_id", nargs="?", help="result 时:信号 ID")
    p.add_argument("outcome", nargs="?", help="result 时:止盈/止损/持有/放弃/回避/踏空/其他")
    p.add_argument("note", nargs="*", help="result 时:备注")
    p.add_argument("--limit", type=int, default=40, help="list 显示条数")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("push", help="推送测试(阶段5;push 未启用时 dry-run 打印)")
    p.add_argument("message", nargs="?", help="可选:测试消息内容")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("morning", help="盘前简报(阶段5;新闻+LLM催化状态→推送)")
    p.add_argument("--dry-run", action="store_true", help="只生成不推送")
    p.set_defaults(func=cmd_morning)

    p = sub.add_parser("monitor", help="盘中监测跑一轮(阶段5;非交易时段跳过)")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("walkforward", help="滚动窗口回测(信号稳定性;默认全池)")
    p.add_argument("symbol", nargs="*", help="ETF 代码(默认全池)")
    p.add_argument("--window", type=int, default=250, help="窗口交易日数(默认250≈1年)")
    p.add_argument("--step", type=int, default=42, help="滑动步长交易日(默认42≈2月)")
    p.add_argument("--detail", action="store_true", help="打印每个窗口明细")
    p.set_defaults(func=cmd_walkforward)

    p = sub.add_parser("battlemap", help="作战地图 → RuleModel 解析(里程碑1)")
    p.add_argument("path", nargs="?", help="作战地图 .md 路径(默认 config 或 AITrader 最新)")
    p.add_argument("--json", action="store_true", help="输出完整 RuleModel JSON")
    p.set_defaults(func=cmd_battlemap)

    p = sub.add_parser("web", help="启动 Web 行情页(里程碑6):http://127.0.0.1:8765")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("panel", help="悬浮面板(里程碑7):pywebview 置顶小窗(状态灯+结论词)")
    p.set_defaults(func=cmd_panel)

    p = sub.add_parser("check", help="盘中核对(里程碑3):RuleModel×实时行情→结论词5档+大盘门控")
    p.add_argument("path", nargs="?", help="作战地图 .md 路径(默认 config 或 AITrader 最新)")
    p.add_argument("--json", action="store_true", help="输出完整核对 JSON")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("close", help="盘后生成(里程碑4):收盘对账+复盘五问+纪律评分")
    p.add_argument("path", nargs="?", help="作战地图 .md 路径(默认 config 或 AITrader 最新)")
    p.add_argument("--write", action="store_true", help="追加写入作战地图每日盯盘记录(默认只打印)")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("counterfactual", help="反事实推演(盘后·情景分支):收盘快照+迁移表→LLM 情景推演")
    p.add_argument("codes", nargs="+", help="标的代码(如 515790)")
    p.add_argument("--scenario", default=None, help="情景描述(如 '大盘明日跌破 3850')")
    p.set_defaults(func=cmd_counterfactual)

    p = sub.add_parser("playbook", help="盘前生成(里程碑2):剧本草稿→确认→追加写入作战地图")
    p.add_argument("path", nargs="?", help="作战地图 .md 路径(默认 config 或 AITrader 最新)")
    p.add_argument("--write", action="store_true", help="写入草稿到每日盯盘记录(默认只打印)")
    p.set_defaults(func=cmd_playbook)

    p = sub.add_parser("backtest-map", help="作战地图回测(里程碑2):按整套可计算规则跑历史区间")
    p.add_argument("path", nargs="?", help="作战地图 .md 路径(默认 config 或 AITrader 最新)")
    p.add_argument("--start", default="2025-08-01", help="开仓时点(默认 2025-08-01)")
    p.add_argument("--end", default="2026-08-01", help="回测区间终点(默认 2026-08-01)")
    p.add_argument("--capital", type=float, default=17300, help="初始本金(默认 17300)")
    p.set_defaults(func=cmd_backtest_map)

    p = sub.add_parser("wind", help="调用 Wind MCP 取数")
    p.add_argument("server", help="server_type,如 stock_data / fund_data / index_data / economic_data")
    p.add_argument("tool", help="tool_name,见 references/tool-contracts.md")
    p.add_argument(
        "params", nargs="+",
        help="key=value 参数对(推荐,如 windcode=600519.SH indexes=最新成交价);或单个 JSON 字符串",
    )
    p.set_defaults(func=cmd_wind)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
