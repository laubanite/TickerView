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
from .db import CATALYST_STATUS_COLUMNS, connect, init_db, log_fetch, upsert_rows
from .fetchers import fuyao, news as news_mod
from .llm import chat
from .morning_verify import prior_prompt
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
    """盘前催化上下文:新闻命中 + 板块异动/热榜 + 技术面 + 拥挤度。

    附带 news_health(抓取健康审计)与 feed(原始合并快讯),供盘前视图复用避免二次抓取。
    每次抓取写 fetch_log:源可达性/条数/错误,失败不再静默(方案 M1)。
    """
    kws = cfg.get("news", "keywords", default={}) or {}
    feed = news_mod.fetch_feeds(
        pages=cfg.get("news", "pages", default=1),
        page_size=cfg.get("news", "page_size", default=50),
    )
    for s in feed["sources"]:
        log_fetch(conn, source="news:" + s.source, scope="morning",
                  start=feed.get("latest_ts") or None, end=feed["fetched_at"],
                  rows=s.rows, status="ok" if s.ok else "error", error=s.error)
    conn.commit()  # fetch_log 落库(connect 默认非自动提交)
    items = feed["items"]
    news_hits = news_mod.filter_by_keywords(items, kws)
    try:
        cat = fuyao.catalyst_context(cfg)  # 最近交易日异动/热榜
    except Exception as exc:  # noqa: BLE001  无同花顺key/网络失败→板块异动降级为空,盘前仍出"消息+状态灯"
        logger.warning("催化剂上下文获取失败(板块异动降级为空,不影响盘前): %s", exc)
        cat = {"sectors": [], "anomalies": [], "hot": []}

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
    return {
        "sectors": sectors,
        "market_anomalies": len(cat["anomalies"]),
        "market_hot": len(cat["hot"]),
        "news_health": {
            "fetched_at": feed["fetched_at"],
            "stale": feed["stale"],
            "latest_ts": feed["latest_ts"],
            "all_failed": feed["all_failed"],
            "sources": [{"source": s.source, "ok": s.ok, "rows": s.rows, "error": s.error}
                        for s in feed["sources"]],
        },
        "feed": items,
    }


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


# ---------------------------------------------------------------- 隔夜重要消息(方案 M2/M3)

_IMPACT_ORDER = {"利空": 0, "利好": 1, "中性": 2}
_TYPES = {"宏观政策", "行业涨价", "龙头业绩", "海外市场", "突发事件", "其他"}
_GRADES = {"官方", "媒体", "快讯", "传闻"}


def _snap_sentence(s: str, n: int) -> str:
    """句子边界截断(反截断规则,docs/盘前方案.md §七):截断只允许落在句读
    (。！？；.!?;)之后补省略号,不产生"话说一半"的半句残片。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    idx = max(cut.rfind(c) for c in "。！？；.!?;")
    return (cut[: idx + 1] if idx >= 0 else cut).rstrip("；;") + "…"


def _llm_important_news(feed: list[dict], sector_hits: dict[str, list[dict]],
                        sector_names: list[str], cfg, prior: str = "",
                        sector_perf: dict | None = None) -> dict:
    """LLM 从新浪+东财快讯里挑重要新闻,分类 利空/利好/中性 并关联板块。失败返回 {}。

    输出每条带 影响/类型/来源分级/置信度(方案 M2),供前端渲染、简报逐条(M3)与 M4 验证。
    prior(方案 M5):历史应验率先验(由 morning_verify.prior_prompt 生成),让挑选带实测权重。
    sector_perf(方向误判修复):跟踪板块最近交易日涨跌 {板块: "+2.3%"},供因果链交叉验证。
    """
    lines = ["你是 A股中长线交易系统的盘前新闻助理。下面是 新浪7x24 + 东方财富快讯(时间倒序,"
             "已标注来源)。跟踪板块: " + ("、".join(sector_names) or "无") + " + 大盘。"]
    if prior:
        lines += ["", "【历史应验率(近20日实测)】", prior,
                  "置信度参考历史应验率:应验率低的类型,同类消息置信降一档。"]
    if sector_perf:
        lines += ["", "【跟踪板块最近交易日涨跌(交叉验证用)】",
                  " ".join(f"{k} {v}" for k, v in sector_perf.items())]
    lines.append("请挑出对以上板块或大盘**最重要**的 5-8 条新闻:")
    lines.append("- 每条压缩到 60 字以内;标注 影响(利好/利空/中性)、类型(宏观政策|行业涨价|龙头业绩|"
                 "海外市场|突发事件|其他)、关联板块(可填 大盘/其他)、来源(新浪/东财)、"
                 "来源可信度(官方|媒体|快讯|传闻)、置信度(高|中|低:官方政策/龙头业绩/行业级涨价→高;"
                 "单条快讯、方向不明→低);用一句话 reason 说明为什么重要")
    lines.append("- **方向判定必须走因果链**:事件 → 传导到该板块的机制 → 方向。'净利润不及预期'本身不是方向,"
                 "要看**原因**:若因资本开支/研发投入大增导致利润不及预期,对上游基础设施(光模块/算力/服务器/"
                 "通信设备/半导体)是**利好**(订单与需求扩张),对该企业自身才是利空。同一新闻对不同板块方向可能"
                 "不同,按'对跟踪板块'的方向标注,reason 必须写出传导机制。")
    lines.append("- **交叉验证**:若判某板块'利空'但该板块最近交易日上涨,或判'利好'但板块下跌,"
                 "reason 必须解释矛盾(如:消息盘后才出明天才反应 / 板块上涨是其他原因 / 传导方向判反)。")
    lines.append("- **宁少勿滥**:同一新闻只挑一次、只标一个最相关的板块,不要重复输出同一条新闻;"
                 "**只能从【快讯】里挑选,严禁编造快讯中不存在的新闻**。")
    lines.append("- **信息不足要诚实**:若快讯只有表面结果(如'净利润不及预期')而没有原因,置信度标**低**,"
                 "方向按最直接传导标注并注明'原因不明';不要给高置信。")
    lines.append("- **优先选宏观/政策/大盘方向/行业级(涨价、扩产、政策、龙头业绩)消息**;"
                 "忽略单个公司注册/股权/琐事、娱乐、重复新闻;宁少勿滥")
    lines.append("\n【快讯】")
    for it in feed[:40]:
        src = "东财" if it.get("tag") else "新浪"
        lines.append(f"- [{src}] {it['time'][5:16]} {it['text'][:90]}")
    lines.append("\n只输出 JSON:")
    lines.append('{"summary":"一句话盘面综述", "items":[{"time":"08:30","text":"...","impact":"利好|利空|中性",'
                 '"type":"宏观政策|行业涨价|龙头业绩|海外市场|突发事件|其他","sector":"化工|大盘|其他",'
                 '"source":"新浪|东财","source_grade":"官方|媒体|快讯|传闻","confidence":"高|中|低","reason":"..."}]}')
    prompt = "\n".join(lines)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.2, max_tokens=2600)
        if not text:
            return {}
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        out = []
        seen_texts: set[str] = set()
        for it in (data.get("items") or [])[:8]:
            impact = it.get("impact", "中性")
            if impact not in _IMPACT_ORDER:
                impact = "中性"
            ctype = it.get("type") or "其他"
            if ctype not in _TYPES:
                ctype = "其他"
            grade = it.get("source_grade") or "快讯"
            if grade not in _GRADES:
                grade = "快讯"
            conf = it.get("confidence") or "低"
            if conf not in ("高", "中", "低"):
                conf = "低"
            prefix = (it.get("text") or "").strip()[:20]
            if not prefix or prefix in seen_texts:
                continue  # 同一新闻重复输出(不同板块),只留第一条
            seen_texts.add(prefix)
            out.append({
                "time": (it.get("time") or "")[:5],
                "text": (it.get("text") or "").strip(),        # 反截断:正文全量入库,不做 [:80]
                "impact": impact,
                "type": ctype,
                "sector": it.get("sector") or "其他",
                "source": (it.get("source") or "新浪")[:4],
                "source_grade": grade,
                "confidence": conf,
                "reason": _snap_sentence(it.get("reason"), 60),  # 反截断:句子边界
            })
        # 幻觉过滤:输出文本须与任一 feed 原文有 ≥5 字公共子串,否则视为编造丢弃
        # (5 字阈值:保住压缩文本如"Q1净利润",同时排除"布202"这类日期巧合)
        feed_grams = set()
        for ft in (it.get("text", "") for it in feed):
            for i in range(max(0, len(ft) - 4)):
                feed_grams.add(ft[i:i + 5])
        for it in list(out):
            t = it["text"]
            if not any(t[i:i + 5] in feed_grams for i in range(max(0, len(t) - 4))):
                logger.warning("丢弃疑似幻觉新闻(快讯中无对应原文): %s", t[:40])
                out.remove(it)
        # 跨源(方案 M2):LLM 压缩文本不回带 cross,按原文重叠回填(前 15 字互相包含)
        feed_cross = [(it.get("text", ""), it.get("cross", 1)) for it in feed]
        for it in out:
            t = it["text"]
            it["cross"] = max([fc for ft, fc in feed_cross if ft[:15] and (t[:15] in ft or ft[:15] in t)],
                              default=1)
        out.sort(key=lambda x: _IMPACT_ORDER.get(x["impact"], 9))
        return {"summary": _snap_sentence(data.get("summary"), 220),   # 反截断:句子边界兜底
                "items": out}
    except Exception as exc:  # noqa: BLE001
        logger.warning("重要新闻 LLM 解析失败: %s", exc)
        return {}


def _rule_important_news(sector_hits: dict[str, list[dict]], sector_names: list[str],
                         feed: list[dict] | None = None,
                         llm_reason: str = "AI 调用失败") -> dict:
    """LLM 不可用时的降级,两档兜底(不编造判断,但也不把用户晾在空态):

    ① 板块关键词命中:每板块取最近 1-2 条命中原文(影响标中性、低置信);
    ② 全无命中:展示各数据源最近的原始快讯(标注"未经 AI 筛选"),让盘前仍有信息量——
       数据源本身是可用的(新浪+东财各 50 条),失败的是 AI 筛选这一步,不该让用户看到"没有新闻"。
    """
    items: list[dict] = []
    for sector in sector_names:
        for it in (sector_hits.get(sector) or [])[:2]:
            items.append({
                "time": it.get("time", "")[5:16][:5] if it.get("time") else "",
                "text": (it.get("text") or "").strip(),   # 反截断:原文全量
                "impact": "中性",
                "type": "其他",
                "sector": sector,
                "source": "新浪" if it.get("tag") else "东财",
                "source_grade": "快讯",
                "confidence": "低",
                "cross": it.get("cross", 1),
                "reason": "板块关键词命中(AI 筛选未成功,人工判断)",
            })
    if not items and feed:
        # 兜底:原始快讯最近 8 条(按时间倒序,去掉跨源重复)
        seen: set[str] = set()
        raw = []
        for it in sorted(feed, key=lambda x: str(x.get("time") or ""), reverse=True):
            t = (it.get("text") or "").strip()[:20]
            if not t or t in seen:
                continue
            seen.add(t)
            raw.append({
                "time": (it.get("time") or "")[5:16][:5],
                "text": (it.get("text") or "").strip(),   # 反截断:原文全量
                "impact": "中性",
                "type": "其他",
                "sector": "其他",
                "source": "新浪" if it.get("tag") else "东财",
                "source_grade": "快讯",
                "confidence": "低",
                "cross": it.get("cross", 1),
                "reason": "原始快讯(未经 AI 筛选,重要度请自行判断)",
            })
            if len(raw) >= 8:
                break
        items = raw
        summary = f"AI 筛选未成功,展示最近原始快讯({len(items)} 条)"
    elif items:
        summary = f"AI 筛选未成功,按板块关键词命中展示({len(items)} 条)"
    else:
        # 数据源真的一条都没有(断网/全部失败)才走这里
        items.append({"time": "", "text": "数据源本轮未取到快讯", "impact": "中性",
                      "type": "其他", "sector": "其他", "source_grade": "快讯",
                      "confidence": "低", "cross": 1, "reason": "新闻源抓取为空"})
        summary = "数据源本轮未取到快讯"
    return {"summary": summary, "items": items}


def _sector_perf(conn, sector_syms: dict[str, str]) -> dict[str, str]:
    """跟踪板块最近交易日涨跌幅 {板块名: "+2.3%"}(方向判定交叉验证用)。"""
    out: dict[str, str] = {}
    for name, sym in sector_syms.items():
        if not sym:
            continue
        rows = conn.execute(
            "SELECT close FROM etf_kline_daily WHERE symbol=? ORDER BY trade_date DESC LIMIT 2",
            (sym,)).fetchall()
        if len(rows) == 2 and rows[1]["close"]:
            out[name] = f"{(rows[0]['close'] / rows[1]['close'] - 1) * 100:+.1f}%"
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
                 '"sectors": {"板块名": {"status": "增强|减弱|新增|未变", "impact": "利好|利空|中性", '
                 '"confidence": "高|中|低", "reason": "依据", "risk": "风险", "scenario": "条件式情景"}}}')
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
                                "scenario": v.get("scenario", ""),
                                "impact": v.get("impact", "中性"),
                                "confidence": v.get("confidence", "低")}
        return {"summary": data.get("summary", ""),
                "watch": data.get("watch", []) or [],
                "verdict": verdict}
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 输出解析失败: %s", exc)
        return None


def _format_briefing(sectors: list[dict], verdict: dict[str, dict], cfg,
                     summary: str = "", watch: list[str] | None = None,
                     important: dict | None = None) -> str:
    """盘前简报 markdown(§6.2:隔夜重要消息 + 催化状态 + 关键价位 + 风险 + 情景)。

    important(方案 M3):逐条重要消息,对齐 mockup 版式(🔴利好/🟢利空/⚪中性)。
    """
    watch = watch or []
    today = date.today().isoformat()
    lines = [f"# AlphaPrism 盘前简报 {today}"]
    if summary:
        lines += ["", f"> **盘面综述**:{summary}"]
    imp_items = (important or {}).get("items") or []
    if imp_items:
        lines += ["", "## 〇、隔夜重要消息"]
        for it in imp_items:
            tag = "🔴" if it.get("impact") == "利好" else "🟢" if it.get("impact") == "利空" else "⚪"
            meta = f"[{it.get('type', '其他')}/{it.get('source_grade', '快讯')}/{it.get('confidence', '低')}置信"
            if it.get("cross", 1) > 1:
                meta += f"/×{it['cross']}源"
            meta += "]"
            sec = f"→{it['sector']}" if it.get("sector") and it["sector"] != "其他" else ""
            lines.append(f"- {tag} {it.get('time', '')} {it.get('text', '')} {meta} {sec}")
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
        ctx = _build_context(cfg, conn)
        sectors = ctx["sectors"]
        prev = _yesterday_status(conn)
        # 隔夜重要消息逐条(方案 M3);数据源不可用/陈旧则留空,不编造
        important = {"summary": "", "items": []}
        health = ctx.get("news_health") or {}
        if not (health.get("all_failed") or health.get("stale")):
            kws = cfg.get("news", "keywords", default={}) or {}
            sector_names = sorted({s["sector"] for s in sectors if s["sector"]})
            sector_hits = news_mod.filter_by_keywords(ctx["feed"], kws)
            perf = _sector_perf(conn, {s["sector"]: s["symbol"] for s in sectors})
            important = _llm_important_news(ctx["feed"], sector_hits, sector_names, cfg,
                                            prior=prior_prompt(conn), sector_perf=perf)
            if not important.get("items"):
                important = _rule_important_news(sector_hits, sector_names, feed=ctx["feed"])
        summary, watch = "", []
        judge = _llm_judge(sectors, prev, cfg)
        if judge:
            verdict = judge["verdict"]
            summary = judge.get("summary", "")
            watch = judge.get("watch", []) or []
        else:
            verdict = _rule_fallback(sectors)
        # 存催化状态(含 impact/confidence,方案 M4)
        today = date.today().isoformat()
        rows = [(today, sym, v.get("status", "未变"), v.get("reason", ""),
                 v.get("impact", "中性"), v.get("confidence", "低"),
                 v.get("type"), v.get("source_grade"), v.get("cross"), today)
                for sym, v in verdict.items()]
        upsert_rows(conn, "catalyst_status", CATALYST_STATUS_COLUMNS, rows)
        conn.commit()
        briefing = _format_briefing(sectors, verdict, cfg, summary, watch, important=important)
        if push and cfg.get("push", "enabled", default=False):
            send("盘前简报", briefing, level="alert", cfg=cfg)
        logger.info("盘前简报完成(%d 板块)", len(sectors))
        return briefing
    finally:
        conn.close()
