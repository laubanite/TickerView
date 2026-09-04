# -*- coding: utf-8 -*-
"""盘前市场状态标签 T1(2026-09)·对应 docs/盘前方案.md v3.3。

纯消息维度:ETF 关键词库(极性×严重级)+ 否定排除 → 绿/黄/红 天气灯。
- 全部确定性,LLM 不参与判定(消息 impact 标签仅作旁证展示,不进定级);
- 技术风险(昨日/盘中)一律不在此模块——由盘中 C15 承担(生效 = max(T1, C15),接线后置);
- 利好/中性命中只计数展示,不参与灯色。

数据流:消息列表(盘前 API 过滤后)→ compose_state → 状态卡载荷 + 写 pre_state 表。
"""
from __future__ import annotations

import re
from datetime import datetime

# 关键词库:类别 × 极性(利空/利好/中性) × 严重级(high 高危 / medium 中度)
#   命中:文本包含任一词;否定排除:同文本出现任一 negation → 该类别不命中
#   极性=利空 → 参与定级(high→红, medium→黄);利好/中性 → 仅计数
KEYWORD_LIB: list[dict] = [
    # ---- 利空 · 高危(直判红灯)----
    {"category": "立案调查", "polarity": "利空", "severity": "high",
     "patterns": ["立案", "立案调查", "立案告知"], "negations": ["撤销立案"]},
    {"category": "退市风险", "polarity": "利空", "severity": "high",
     "patterns": ["退市", "终止上市", "暂停上市"], "negations": ["撤销退市", "摘帽"]},
    {"category": "制裁管制", "polarity": "利空", "severity": "high",
     "patterns": ["制裁", "出口管制", "实体清单", "禁售"], "negations": ["取消制裁", "解除制裁", "移出实体清单"]},
    {"category": "暂停交易", "polarity": "利空", "severity": "high",
     "patterns": ["停牌", "暂停交易"], "negations": ["复牌"]},
    {"category": "业绩暴雷", "polarity": "利空", "severity": "high",
     "patterns": ["预亏", "大幅下修", "业绩变脸", "爆雷"], "negations": []},
    {"category": "股东减持", "polarity": "利空", "severity": "high",
     "patterns": ["减持", "拟减持"], "negations": ["取消减持", "终止减持"]},
    # ---- 利空 · 中度(→黄灯)----
    {"category": "加征关税", "polarity": "利空", "severity": "medium",
     "patterns": ["加征关税", "提高关税", "关税上调", "贸易壁垒"], "negations": ["取消关税", "关税下调", "降低关税"]},
    {"category": "价格压力", "polarity": "利空", "severity": "medium",
     "patterns": ["降价", "价格战", "产能过剩"], "negations": ["涨价"]},
    # ---- 利好(不参与定级,仅计数)----
    {"category": "政策补贴", "polarity": "利好", "severity": None,
     "patterns": ["补贴", "财政支持", "产业基金", "大基金"], "negations": []},
    {"category": "流动性宽松", "polarity": "利好", "severity": None,
     "patterns": ["降准", "降息", "MLF", "LPR下调"], "negations": []},
]

SEVERITY_ORDER = {"high": 2, "medium": 1}

# 卡面文案(用户语言,一行式)· docs/盘前方案.md §五
STATE_TEXT = {
    "green":  {"name": "今日正常", "hint": "无干扰，按计划执行。"},
    "yellow": {"name": "今日谨慎", "hint": "有干扰，少动不追涨。"},
    "red":    {"name": "今日警惕", "hint": "风险高，只守仓，止损照旧。"},
}

# 来源分级映射(证据展示用):官方→S / 媒体→A / 快讯→B;传闻不参与定级
GRADE_SRC = {"官方": "S级", "媒体": "A级", "快讯": "B级"}

_PARTICLE_RE = re.compile(r"(ETF|LOF|基金|指数|联接|QDII|上市|开放式|交易型)")
_SENT_END = "。！？；.!?;"


def _snap(s: str, n: int) -> str:
    """句子边界截断(反截断规则):只允许落在句读之后补省略号,不产生半句。"""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    idx = max(cut.rfind(c) for c in _SENT_END)
    return (cut[: idx + 1] if idx >= 0 else cut).rstrip("；;") + "…"


def _now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def _theme_grams(name: str) -> set[str]:
    """标的名字 → 中文主题二元组集(剥离 ETF 等粒子),用于标的是否被消息波及的粗匹配。"""
    core = _PARTICLE_RE.sub("", str(name or ""))
    return {core[i:i + 2] for i in range(max(0, len(core) - 1)) if core[i:i + 2].strip()}


def match_text(text: str) -> list[dict]:
    """单条消息 → 命中类别列表(含否定排除)。"""
    out = []
    for lib in KEYWORD_LIB:
        if not any(p in text for p in lib["patterns"]):
            continue
        if any(ng in text for ng in lib["negations"]):
            continue
        out.append(lib)
    return out


def _tag_for(text: str) -> str:
    """消息 → 展示用命中标签(证据列表标注用):利空·高危(类别)/利空·中度(类别)/利好/""。"""
    hits = match_text(text)
    for h in hits:
        if h["polarity"] == "利空":
            sev = "高危" if h["severity"] == "high" else "中度"
            return f"利空·{sev}({h['category']})"
    if any(h["polarity"] == "利好" for h in hits):
        return "利好"
    return ""


def _evidence_lines(hits: list[dict]) -> list[str]:
    """利空命中按 (类别,严重级) 聚合为证据行。"""
    agg: dict[tuple[str, str], dict] = {}
    for h in hits:
        key = (h["category"], h["severity"])
        g = agg.setdefault(key, {"n": 0, "grades": set()})
        g["n"] += 1
        g["grades"].add(GRADE_SRC.get(h.get("source_grade", ""), "快讯级"))
    lines = []
    for (cat, sev), g in sorted(agg.items(), key=lambda kv: -SEVERITY_ORDER[kv[0][1]]):
        src = sorted(g["grades"])
        lines.append(f"利空·{'高危' if sev == 'high' else '中度'}「{cat}」×{g['n']}({'/'.join(src)})")
    return lines


def _per_symbol_labels(watchlist: list[dict], items: list[dict]) -> list[dict]:
    """自选池每标的一枚小灯(纯消息维度):标的是否被利空消息波及。

    匹配:标的名字主题二元组 或 消息板块 出现在 利空命中消息的文本/板块 中。
    """
    out = []
    for w in watchlist:
        name = str(w.get("name") or "")
        sym = str(w.get("symbol") or "")
        grams = _theme_grams(name)
        symbol_hits: list[tuple[str, str]] = []
        for it in items:
            text = str(it.get("text") or "")
            sector = str(it.get("sector") or "")
            for h in match_text(text):
                if h["polarity"] != "利空":
                    continue
                touched = any(g and g in text + " " + sector for g in grams) or \
                          (sector and (sector in name or core_txt_inside(sector, name)))
                if not touched:
                    continue
                symbol_hits.append((h["severity"], h["category"]))
        level = "green"
        if any(sev == "high" for sev, _c in symbol_hits):
            level = "red"
        elif any(sev == "medium" for sev, _c in symbol_hits):
            level = "yellow"
        cats = sorted({c for _s, c in symbol_hits})
        if level == "green" and not sym:
            continue
        out.append({"symbol": sym, "name": name, "level": level,
                    "hits": [c for c in cats] if symbol_hits else []})
    return out


def core_txt_inside(sector: str, name: str) -> bool:
    """板块名与标的名互为子串(去粒子后),如 sector='光伏' ∈ name='光伏ETF'。"""
    ns = _PARTICLE_RE.sub("", str(name))
    return bool(sector) and (sector in ns or ns in sector)


def compose_state(items: list[dict], watchlist: list[dict] | None = None) -> dict:
    """T1 盘前定级(纯消息):items(重要消息列表) → 状态卡载荷。

    - 利空高危任一 → 红; 利空中度任一 → 黄; 否则绿(利好/中性只计数)
    - 同日多消息利空优先取严,利好不抵消
    """
    watchlist = watchlist or []
    level = "green"
    hits_all: list[dict] = []
    bonus = {"利好": 0, "中性": 0}
    for it in items:
        text = str(it.get("text") or "")
        grade = str(it.get("source_grade") or "快讯")
        if grade == "传闻":
            continue
        item_hits = match_text(text)
        for h in item_hits:
            if h["polarity"] == "利空":
                hits_all.append({**h, "source_grade": grade, "text": text,
                                 "sector": str(it.get("sector") or "")})
            elif h["polarity"] == "利好":
                bonus["利好"] += 1
            else:
                bonus["中性"] += 1
    if any(h["severity"] == "high" for h in hits_all):
        level = "red"
    elif any(h["severity"] == "medium" for h in hits_all):
        level = "yellow"
    st = STATE_TEXT[level]
    return {
        "level": level,
        "name": st["name"],
        "hint": st["hint"],
        "evidence": _evidence_lines(hits_all),
        "hits": hits_all,
        "bonus": bonus,
        "per_symbol": _per_symbol_labels(watchlist, items),
        "at": _now_hhmm(),
        "tags": [_tag_for(str(it.get("text") or "")) for it in items],
    }