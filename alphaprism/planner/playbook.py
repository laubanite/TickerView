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
from ..db import connect
from ..fetchers import news as news_mod
from ..morning import _build_context, _yesterday_status
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
    """盘前视图:①隔夜重要消息(新浪7x24 实时 + LLM 提取/分类) + ②今日剧本 + ③盘前预案。

    三者联动:LLM 先提取重要新闻(带 利空/利好/中性 + 板块),再在同一上下文里生成
    剧本(隔夜影响/今日动作)与预案(当前/若…则…)。返回结构化 dict 供前端分块渲染。
    LLM 任一环节失败 → 各自降级(新闻给板块命中原文,剧本/预案给规则模板)。
    """
    cfg = cfg or Config()
    conn = connect()
    try:
        ctx = _build_context(cfg, conn)
        sectors = ctx["sectors"]
        prev = _yesterday_status(conn)
    finally:
        conn.close()

    rows = _instrument_contexts(model, sectors)

    # ① 隔夜重要消息:实时抓取 + LLM 提取
    kws = cfg.get("news", "keywords", default={}) or {}
    feed = news_mod.fetch_sina_feed(
        pages=cfg.get("news", "pages", default=1),
        page_size=cfg.get("news", "page_size", default=50),
    )
    sector_hits = news_mod.filter_by_keywords(feed, kws)
    important = _llm_important_news(feed, sector_hits, rows, cfg)
    if not important.get("items"):
        important = _rule_important_news(sector_hits, rows)

    # ② 剧本 + ③ 预案(联动重要新闻)
    pp = _llm_playbook_plan(model, rows, important, cfg)
    playbook = pp.get("playbook") or _fallback_rows(rows)
    plan = pp.get("plan") or _fallback_plan(rows)
    # 保证每只标的都有行(LLM 覆盖不全时用规则模板补齐)
    playbook = _merge_missing(playbook, _fallback_rows(rows))
    plan = _merge_missing(plan, _fallback_plan(rows))

    g = model.global_.market_gate
    return {
        "date": date.today().isoformat(),
        "summary": important.get("summary", ""),
        "gate": {"conclusion": g.conclusion or "",
                 "rules": [{"condition": r.condition, "action": r.action} for r in g.rules]},
        "news": important,
        "playbook": playbook,
        "plan": plan,
    }


# ---------------------------------------------------------------- ① 隔夜重要消息

_IMPACT_ORDER = {"利空": 0, "利好": 1, "中性": 2}


def _llm_important_news(feed: list[dict], sector_hits: dict[str, list[dict]],
                        rows: list[dict], cfg) -> dict:
    """LLM 从新浪 7x24 快讯里挑重要新闻,分类 利空/利好/中性 并关联板块。失败返回 {}。"""
    from ..llm import chat

    sectors_names = sorted({r["sector"] for r in rows if r["sector"]})
    lines = ["你是 A股中长线交易系统的盘前新闻助理。下面是新浪 7x24 快讯(时间倒序)。"
             f"跟踪板块: {'、'.join(sectors_names) or '无'} + 大盘。"]
    lines.append("请挑出对以上板块或大盘**最重要**的 5-8 条新闻:")
    lines.append("- 每条压缩到 60 字以内;标注 影响(利好/利空/中性) 和 关联板块(可填 大盘/其他);"
                 "用一句话 reason 说明为什么重要")
    lines.append("- 忽略无关/娱乐/重复新闻;宁少勿滥,只保留真正影响盘面的")
    lines.append("\n【快讯】")
    for it in feed[:30]:
        lines.append(f"- {it['time'][5:16]} {it['text'][:90]}")
    lines.append("\n只输出 JSON:")
    lines.append('{"summary":"一句话盘面综述", "items":[{"time":"08:30","text":"...","impact":"利好|利空|中性",'
                 '"sector":"化工|大盘|其他","reason":"..."}]}')
    prompt = "\n".join(lines)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.2, max_tokens=1000)
        if not text:
            return {}
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        out = []
        for it in (data.get("items") or [])[:8]:
            impact = it.get("impact", "中性")
            if impact not in _IMPACT_ORDER:
                impact = "中性"
            out.append({
                "time": (it.get("time") or "")[:5],
                "text": (it.get("text") or "").strip()[:80],
                "impact": impact,
                "sector": it.get("sector") or "其他",
                "reason": (it.get("reason") or "").strip()[:50],
            })
        out.sort(key=lambda x: _IMPACT_ORDER.get(x["impact"], 9))
        return {"summary": (data.get("summary") or "").strip(), "items": out}
    except Exception as exc:  # noqa: BLE001
        logger.warning("重要新闻 LLM 解析失败: %s", exc)
        return {}


def _rule_important_news(sector_hits: dict[str, list[dict]], rows: list[dict]) -> dict:
    """LLM 不可用的降级:每板块取最近 1-2 条命中原文,影响标中性(不编造判断)。"""
    items: list[dict] = []
    for r in rows:
        sector = r["sector"]
        if not sector:
            continue
        for it in (sector_hits.get(sector) or [])[:2]:
            items.append({
                "time": it.get("time", "")[5:16][:5] if it.get("time") else "",
                "text": (it.get("text") or "")[:80],
                "impact": "中性",
                "sector": sector,
                "reason": "板块关键词命中(LLM 未启用,人工判断)",
            })
    if not items:
        items.append({"time": "", "text": "暂无板块相关重要新闻", "impact": "中性",
                      "sector": "其他", "reason": "新浪7x24 关键词无命中"})
    return {"summary": f"共 {len(items)} 条板块相关新闻(降级:LLM 未启用)", "items": items}


# ---------------------------------------------------------------- ②③ 剧本+预案

def _llm_playbook_plan(model: RuleModel, rows: list[dict], important: dict, cfg) -> dict:
    """LLM 在同一消息面上下文里生成 今日剧本(隔夜影响/今日动作) + 盘前预案(当前/若…则…)。"""
    from ..llm import chat

    lines = ["你是 A股中长线交易系统的盘前剧本与预案助手。基于**隔夜重要新闻** + 作战地图关键位 + 技术面,"
             "为每只标的生成两块:今日剧本(隔夜影响/今日动作) + 盘前预案(当前状态/若…则…预案)。",
             "规则:动作必须引用真实价位、用'若…则…'、不编造数字;剧本与预案应体现消息面催化的影响。"]
    lines.append("\n【盘面综述】")
    lines.append(f"- {important.get('summary') or '-'}")
    lines.append("\n【隔夜重要新闻】")
    for it in important.get("items") or []:
        lines.append(f"- [{it['impact']}]({it.get('sector')}) {it.get('time')} {it.get('text')} —— {it.get('reason')}")
    g = model.global_.market_gate
    lines.append("\n【大盘门控】")
    if g.conclusion:
        lines.append(f"- {g.conclusion}")
    for r in g.rules:
        lines.append(f"- 门控: {r.condition} → {r.action}")
    if model.global_.discipline:
        lines.append("\n【通用纪律(节选)】")
        for d in model.global_.discipline[:3]:
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
    lines.append("\n只输出 JSON,每只标的都要有 playbook 行与 plan 行:")
    lines.append('{"playbook":[{"code":"516020","overnight":"隔夜影响(1行)","action":"今日动作(若…则…)"}], '
                 '"plan":[{"code":"516020","current":"当前状态","plan":"今日预案(若…则…)"}]}')
    prompt = "\n".join(lines)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.3, max_tokens=2400)
        if not text:
            return {}
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        code_set = {r["code"] for r in rows}

        def _map(kind):
            out = []
            for row in data.get(kind) or []:
                code = str(row.get("code", "")).split(".")[0]
                name = next((r["name"] for r in rows if r["code"] == code), "")
                if not name and not code:
                    name = row.get("name", "大盘")
                if kind == "playbook":
                    out.append({"code": code, "name": name,
                                "overnight": (row.get("overnight") or "").strip(),
                                "action": (row.get("action") or "").strip()})
                else:
                    out.append({"code": code, "name": name,
                                "current": (row.get("current") or "").strip(),
                                "plan": (row.get("plan") or "").strip()})
            return out

        return {"playbook": _map("playbook"), "plan": _map("plan")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("剧本/预案 LLM 解析失败: %s", exc)
        return {}


def _fallback_plan(rows: list[dict]) -> list[dict]:
    """预案降级:当前 = 技术状态;预案 = 规则条件(若…则…)。"""
    out: list[dict] = []
    for r in rows:
        t = r["tech"]
        current = f"{t.get('structure')}结构,支撑{t.get('support')}/压力{t.get('resistance')}" if t else "无技术状态"
        acts = []
        for rule in r["rules"]:
            if rule["computable"]:
                acts.append(f"若{rule['condition']}→{rule['operation']}")
        plan = "；".join(acts) if acts else "等信号(回踩企稳/放量突破),不追高"
        out.append({"code": r["code"], "name": r["name"], "current": current, "plan": plan})
    return out


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