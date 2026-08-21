"""盘前简报编排(阶段5,9:15 前):新闻+异动+技术面 → 催化状态(LLM,降级规则) → 推送。

run_morning() 供早间定时任务调用:
1. 同花顺日历判交易日,非交易日跳过
2. 新浪 7x24 新闻 → 板块关键词过滤(隔夜+今晨)
3. 同花顺异动/热榜(最近交易日) + 库内日线信号/关键价位/拥挤度
4. LLM 判每板块催化状态(增强/减弱/新增/未变 + 依据 + 风险 + 情景)
   —— LLM 未配置/失败时降级规则:有新闻命中→"新增",否则按异动延续"未变"
5. 存 catalyst_status,组盘前简报(§6.2),飞书推送
"""
from __future__ import annotations

import json
import logging
from datetime import date

from .config import Config
from .db import CATALYST_STATUS_COLUMNS, connect, init_db, upsert_rows
from .fetchers import fuyao, news as news_mod
from .llm import chat
from .push import send

logger = logging.getLogger(__name__)

_STATUS_ORDER = {"新增": 0, "增强": 1, "未变": 2, "减弱": 3}


def _fetch_daily(conn, symbol: str):
    import pandas as pd

    return pd.read_sql_query(
        "SELECT trade_date, open, high, low, close, volume, turnover FROM etf_kline_daily "
        "WHERE symbol=? ORDER BY trade_date", conn, params=(symbol,),
    )


def _technical(symbol: str, conn) -> dict:
    """单只 ETF 的技术状态(昨收):结构/支撑/压力/信号。"""
    from .signals.analyze import analyze
    from .signals.signals import display_signal

    df = _fetch_daily(conn, symbol)
    if df.empty:
        return {}
    res = analyze(df)
    return {
        "structure": res["structure"]["state"]["state"],
        "support": res.get("support"),
        "resistance": res.get("resistance"),
        "signal": display_signal(res),
    }


def _build_context(cfg, conn) -> dict:
    """盘前催化上下文:新闻命中 + 板块异动/热榜 + 技术面 + 拥挤度。"""
    kws = cfg.get("news", "keywords", default={}) or {}
    items = news_mod.fetch_all_feeds(
        pages=cfg.get("news", "pages", default=1),
        page_size=cfg.get("news", "page_size", default=50),
    )
    news_hits = news_mod.filter_by_keywords(items, kws)
    cat = fuyao.catalyst_context(cfg)  # 最近交易日异动/热榜

    sectors = []
    for s in cat["sectors"]:
        sym = s["symbol"]
        tech = _technical(sym, conn)
        crowd = conn.execute(
            "SELECT ratio_pct, level FROM sector_turnover WHERE symbol=? "
            "ORDER BY trade_date DESC LIMIT 1", (sym,),
        ).fetchone()
        sectors.append({
            "symbol": sym,
            "name": s["name"],
            "sector": s["sector"],
            "news": news_hits.get(s["sector"], []),
            "anomalies": s["anomalies"],
            "hot": s["hot"],
            "tech": tech,
            "crowd": dict(crowd) if crowd else None,
        })
    return {"sectors": sectors, "market_anomalies": len(cat["anomalies"]), "market_hot": len(cat["hot"])}


def _yesterday_status(conn) -> dict[str, str]:
    """上一个盘前记录的催化状态:{symbol: status}。"""
    latest = conn.execute("SELECT MAX(trade_date) FROM catalyst_status").fetchone()[0]
    if not latest:
        return {}
    return {r["symbol"]: r["status"] for r in conn.execute(
        "SELECT symbol, status FROM catalyst_status WHERE trade_date=?", (latest,)).fetchall()}


def _rule_fallback(sectors: list[dict]) -> dict[str, dict]:
    """无 LLM 时的降级判定:检测"有没有新催化"。
    有新闻命中 → 新增;无新闻但有异动 → 未变(盘面延续);否则未变。
    """
    out = {}
    for s in sectors:
        if s["news"]:
            out[s["symbol"]] = {"status": "新增", "reason": f"盘前新闻 {len(s['news'])} 条命中"}
        elif s["anomalies"]:
            out[s["symbol"]] = {"status": "未变", "reason": f"昨日异动 {len(s['anomalies'])} 条,无新消息"}
        else:
            out[s["symbol"]] = {"status": "未变", "reason": "无新闻、无异动"}
    return out


def _llm_judge(sectors: list[dict], prev: dict[str, str], cfg) -> dict | None:
    """LLM 判催化状态 + 盘面综述/今日关注。

    返回 {"summary": str, "watch": [str], "verdict": {symbol: {status, reason, risk, scenario}}} 或 None。
    """
    lines = ["你是 A股中长线交易系统的盘前分析师。基于以下数据,对每个跟踪板块判定催化剂状态,并写盘面综述。"]
    lines.append("\n【昨日催化状态】")
    for s in sectors:
        lines.append(f"- {s['sector']}: {prev.get(s['symbol']) or '无记录'}")
    lines.append("\n【今日盘前催化上下文】")
    for s in sectors:
        lines.append(f"\n* {s['sector']}({s['name']})")
        if s["news"]:
            lines.append("  新闻:")
            for it in s["news"][:5]:
                lines.append(f"    - {it['time']} {it['text'][:70]}")
        if s["anomalies"]:
            lines.append(f"  昨日异动 {len(s['anomalies'])} 条:")
            for a in s["anomalies"][:5]:
                kw = "/".join(a.get("keyword_list") or [])
                lines.append(f"    - [{a.get('tag_name')}] {a.get('stock_name')} {kw}")
        t = s["tech"]
        if t:
            lines.append(f"  技术: {t.get('structure')}结构, 支撑{t.get('support')}/压力{t.get('resistance')}, 信号:{t.get('signal')}")
    lines.append("\n只输出 JSON:")
    lines.append('{"summary": "一句话盘面综述", "watch": ["今日关注1", "今日关注2"], '
                 '"sectors": {"板块名": {"status": "增强|减弱|新增|未变", "reason": "依据", "risk": "风险", "scenario": "条件式情景"}}}')
    prompt = "\n".join(lines)
    text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.2)
    if not text:
        return None
    try:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        name2sym = {s["sector"]: s["symbol"] for s in sectors}
        verdict = {}
        for sector, v in (data.get("sectors") or {}).items():
            sym = name2sym.get(sector)
            if sym and isinstance(v, dict):
                verdict[sym] = {"status": v.get("status", "未变"),
                                "reason": v.get("reason", ""),
                                "risk": v.get("risk", ""),
                                "scenario": v.get("scenario", "")}
        return {"summary": data.get("summary", ""),
                "watch": data.get("watch", []) or [],
                "verdict": verdict}
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 输出解析失败: %s", exc)
        return None


def _format_briefing(sectors: list[dict], verdict: dict[str, dict], cfg,
                     summary: str = "", watch: list[str] | None = None) -> str:
    """盘前简报 markdown(§6.2:催化状态+关键价位+风险+情景)。"""
    watch = watch or []
    today = date.today().isoformat()
    lines = [f"# AlphaPrism 盘前简报 {today}"]
    if summary:
        lines += ["", f"> **盘面综述**:{summary}"]
    lines += ["", "## 一、催化剂状态(较昨日)", "",
              "| 板块 | 状态 | 依据/风险 |", "|---|---|---|"]
    for s in sectors:
        v = verdict.get(s["symbol"], {})
        st = v.get("status", "未变")
        reason = v.get("reason", "")
        risk = f" ⚠️{v.get('risk')}" if v.get("risk") else ""
        lines.append(f"| {s['sector']} | **{st}** | {reason}{risk} |")
    lines += ["", "## 二、今日关键价位与信号", "",
              "| 板块 | 结构 | 支撑 | 压力 | 信号 |", "|---|---|---|---|---|"]
    for s in sectors:
        t = s["tech"]
        crowd = f"占比{round(s['crowd']['ratio_pct'], 1)}%" if s.get("crowd") and s["crowd"].get("ratio_pct") else "-"
        lines.append(f"| {s['sector']} | {t.get('structure', '-')} | {t.get('support', '-')} | "
                     f"{t.get('resistance', '-')} | {t.get('signal', '-')} ({crowd}) |")
    lines += ["", "## 三、大概率情景(条件表述)"]
    scenarios = [v.get("scenario") for v in verdict.values() if v.get("scenario")]
    lines += [f"- {sc}" for sc in scenarios] or ["- 各板块结构未到买卖点,观望为主"]
    if watch:
        lines += ["", "## 四、今日关注"]
        lines += [f"- {w}" for w in watch]
    lines.append("")
    lines.append("> 数据:新浪7x24新闻 + 同花顺异动/热榜 + 库内日线。催化判定由 LLM/规则引擎生成,辅助决策。")
    return "\n".join(lines)


def run_morning(cfg: Config | None = None, push: bool = True) -> str | None:
    """执行盘前简报:非交易日返回 None。返回简报文本(并推送)。"""
    cfg = cfg or Config()
    conn = connect()
    init_db(conn)
    try:
        if not fuyao.is_trading_day():
            logger.info("今日非交易日,跳过盘前简报")
            return None
        sectors = _build_context(cfg, conn)["sectors"]
        prev = _yesterday_status(conn)
        summary, watch = "", []
        judge = _llm_judge(sectors, prev, cfg)
        if judge:
            verdict = judge["verdict"]
            summary = judge.get("summary", "")
            watch = judge.get("watch", []) or []
        else:
            verdict = _rule_fallback(sectors)
        # 存催化状态
        today = date.today().isoformat()
        rows = [(today, sym, v.get("status", "未变"), v.get("reason", "")) for sym, v in verdict.items()]
        upsert_rows(conn, "catalyst_status", CATALYST_STATUS_COLUMNS, rows)
        conn.commit()
        briefing = _format_briefing(sectors, verdict, cfg, summary, watch)
        if push and cfg.get("push", "enabled", default=False):
            send("盘前简报", briefing, level="alert", cfg=cfg)
        logger.info("盘前简报完成(%d 板块)", len(sectors))
        return briefing
    finally:
        conn.close()
