"""盘前生成(产品方案 §5.3 盘前 tab / §4.1 daily.playbook,里程碑2):剧本草稿 → 人工确认 → 追加写入。

规则驱动:引擎从作战地图 levels + 库内技术面 + 隔夜新闻/异动组上下文,LLM 写
每只标的的「隔夜影响 / 今日动作」;LLM 不可用 → 降级为确定性模板(引规则条件)。
生成的是**草稿**,必须走盘前确认流(人工确认/修改)后才写入作战地图「每日盯盘记录」——
计划层不被机器污染(§三 原则4)。

输出:剧本草稿 markdown(格式与人工手写盘前条目一致,可无缝替换),写入格式:
  ### 📅 YYYY-MM-DD(周X)盘前 HH:MM(剧本草稿·待确认)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from ..config import Config
from ..db import connect, init_db
from ..fetchers import news as news_mod
from ..morning import _build_context, _llm_important_news, _rule_important_news, _yesterday_status
from .rulemodel import RuleModel

logger = logging.getLogger(__name__)

_WEEKDAYS = "一二三四五六日"


def _weekday(d: date) -> str:
    return _WEEKDAYS[d.weekday()]


# ---------------------------------------------------------------- 上下文

def _instrument_contexts(model: RuleModel, sectors: list[dict]) -> list[dict]:
    """每只标的的上下文:levels/rules + 板块新闻/异动/技术面/拥挤度。"""
    by_sym = {s["symbol"]: s for s in sectors}
    rows: list[dict[str, Any]] = []
    for instr in model.instruments:
        sctx = by_sym.get(instr.code) or {}
        news = sctx.get("news") or []
        tech = sctx.get("tech") or {}
        rows.append({
            "code": instr.code,
            "name": instr.name,
            "sector": sctx.get("sector") or "",
            "news": news,
            "anomaly_count": len(sctx.get("anomalies") or []),
            "tech": tech,
            "crowd": sctx.get("crowd"),
            "levels": [{"name": l.name, "price": l.price, "source": l.source}
                       for l in instr.levels],
            "rules": [{"action": r.action, "condition": r.condition,
                       "operation": r.operation, "computable": r.computable}
                      for r in instr.rules],
        })
    return rows


# ---------------------------------------------------------------- 草稿生成

def build_draft(model: RuleModel, cfg: Config | None = None) -> dict:
    """生成今日剧本草稿。

    上下文:morning._build_context(新浪7x24新闻按板块命中 + 同花顺异动/热榜 + 库内技术面 + 拥挤度)。
    每只标的:levels(买区/突破/红线/生命线) + rules → LLM 写隔夜影响/今日动作。
    返回 {"date", "summary", "rows":[{code,name,overnight,action}]}。
    """
    cfg = cfg or Config()
    conn = connect()
    try:
        sectors = _build_context(cfg, conn)["sectors"]
    finally:
        conn.close()

    rows = _instrument_contexts(model, sectors)
    draft = _llm_rows(rows, model, cfg)
    return {"date": date.today().isoformat(),
            "summary": draft.get("summary", ""),
            "rows": draft.get("rows") or _fallback_rows(rows)}


def _llm_rows(rows: list[dict], model: RuleModel, cfg) -> dict:
    """LLM 写每只标的的隔夜影响/今日动作。失败返回 {"rows": []}(调用方降级)。"""
    from ..llm import chat

    lines = ["你是 A股中长线交易系统的盘前剧本助手。基于作战地图关键位 + 隔夜新闻 + 技术状态,"
             "为每只标的写'今日剧本修正'。今日动作必须用'若…则…'并引用具体价位,不编造数字。"]
    lines.append("\n【大盘门控】")
    g = model.global_.market_gate
    if g.conclusion:
        lines.append(f"- {g.conclusion}")
    for r in g.rules:
        lines.append(f"- 门控: {r.condition} → {r.action}")
    if model.global_.discipline:
        lines.append("\n【通用纪律(节选)】")
        for d in model.global_.discipline[:4]:
            lines.append(f"- {d}")
    lines.append("\n【各标的上下文】")
    for r in rows:
        lines.append(f"\n* {r['name']} {r['code']}({r['sector']})")
        if r["levels"]:
            lv = " ".join(f"{l['name']}{l['price']}" + (f"({l['source']})" if l['source'] else "")
                          for l in r["levels"])
            lines.append(f"  关键位: {lv}")
        t = r["tech"]
        if t:
            lines.append(f"  技术: {t.get('structure')}结构 支撑{t.get('support')}/压力{t.get('resistance')} 信号:{t.get('signal')}")
        if r["news"]:
            for it in r["news"][:3]:
                lines.append(f"  新闻: {it['time'][5:16]} {it['text'][:70]}")
        elif r["anomaly_count"]:
            lines.append(f"  异动 {r['anomaly_count']} 条,无新消息")
        else:
            lines.append("  无新增消息/异动")
    lines.append("\n只输出 JSON:")
    lines.append('{"summary": "一句话盘面综述(含大盘方向)", '
                 '"rows": [{"code": "516020", "overnight": "隔夜影响(1行,有消息写消息,无则写无新增消息)", '
                 '"action": "今日动作(若…则…,引用具体价位)"}]}')
    prompt = "\n".join(lines)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.3, max_tokens=1200)
        if not text:
            return {"rows": []}
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        code_set = {r["code"] for r in rows}
        out = []
        for row in data.get("rows") or []:
            code = str(row.get("code", "")).split(".")[0]
            if code not in code_set:
                continue
            out.append({"code": code,
                        "name": next((r["name"] for r in rows if r["code"] == code), ""),
                        "overnight": (row.get("overnight") or "").strip(),
                        "action": (row.get("action") or "").strip()})
        return {"summary": (data.get("summary") or "").strip(), "rows": out}
    except Exception as exc:  # noqa: BLE001
        logger.warning("剧本 LLM 解析失败: %s", exc)
        return {"rows": []}


def _fallback_rows(rows: list[dict]) -> list[dict]:
    """LLM 不可用时的降级:引规则条件 + 新闻摘要(不编造解读)。"""
    out: list[dict] = []
    for r in rows:
        news_txt = "、".join(dict.fromkeys(it["text"][:40] for it in r["news"][:3])) or "无新增消息"
        actions = []
        for rule in r["rules"]:
            if not rule["computable"]:
                continue
            actions.append(f"{rule['condition']}→{rule['operation']}")
        act = "；".join(actions) if actions else "等信号(回踩企稳/放量突破),不追高"
        out.append({"code": r["code"], "name": r["name"], "overnight": news_txt, "action": act})
    return out


# ---------------------------------------------------------------- 格式化

def format_draft(draft: dict, model: RuleModel, now: datetime | None = None) -> str:
    """草稿 dict → 盘前条目标记块(与手写格式一致,可无缝替换)。"""
    now = now or datetime.now()
    lines = [f"### 📅 {now:%Y-%m-%d}({_weekday(now.date())})盘前 {now:%H:%M}(剧本草稿·待确认)"]
    if draft.get("summary"):
        lines += ["", f"> **盘面综述**:{draft['summary']}"]
    lines += ["", "**今日剧本修正(草稿)**", "", "| 品种 | 隔夜影响 | 今日动作 |", "|---|---|---|"]
    for r in draft.get("rows") or []:
        ov = (r.get("overnight") or "-").replace("\n", " ").replace("|", "｜")
        act = (r.get("action") or "-").replace("\n", " ").replace("|", "｜")
        lines.append(f"| {r.get('name', '')} {r.get('code', '')} | {ov} | {act} |")
    g = model.global_.market_gate
    disc = model.global_.discipline[:3]
    lines += ["", "**今日纪律(沿用 §六)**:",
              f"- 大盘总闸门: {g.conclusion or '-'}"]
    for d in disc:
        lines.append(f"- {d}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 写入盯盘记录

def append_to_journal(md: str, path: str) -> None:
    """把盘前草稿块追加/替换进作战地图「每日盯盘记录」。

    规则:按日期分块;该日期已有「盘前」块 → 替换;只有「盘后」→ 在盘后前插入;
    无该日期 → 追加到盯盘记录段落末尾(无段落则创建)。保持原文件其余内容不动。
    """
    from pathlib import Path

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    date_md = md.splitlines()[0].replace("### 📅 ", "").split("(")[0]  # YYYY-MM-DD
    marker = f"### 📅 {date_md}"

    # 收集该日期各块边界(块 = 一个 ### 📅 标题到下一个标题或文末)
    blocks: list[tuple[int, int, str]] = []
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.strip().startswith(f"### 📅 {date_md}")]
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        blocks.append((s, e, "".join(lines[s:e])))

    if blocks:
        # 该日期已有「盘前」块 → 替换
        for i, (s, e, block) in enumerate(blocks):
            if "盘前" in block:
                new_text = "".join(lines[:s]) + md + "\n" + "".join(lines[e:])
                p.write_text(new_text, encoding="utf-8")
                return
        # 有该日期但无盘前 → 在「盘后」块前插入(盘前应在盘后之前)
        insert_at = e  # 默认插到该日期最后一块之后
        for s, e, block in blocks:
            if "盘后" in block:
                insert_at = s
                break
        new_text = "".join(lines[:insert_at]) + "\n" + md + "\n" + "".join(lines[insert_at:])
        p.write_text(new_text, encoding="utf-8")
        return

    # 无该日期 → 追加到「每日盯盘记录」段落末尾;无段落则创建
    seg_start = text.find("每日盯盘记录")
    if seg_start >= 0:
        new_text = text.rstrip() + "\n\n" + md + "\n"
    else:
        new_text = text.rstrip() + "\n\n## 每日盯盘记录\n\n" + md + "\n"
    p.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------- 盘前视图(实时+LLM,三块联动)

def build_morning_view(model: RuleModel, cfg: Config | None = None) -> dict:
    """盘前视图:①隔夜重要消息(新浪+东财实时 + LLM 提取/分类) + ②今日剧本 + ③今日纪律。

    联动:LLM 先提取重要新闻(带 利空/利好/中性 + 板块 + 来源),再在同一上下文里生成
    剧本(隔夜影响/今日动作,唯一可编辑真源,写入作战地图)与**今日纪律**(结合消息面催化,
    非照搬作战地图 §六)。返回结构化 dict 供前端分块渲染。
    LLM 任一环节失败 → 各自降级(新闻给板块命中原文,剧本给规则模板,纪律给 §六 节选)。
    """
    cfg = cfg or Config()
    conn = connect()
    try:
        init_db(conn)   # 确保 important_news/catalyst_verification 等表存在(幂等)
        ctx = _build_context(cfg, conn)
        sectors = ctx["sectors"]
        prev = _yesterday_status(conn)
        news_health = ctx.get("news_health")

        rows = _instrument_contexts(model, sectors)

        # ① 隔夜重要消息:复用 _build_context 的抓取结果(含健康状态),避免二次抓取
        feed = ctx.get("feed") or []
        kws = cfg.get("news", "keywords", default={}) or {}
        if news_health and (news_health.get("all_failed") or news_health.get("stale")):
            # 数据源不可用 / 陈旧:坏数据不进列表,消息区留空由前端显示诚实文案
            important = {"summary": "", "items": []}
        else:
            sector_hits = news_mod.filter_by_keywords(feed, kws)
            sector_names = sorted({r["sector"] for r in rows if r["sector"]})
            from ..morning import _sector_perf
            from ..morning_verify import prior_prompt

            important = _llm_important_news(feed, sector_hits, sector_names, cfg,
                                            prior=prior_prompt(conn),
                                            sector_perf=_sector_perf(conn, {r["sector"]: r["code"] for r in rows}))
            if not important.get("items"):
                important = _rule_important_news(sector_hits, sector_names)
        # 持久化当日重要消息(供 M4 验证)+ 顺手跑到期验证(幂等)
        _persist_important_news(conn, important, rows, cfg)
        _run_verification(conn)

        # ② 剧本(唯一可编辑真源) + ③ 今日纪律(消息面催化驱动)
        pp = _llm_playbook_discipline(model, rows, important, cfg)
        playbook = pp.get("playbook") or _fallback_rows(rows)
        discipline = pp.get("discipline") or _fallback_discipline(model)
        # 保证每只标的都有行(LLM 覆盖不全时用规则模板补齐)
        playbook = _merge_missing(playbook, _fallback_rows(rows))

        g = model.global_.market_gate
        return {
            "date": date.today().isoformat(),
            "summary": important.get("summary", ""),
            "gate": {"conclusion": g.conclusion or "",
                     "rules": [{"condition": r.condition, "action": r.action} for r in g.rules]},
            "news": important,
            "news_health": news_health,
            "playbook": playbook,
            "discipline": discipline,
        }
    finally:
        conn.close()


def _persist_important_news(conn, important: dict, rows: list[dict], cfg) -> None:
    """当日重要消息写入 important_news(方案 M4,验证输入)。按 (trade_date, text) upsert。"""
    from ..morning_verify import verify_pending  # noqa: F401 (调用方 _run_verification 再引)

    kws_map = cfg.get("news", "keywords", default={}) or {}
    all_kws = sorted({k for ks in kws_map.values() for k in ks})
    sector2sym = {r["sector"]: r["code"] for r in rows if r["sector"]}
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    for it in (important.get("items") or []):
        text = (it.get("text") or "").strip()
        if not text:
            continue
        hit_kws = [k for k in all_kws if k in text]
        conn.execute(
            "INSERT INTO important_news (trade_date, sector, symbol, text, impact, confidence, "
            "type, source_grade, cross, source, keywords, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_date, text) DO UPDATE SET impact=excluded.impact, "
            "confidence=excluded.confidence, type=excluded.type, source_grade=excluded.source_grade, "
            "cross=excluded.cross, keywords=excluded.keywords",
            (today, it.get("sector") or "其他", sector2sym.get(it.get("sector")),
             text, it.get("impact", "中性"), it.get("confidence"), it.get("type"),
             it.get("source_grade"), it.get("cross", 1), it.get("source"),
             json.dumps(hit_kws, ensure_ascii=False), now))
    conn.commit()


def _run_verification(conn) -> None:
    """到期催化判定跑 N 日验证(幂等)。失败不阻断盘前视图。"""
    try:
        from ..morning_verify import verify_pending

        res = verify_pending(conn)
        if res.get("verified"):
            logger.info("催化验证新增 %d 条", res["verified"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("催化验证失败(不阻断视图): %s", exc)


# ---------------------------------------------------------------- ②③ 剧本+预案

def _llm_playbook_discipline(model: RuleModel, rows: list[dict], important: dict, cfg) -> dict:
    """LLM 在同一消息面上下文里生成 今日剧本(隔夜影响/今日动作) + 今日纪律(消息面催化驱动)。"""
    from ..llm import chat

    lines = ["你是 A股中长线交易系统的盘前剧本与纪律助手。基于**隔夜重要新闻** + 作战地图关键位 + 技术面,"
             "为每只标的生成今日剧本(隔夜影响/今日动作),并给出**今日纪律**(结合消息面催化,"
             "不是照搬通用纪律——把利空/利好落到'今天具体怎么防/怎么等'上)。",
             "规则:动作必须引用真实价位、用『价格→操作』格式(如『回踩0.855-0.860企稳→第一批10%』、"
             "『放量突破0.90→观察升级』、『跌破减仓红线0.83→减1/3留一手』),不要用'若…则…'长句、"
             "不编造数字;剧本应体现消息面催化的影响。",
             "纪律要求:**每条不超过 30 字、一句话、只讲今天最要紧的一条**,宁少勿滥(3-5 条)。"]
    lines.append("\n【盘面综述】")
    lines.append(f"- {important.get('summary') or '-'}")
    lines.append("\n【隔夜重要新闻】")
    for it in important.get("items") or []:
        lines.append(f"- [{it['impact']}]({it.get('sector')}/{it.get('source')}) {it.get('time')} {it.get('text')} —— {it.get('reason')}")
    g = model.global_.market_gate
    lines.append("\n【大盘门控】")
    if g.conclusion:
        lines.append(f"- {g.conclusion}")
    for r in g.rules:
        lines.append(f"- 门控: {r.condition} → {r.action}")
    if model.global_.discipline:
        lines.append("\n【通用纪律(节选,仅作底线,今日纪律要结合消息改写)】")
        for d in model.global_.discipline[:4]:
            lines.append(f"- {d}")
    lines.append("\n【各标的】")
    for r in rows:
        lines.append(f"\n* {r['name']} {r['code']}({r['sector']})")
        key_levels = [l for l in r["levels"]
                      if l["name"] in ("买区下沿", "买区上沿", "突破点", "减仓红线", "生命线")]
        if key_levels:
            lv = " ".join(f"{l['name']}{l['price']}" for l in key_levels)
            lines.append(f"  关键位: {lv}")
        t = r["tech"]
        if t:
            lines.append(f"  技术: {t.get('structure')}结构 支撑{t.get('support')}/压力{t.get('resistance')} 信号:{t.get('signal')}")
        hits = [it for it in important.get("items") or [] if it.get("sector") == r["sector"]]
        if hits:
            lines.append(f"  相关新闻: {'; '.join(it['text'] for it in hits[:2])}")
        elif r["anomaly_count"]:
            lines.append(f"  异动 {r['anomaly_count']} 条,无新消息")
        else:
            lines.append("  无新增消息/异动")
    lines.append("\n只输出 JSON,每只标的都要有 playbook 行;discipline 为 3-5 条今日纪律:")
    lines.append('{"playbook":[{"code":"516020","overnight":"隔夜影响(1行)","action":"今日动作(价格→操作,引用具体价位)"}], '
                 '"discipline":["今日纪律1(结合消息面)","今日纪律2",...]}')
    prompt = "\n".join(lines)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.3, max_tokens=2000)
        if not text:
            return {}
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        code_set = {r["code"] for r in rows}
        out = []
        for row in data.get("playbook") or []:
            code = str(row.get("code", "")).split(".")[0]
            name = next((r["name"] for r in rows if r["code"] == code), "")
            if not name and not code:
                name = row.get("name", "大盘")
            out.append({"code": code, "name": name,
                        "overnight": (row.get("overnight") or "").strip(),
                        "action": (row.get("action") or "").strip()})
        discipline = [str(d).strip() for d in (data.get("discipline") or []) if str(d).strip()]
        return {"playbook": out, "discipline": discipline}
    except Exception as exc:  # noqa: BLE001
        logger.warning("剧本/纪律 LLM 解析失败: %s", exc)
        return {}


def _fallback_discipline(model: RuleModel) -> list[str]:
    """纪律降级:大盘门控结论 + §六 通用纪律节选(不编造消息面解读)。"""
    g = model.global_.market_gate
    out: list[str] = []
    if g.conclusion:
        out.append(f"大盘: {g.conclusion}")
    for d in model.global_.discipline[:4]:
        out.append(d)
    return out or ["回踩买、突破买,绝不追买;等缩量企稳信号,不接飞刀"]


def _merge_missing(entries: list[dict], fallback: list[dict]) -> list[dict]:
    """LLM 未覆盖的标的用规则模板补齐,顺序:无码行(如大盘)在前,其余按作战地图顺序。"""
    by_code = {e.get("code"): e for e in entries if e.get("code")}
    extra = [e for e in entries if not e.get("code")]   # 大盘等无码行
    fb_by = {f.get("code"): f for f in fallback}
    out = []
    for f in fallback:
        code = f.get("code")
        out.append(by_code.get(code, fb_by.get(code, f)))
    return extra + out