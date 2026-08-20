"""盘中快照(产品方案 §5.4,里程碑3 延伸):规则事实 + LLM 解读 → 差异输出。

规则驱动:引擎用速查表 levels + 实时行情算事实(结论词5档 + 大盘门控),
LLM 只做解读与措辞;LLM 不可用 → 降级为纯规则事实(不编造)。
差异输出:有动作的标的重点写,平静的一行带过。

输出 markdown,供悬浮面板/Web 盘中 tab / 推手机共用。
"""
from __future__ import annotations

import logging
from datetime import datetime

from .checker import CONCLUSION_STYLE  # noqa: F401
from .rulemodel import RuleModel

logger = logging.getLogger(__name__)

_ACTIVE = ("买点触发", "接近买点", "接近卖点", "破位")


def _fmt_pct(x) -> str:
    if x is None:
        return "-"
    return f"{'+' if x > 0 else ''}{x:.2f}%"


def _fmt_price(x) -> str:
    return f"{x:.3f}" if x is not None else "-"


def _rule_takeaway(verdicts: list[dict], gate: dict, discipline: list[str]) -> str:
    """规则事实层:大盘 → 现状表 → 差异 → 接下来只看一件事 → 纪律。"""
    gate_txt = ("大盘门控开放" if gate.get("open")
                else f"大盘门控关闭·不加仓(J={gate.get('j') or '-'})")
    lines = [f"**大盘**: {gate_txt}", "", "**现状**:", ""]
    active = [v for v in verdicts if v["conclusion"] in _ACTIVE]
    wait = [v for v in verdicts if v["conclusion"] == "等待·缺条件"]
    for v in verdicts:
        near = f"({v['near']})" if v.get("near") else ""
        vol = f"{v.get('vol_label') or ''}".strip()
        lines.append(f"- {v['name']} {_fmt_price(v['price'])} {_fmt_pct(v['change_pct'])}"
                     f" {vol} → **{v['conclusion']}**{near}")
    lines += ["", "**差异重点**:", ""]
    if active:
        for v in active[:3]:
            lines.append(f"- 🔴 {v['name']}: {v['conclusion']} {v['near']}")
        lines.append(f"- 其余 {len(verdicts) - len(active)} 只平静/等待,不动")
    else:
        lines.append("- 无触发,全部观望")
    lines += ["", "**接下来只看一件事**:", ""]
    if active:
        top = active[0]
        lines.append(f"{top['name']} {top['conclusion']} {top['near']} —— 按作战地图执行")
    elif wait:
        lines.append(f"{wait[0]['name']} 等信号(缩量企稳/放量突破),不追高")
    else:
        lines.append("保持观察,大盘总闸门未开前不加仓")
    if discipline:
        lines += ["", "**纪律**:", f"- {' | '.join(discipline[:3])}"]
    return "\n".join(lines)


def _llm_takeaway(verdicts: list[dict], gate: dict, cfg) -> str | None:
    """LLM 把事实改写成一句话解读。失败返回 None(调用方降级规则)。"""
    from ..llm import chat

    rows = "\n".join(
        f"- {v['name']} {_fmt_price(v['price'])} {_fmt_pct(v['change_pct'])} "
        f"{v.get('vol_label') or ''} → {v['conclusion']}" + (f" {v['near']}" if v.get("near") else "")
        for v in verdicts)
    prompt = (
        "你是 A股中长线交易系统的盘中快照助手。基于以下核对事实,写 2-3 句解读:"
        "先说大盘门控结论,再说 1-3 个有动作标的(引用结论词),最后一句给操作纪律提醒。"
        "只输出解读,不编造数据。\n\n"
        f"大盘门控: {'开放' if gate.get('open') else '关闭'}(J={gate.get('j') or '-'})\n"
        f"核对结果:\n{rows}"
    )
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg,
                    temperature=0.3, max_tokens=300)
        return text.strip() if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("快照 LLM 解读失败: %s", exc)
        return None


def build_snapshot(model: RuleModel, check_result: dict, cfg=None,
                   now: datetime | None = None) -> str:
    """生成盘中快照 markdown(规则事实 + LLM 解读)。"""
    now = now or datetime.now()
    gate = check_result.get("gate") or {}
    verdicts = check_result.get("verdicts") or []
    llm = _llm_takeaway(verdicts, gate, cfg)
    lines = [f"# 盘中快照 {now:%H:%M}"]
    if llm:
        lines += ["", f"> {llm}", ""]
    lines.append(_rule_takeaway(verdicts, gate, model.global_.discipline))
    lines.append("")
    return "\n".join(lines)