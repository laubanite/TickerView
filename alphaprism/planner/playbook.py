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
from ..morning import _build_context
from .rulemodel import RuleModel

logger = logging.getLogger(__name__)

_WEEKDAYS = "一二三四五六日"


def _weekday(d: date) -> str:
    return _WEEKDAYS[d.weekday()]


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
            "levels": [{"name": l.name, "price": l.price, "source": l.source}
                       for l in instr.levels],
            "rules": [{"action": r.action, "condition": r.condition,
                       "operation": r.operation, "computable": r.computable}
                      for r in instr.rules],
        })

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