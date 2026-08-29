"""结论卡 schema(报道结论层结构化,2026-08-27)。

对齐 daily_stock_analysis 的 report_schema 思路:把「结论」从自由散文变成
必填结构化字段,防注水从"规则补丁"变"结构杜绝";同时让 verifier 准则、
盘后增量4 直接吃结构化字段。

结论卡 JSON(LLM 在报告末尾输出):
{
  "conclusion": {
    "one_sentence":   方向+核心判断一句话(含因/但,非复述状态词),
    "signal_type":    白名单一致:见 SIGNAL_TYPES,
    "time_sensitivity": "盘中|当日收盘|跨日",
    "priority":       多周期优先级一句(日线MACD定方向/30分KDJ定节奏),
    "trigger":        {"verb": 回踩|跌破|放量站上|站上|企稳,
                        "price_level": "0.836-0.837(回踩带)",
                        "confirm": "放量|日线收盘|30分K收盘|企稳信号",
                        "action": "减仓|清仓止损|试多候选|加仓候选"},
    "invalidation":   {"verb": "跌破", "price_level": "0.836",
                        "source": "回踩带下沿", "action": "减仓"}
  }
}

容错:免费档 JSON 不稳 → 容忍解析;校验不过 → 程序兜底(确定性字段生成)。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

SIGNAL_TYPES = frozenset({"观望", "试多候选", "右侧初现候选", "蓄势候选",
                          "减仓参考", "破位退出", "清仓止损"})
TIME_SENSITIVITY = frozenset({"盘中", "当日收盘", "跨日"})
CONFIRM_WORDS = ("放量", "日线收盘", "30分K收盘", "收盘", "企稳", "次日", "金叉")
TRIGGER_VERBS_DOWN = ("回踩", "跌破", "企稳")
TRIGGER_VERBS_UP = ("放量站上", "站上", "突破")
TRIGGER_ACTIONS = frozenset({"加仓候选", "试多候选", "减仓", "清仓止损", "右侧初现候选"})
INVALIDATION_ACTIONS = frozenset({"减仓", "清仓止损"})


def locate_conclusion(text: str) -> tuple[int, int] | None:
    """定位含 'one_sentence' 的最外层 JSON 块 [start, end);找不到 → None。"""
    idx = text.find("one_sentence") if text else -1
    if idx < 0:
        return None
    # 优先定位标准 wrapper {"conclusion":{...}};否则退回最外层平衡块扫描
    start = None
    marker = '{"conclusion"'
    m = text.rfind(marker, 0, idx)
    if m >= 0:
        start = m
    else:
        depth = 0
        for i in range(idx, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                if depth > 0:
                    depth -= 1
                else:
                    start = i            # 最左"深度0"的 { = 最外层
    if start is None:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return None


def _extract_conclusion_json(text: str) -> dict | None:
    """从文本中提取含 'one_sentence' 的最外层 {} 并解析(容忍 ```json 包裹/多余文字)。"""
    loc = locate_conclusion(text)
    if loc is None:
        return None
    try:
        js = json.loads(text[loc[0]:loc[1]])
    except json.JSONDecodeError as exc:
        logger.warning("结论卡 JSON 解析失败: %s", str(exc)[:120])
        return None
    return js if isinstance(js, dict) else None


def _as_str(v, default: str = "") -> str:
    return str(v).strip() if isinstance(v, (str, int, float)) else default


def normalize_conclusion(text: str) -> dict | None:
    """从 LLM 报告文本提取并规整结论卡;无 one_sentence → None。"""
    js = _extract_conclusion_json(text)
    if not js:
        return None
    c = js.get("conclusion") if isinstance(js.get("conclusion"), dict) else js
    if not isinstance(c, dict):
        return None
    out = {
        "one_sentence": _as_str(c.get("one_sentence")),
        "signal_type": _as_str(c.get("signal_type")),
        "time_sensitivity": _as_str(c.get("time_sensitivity"), "当日收盘"),
        "priority": _as_str(c.get("priority")),
        "trigger": {},
        "invalidation": {},
    }
    for key in ("trigger", "invalidation"):
        sub = c.get(key)
        if isinstance(sub, dict):
            out[key] = {k: _as_str(v) for k, v in sub.items()
                        if isinstance(v, (str, int, float)) and str(v).strip()}
    if not out["one_sentence"] or not out["signal_type"]:
        return None
    return out


def _allowed_prices(snapshot_md: str, anchors: dict) -> set[str]:
    allowed: set[str] = set()
    for line in snapshot_md.splitlines():
        if not (line.startswith("| 盘口 |") or line.startswith("| 资金 |")):
            allowed |= set(re.findall(r"\d+\.\d{3}(?!\d)", line))
    for v in (anchors.get("anchor_price"), anchors.get("breakout_add"),
              anchors.get("pullback_add"), anchors.get("cut_loss"),
              anchors.get("stop_loss")):
        if v is not None:
            allowed.add(f"{float(v):.3f}")
    return allowed


def validate_conclusion(c: dict, snapshot_md: str, anchors: dict,
                        anchor_price: float | None = None,
                        state: dict | None = None) -> tuple[bool, list[str]]:
    """结构化校验(比散文校验更强):白名单 + 价位∈白名单 + 动词方向 + 语义约束。

    state(2026-08-29):signal_type 必须与状态词映射一致(左侧观望→观望,
    超跌试多→试多候选…),LLM 越界 → 校验失败,调用方走程序兜底。
    """
    issues: list[str] = []
    if c.get("signal_type") not in SIGNAL_TYPES:
        issues.append(f"signal_type 越界:{c.get('signal_type')}")
    # 状态词 → signal_type 一致性(程序兜底映射,LLM 不得越级)
    if state:
        st = str(state.get("state_word") or "")
        expected = {
            "左侧观望": "观望", "破位退出": "破位退出", "超跌试多": "试多候选",
            "右侧初现": "右侧初现候选", "蓄势": "蓄势候选",
            "变盘前兆": "蓄势候选", "区间震荡": "观望",
        }.get(st)
        if expected and str(c.get("signal_type")) != expected:
            issues.append(f"signal_type '{c.get('signal_type')}' 与状态词 '{st}' 不一致"
                          f"(应为 '{expected}')")
    if c.get("time_sensitivity") not in TIME_SENSITIVITY:
        issues.append(f"time_sensitivity 越界:{c.get('time_sensitivity')}")
    if len(c.get("one_sentence", "")) < 8 or "市场状态为" in c.get("one_sentence", ""):
        issues.append("one_sentence 过短或复述状态词(结论注水)")
    if not c.get("priority"):
        issues.append("priority 缺多周期优先级判定")
    allowed = _allowed_prices(snapshot_md, anchors)
    trig, inv = c.get("trigger", {}), c.get("invalidation", {})
    if not trig.get("verb") or not trig.get("price_level") or not trig.get("action"):
        issues.append("trigger 缺 verb/price_level/action")
    if not inv.get("verb") or not inv.get("price_level") or not inv.get("source"):
        issues.append("invalidation 缺 verb/price_level/source")
    for label, sub in (("trigger", trig), ("invalidation", inv)):
        pl = _as_str(sub.get("price_level"))
        m = re.search(r"(\d+\.\d{3})", pl)
        if m and m.group(1) not in allowed:
            issues.append(f"{label}.price_level 不在白名单:{m.group(1)}")
        elif pl and re.search(r"\d", pl) and m is None:
            issues.append(f"{label}.price_level 必须 3 位小数:{pl[:20]}")
        # v2.10:区间内所有数字都须 3 位小数(0.822-0.84 → 0.84 违规)
        nums = re.findall(r"\d+\.\d+", pl)
        if nums and any(len(n.split(".")[1]) != 3 for n in nums):
            issues.append(f"{label}.price_level 含非 3 位小数:{pl[:20]}")
    # action 白名单(v2.10):触发必须引向动作化结论,禁止"待观望/观望"类弱结论
    if trig.get("action") and trig["action"] not in TRIGGER_ACTIONS:
        issues.append(f"trigger.action 越界:{trig['action'][:20]}")
    if inv.get("action") and inv["action"] not in INVALIDATION_ACTIONS:
        issues.append(f"invalidation.action 越界:{inv['action'][:20]}")
    # 动词方向 vs 锚定价(下方=回踩/跌破/企稳,上方=放量站上/站上/突破)
    if anchor_price is not None:
        for label, sub in (("trigger", trig), ("invalidation", inv)):
            verb = _as_str(sub.get("verb"))
            m = re.search(r"(\d+\.\d{3})", _as_str(sub.get("price_level")))
            if m and verb:
                p = float(m.group(1))
                is_down = any(v in verb for v in TRIGGER_VERBS_DOWN)
                is_up = any(v in verb for v in TRIGGER_VERBS_UP)
                if (is_down and p > anchor_price) or (is_up and p < anchor_price):
                    issues.append(f"{label} 动词方向与现价矛盾:{verb} {m.group(1)}")
    return not issues, issues


def fallback_conclusion(state: dict, anchors: dict) -> dict:
    """LLM 结论卡缺失/无效时,程序用确定性字段兜底(零 LLM)。"""
    state_word = state.get("state_word") or ""
    signal_map = {
        "破位退出": "破位退出", "左侧观望": "观望", "超跌试多": "试多候选",
        "右侧初现": "右侧初现候选", "蓄势": "蓄势候选", "区间震荡": "观望",
        "变盘前兆": "蓄势候选",
    }
    signal = signal_map.get(state_word, "观望")
    anchor = anchors.get("anchor_price")
    zone = anchors.get("pullback_zone")
    cut, cut_src = anchors.get("cut_loss"), anchors.get("cut_src") or "减仓档"
    stop, stop_src = anchors.get("stop_loss"), anchors.get("stop_src") or "清仓止损档"
    breakout, b_src = anchors.get("breakout_add"), anchors.get("breakout_src") or "突破档"
    if zone:
        trigger = {"verb": "回踩",
                   "price_level": f"{float(zone.get('lower')):.3f}-{float(zone.get('upper')):.3f}",
                   "confirm": "企稳信号", "action": "加仓候选"}
    elif breakout:
        trigger = {"verb": "放量站上", "price_level": f"{float(breakout):.3f}",
                   "confirm": "放量+日线收盘", "action": "右侧初现候选"}
    else:
        trigger = {}
    invalidation = {"verb": "跌破", "price_level": f"{float(cut):.3f}", "source": cut_src,
                    "action": "减仓"} if cut else {}
    one_sentence = (f"现价 {float(anchor):.3f} 处于 {state_word} 状态"
                    + (f",回踩带 {float(zone.get('lower')):.3f}-{float(zone.get('upper')):.3f}"
                       + (" 内,企稳观察" if zone.get("in_zone")
                          else " 上方附近,尚未跌破,回踩带观察中" if zone.get("tight")
                          else " 下方未到,等待回踩")
                       if zone else ",等待企稳/恐慌证据")
                    + (f";跌破 {float(stop):.3f} 清仓止损" if stop else ""))
    return {
        "one_sentence": one_sentence,
        "signal_type": signal,
        "time_sensitivity": "当日收盘",
        "priority": "程序兜底:日线MACD定方向、30分KDJ定节奏(详见盘面状态/程序标注)",
        "trigger": trigger,
        "invalidation": invalidation,
    }


def program_priority_sentence(snapshot_md: str) -> str:
    """程序多周期优先级句(确定性,verifier priority 维度的事实依据)。

    从快照程序标注解析:日线 MACD 零轴语境(零上/零下/穿越)定方向;
    30分 KDJ 状态(超买/超卖/中性)定节奏;按解读词典给出环境细分句。
    解析失败 → 通用句(宁可保守不编)。
    """
    etf_part = snapshot_md.split("## 大盘")[0]
    zero = None
    m = re.search(r"\|\s*日线\s*\|\s*MACD[^\n]*?\((?:[^)]*?(零上|零下|穿越))[^)]*\)", etf_part)
    if m:
        zero = m.group(1)
    kdj30 = None
    m = re.search(r"\|\s*30分\s*\|\s*KDJ[^\n]*?\((超买|超卖|中性)", etf_part)
    if m:
        kdj30 = m.group(1)
    if not zero or not kdj30:
        return "日线MACD定方向、30分KDJ定节奏(详见盘面状态程序标注)"
    direction = {"零上": "零上(多头环境)", "零下": "零下(空头环境)", "穿越": "零轴附近(方向未明)"}[zero]
    rhythm = f"30分KDJ定节奏:{kdj30}"
    env = {"零下": "零下空头环境下,30分 超买=回调压力、30分 超卖不必然是买点(可能下跌中继)",
           "零上": "零上多头环境下,30分 超卖=低吸机会、30分 超买=高位保护",
           "穿越": "零轴附近,KDJ 信号弱化,以观望为主"}[zero]
    return f"日线MACD定方向:{direction};{rhythm};{env}"


def conclusion_card_prompt(snapshot_md: str, anchors: dict, state: dict,
                           prior_evidence: list[tuple[str, str]] | None = None) -> str:
    """结论卡先行生成 prompt(步骤2):独立小调用,单一 JSON 结构,免费档服从率高。"""
    zone = anchors.get("pullback_zone")
    zb = (f"回踩带 {zone.get('lower')}-{zone.get('upper')}({zone.get('upper_src') or ''})"
          if zone else "无回踩结构")
    prio = program_priority_sentence(snapshot_md)
    evidence_note = ""
    if prior_evidence:
        lines = "\n".join(f"- {d}: {ev[:100]}" for d, ev in prior_evidence)
        evidence_note = ("【该标的近期评审缺陷提醒】(程序评审历史指出的问题,本卡必须主动避免重犯):\n"
                         + lines + "\n")
    return (
        "你是结论卡生成器。输入【事实卡片(确定性)】与【操作参数】,只输出一个 JSON 结论卡,"
        "禁止任何注释/其他文字:\n"
        '{"conclusion":{"one_sentence":"方向+核心判断一句话(含因/但,禁止复述状态词)",'
        '"signal_type":"观望|试多候选|右侧初现候选|蓄势候选|减仓参考|破位退出|清仓止损",'
        '"time_sensitivity":"盘中|当日收盘|跨日","priority":"多周期优先级一句",'
        '"trigger":{"verb":"回踩|跌破|放量站上|站上|企稳",'
        '"price_level":"0.836-0.837(回踩带)","confirm":"放量|日线收盘|30分K收盘|企稳信号",'
        '"action":"减仓|清仓止损|试多候选|加仓候选"},'
        '"invalidation":{"verb":"跌破","price_level":"0.836","source":"回踩带下沿","action":"减仓"}}}\n'
        "纪律:① 价位只从操作参数/关键位;② trigger/invalidation 动词方向与现价自洽"
        "(下方=回踩/跌破/企稳,上方=放量站上/站上/突破);"
        f"③ 状态词={state.get('state_word') or '?'} → signal_type 必须一致"
        "(左侧观望→观望,超跌试多→试多候选,右侧初现→右侧初现候选,蓄势→蓄势候选,"
        "破位退出→破位退出);④ one_sentence 必须给出方向判断('因…但…'),禁止只描述状态;"
        f"⑤ priority 字段必须照抄下列【程序多周期优先级句】并按其展开:\n"
        f"【程序多周期优先级句】{prio}\n\n"
        "⑥ 所有价位必须 3 位小数(如 0.836,禁止 0.84/0.8),且来自上面【操作参数】;"
        "trigger.action 必须 ∈ 加仓候选|试多候选|减仓|清仓止损|右侧初现候选;"
        "invalidation.action 必须 ∈ 减仓|清仓止损;触发需引向动作化结论,禁止'观望'作 action。\n\n"
        + evidence_note
        + "【事实卡片】\n" + snapshot_md +
        f"\n\n【操作参数】突破 {anchors.get('breakout_add')} / {zb}"
        f" / 减仓 跌破 {anchors.get('cut_loss')} / 清仓止损 {anchors.get('stop_loss')}\n\n"
        "输出 JSON:"
    )


def generate_conclusion_card(snapshot_md: str, anchors: dict, state: dict, cfg,
                             chat_call=None,
                             prior_evidence: list[tuple[str, str]] | None = None) -> tuple[dict, str]:
    """结论卡先行:LLM 卡(校验通过)否则程序兜底。返回 (card, source='llm'|'fallback')。"""
    import sys
    sys.path.insert(0, ".")
    from ..llm import chat as _default_chat

    chat = chat_call or _default_chat
    try:
        text = chat([{"role": "user",
                      "content": conclusion_card_prompt(snapshot_md, anchors, state,
                                                        prior_evidence=prior_evidence)}],
                    cfg, temperature=0.2, max_tokens=600, timeout=120) or ""
        card = normalize_conclusion(text) if text else None
        if card:
            ok, issues = validate_conclusion(card, snapshot_md, anchors,
                                             anchors.get("anchor_price"), state)
            if ok:
                return card, "llm"
            logger.warning("结论卡先行校验不过(%s),走兜底: %s", "; ".join(issues)[:160], text[:120])
    except Exception as exc:  # noqa: BLE001
        logger.warning("结论卡先行生成失败: %s", exc)
    return fallback_conclusion(state, anchors), "fallback"


def render_conclusion_card(c: dict) -> str:
    """结论卡 → markdown(渲染成段八之后的机器可核验结论块)。"""
    t, iv = c.get("trigger", {}), c.get("invalidation", {})
    lines = ["### 📋 结论卡(结构化·程序校验)"]
    lines.append(f"- 核心判断: {c.get('one_sentence', '-')}")
    lines.append(f"- 信号类型: {c.get('signal_type', '-')} · 时效: {c.get('time_sensitivity', '-')}")
    lines.append(f"- 多周期优先级: {c.get('priority', '-')}")
    if t:
        lines.append(f"- 触发: {t.get('verb', '?')} {t.get('price_level', '?')}"
                     f"({t.get('confirm', '?')}) → {t.get('action', '?')}")
    if iv:
        lines.append(f"- 失效: {iv.get('verb', '?')} {iv.get('price_level', '?')}"
                     f"({iv.get('source', '?')}) → {iv.get('action', '?')}")
    return "\n".join(lines)