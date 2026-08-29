# -*- coding: utf-8 -*-
"""规则不变量断言(合成矩阵用):无论什么形态,档位/场景必须满足的性质。

每条不变量对应《交易系统设计》一条规则(§6.1.4 校验七条 / 止损 v2.8 /
场景词表白名单)。返回违反列表(空 = 全过)。任何违反 = 规则漏洞,须补洞。
"""
from __future__ import annotations

from alphaprism.planner.intraday_engine import (
    STATE_WORDS, BAND_TIGHT_MIN, BAND_TIGHT_ATR,
)

# 场景词永远 ∈ 白名单(单日截面禁"右侧确认"等,§1)
VALID_SCENARIOS = {"趋势", "区间震荡", "变盘前兆", "突破异动日", "破位", "数据不足"}
VALID_STATE_WORDS = STATE_WORDS | {"数据不足"}

# 止损位源白名单(止损类=近20日低/阶段低;兜底允许出现支撑类位源)
ALLOWED_STOP_SRC = {
    "近20日低", "阶段低",
    "日线M5", "日线M10", "日线M20", "日线M60",
    "30分M5", "30分M10", "30分M20",
    "30分区间低", "日内低", "白名单",
}

# 止损档距现价上限(观察阈值;想收紧 → 改这里,契约注释见 交易系统设计 §6.1 校准)
STOP_MAX_DISTANCE_PCT = 0.25


def check_state_invariants(state: dict) -> list[str]:
    """场景/状态词白名单(§1/§8.1):状态词必须 ∈ 白名单。"""
    out = []
    if not state:
        return ["state 为空"]
    sc = state.get("scenario")
    sw = state.get("state_word")
    if sc not in VALID_SCENARIOS:
        out.append(f"场景 '{sc}' 不在白名单 {sorted(VALID_SCENARIOS)}")
    if sw not in VALID_STATE_WORDS:
        out.append(f"状态词 '{sw}' 不在白名单 {sorted(VALID_STATE_WORDS)}")
    # 场景×状态词一致性:破位场景必须破位退出;区间震荡不得输出右侧词
    if sc == "破位" and sw != "破位退出":
        out.append(f"破位场景状态词应为'破位退出',得 '{sw}'")
    if sc == "区间震荡" and sw not in ("区间震荡",):
        out.append(f"区间震荡状态词应为'区间震荡',得 '{sw}'")
    if sw == "右侧确认":
        out.append("单日截面不得输出'右侧确认'")
    return out


def check_anchors_invariants(facts: dict, anchors: dict) -> list[str]:
    """档位不变量(§6.1.4 校验七条 + 止损 v2.8 距离约束)。"""
    out = []
    if not anchors:
        return ["anchors 为空"]
    price = anchors.get("anchor_price")
    ok = anchors.get("ok")
    if not ok:
        # 档位不完整必须带 reason(可复核);否则是静默失败
        if not anchors.get("reason"):
            out.append("ok=False 但无 reason(静默失败)")
        return out
    if price is None:
        return ["ok=True 但 anchor_price 为空"]
    breakout = anchors.get("breakout_add")
    cut = anchors.get("cut_loss")
    stop = anchors.get("stop_loss")
    zone = anchors.get("pullback_zone")
    no_pb = bool(anchors.get("no_pullback"))
    # 不全则跳过存在性断言(数据真的不够时 ok 应为 False,故到达此处应齐备)
    for label, v in (("突破", breakout), ("减仓", cut), ("止损", stop)):
        if v is None:
            out.append(f"ok=True 但 {label} 档为空")
    if breakout is not None and not (breakout > price):
        out.append(f"breaks 突破档 {breakout} 未高于现价 {price}")
    if cut is not None and cut > price + 1e-9:
        out.append(f"减仓档 {cut} 高于现价 {price}")
    if stop is not None and cut is not None and not (stop < cut - 1e-9):
        out.append(f"止损 {stop} 未低于减仓 {cut}(校验④)")
    # 止损 v2.8:止损距现价上限(避免 250日极值远距止损回归)
    if stop is not None and (price - stop) / price > STOP_MAX_DISTANCE_PCT:
        out.append(f"止损 {stop} 距现价 {price} 超 {STOP_MAX_DISTANCE_PCT*100:.0f}%"
                   f"(允许兜底/降级)")
    # 止损位源白名单
    src = anchors.get("stop_src") or ""
    if stop is not None and src not in ALLOWED_STOP_SRC:
        out.append(f"止损位源 '{src}' 不在白名单")
    # 回踩带存在性 ↔ no_pullback 一致性
    if no_pb and zone is not None:
        out.append("no_pullback=True 但 pullback_zone 存在")
    if not no_pb and zone is None:
        out.append("no_pullback=False 但 pullback_zone 缺失")
    # 回踩带上沿 ≤ 现价(§2.4 触发≠校验;校验看静态成立)
    if zone is not None:
        if zone.get("upper", 0) > price + 1e-9:
            out.append(f"回踩带上沿 {zone['upper']} 高于现价 {price}")
        if zone.get("lower", 0) > zone.get("upper", 0) + 1e-9:
            out.append("回踩带下沿高于上沿")
        if cut is not None and abs(zone.get("lower", 0) - cut) > 1e-9:
            out.append("减仓档 != 回踩带下沿(v2.8 cut 语义)")
    return out


def check_zone_tight(facts: dict, zone: dict) -> list[str]:
    """回踩带'已到位/带内'阈值一致性(§6.1.3):tight 必须与规则定义一致。"""
    out = []
    if not zone:
        return out
    upper = zone.get("upper")
    price = (facts.get("etf") or {}).get("minute", {}).get("price")
    atr20 = (facts.get("etf") or {}).get("daily", {}).get("atr20")
    if upper is None or price is None:
        return out
    tight_need = max(price * BAND_TIGHT_MIN / 100,
                     (BAND_TIGHT_ATR * atr20) if atr20 else 0)
    expected = bool(price - upper <= tight_need)
    if zone.get("tight") != expected:
        out.append(f"tight={zone.get('tight')} 与规则不符(应为 {expected},"
                   f"距上沿 {price - upper:.4f} vs 阈值 {tight_need:.4f})")
    return out


def check_all(facts: dict, state: dict, anchors: dict) -> list[str]:
    """汇总全部不变量(结构问题 + 场景词 + 档位 + tight)。"""
    out = check_state_invariants(state)
    out += check_anchors_invariants(facts, anchors)
    out += check_zone_tight(facts, anchors.get("pullback_zone") or {})
    return out


def check_sanitize_case(case: dict, fn) -> list[str]:
    """sanitize 注入用例校验:分析文本经 fn(sanitize_v2)后,必须满足
    must_keep 全保留 / must_remove 全删除 / expect_degraded 一致。

    fn: sanitize_v2 签名的(analysis, snapshot, state, anchors) 调用。
    """
    out = []
    analysis = case["analysis"]
    snapshot = case.get("snapshot", "")
    state = case.get("state", {})
    anchors = case.get("anchors", {})
    try:
        text, degraded, _issues = fn(analysis, snapshot, state, anchors)
    except Exception as exc:  # noqa: BLE001
        return [f"sanitize_v2 抛出异常: {type(exc).__name__}: {exc}"]
    for tok in case.get("must_keep", []):
        if tok not in text:
            out.append(f"该保留但被删/改写: '{tok}'")
    for tok in case.get("must_remove", []) or []:
        if tok in text:
            out.append(f"该删除但仍存在: '{tok}'")
    if case.get("expect_degraded") and not degraded:
        out.append("预期降级(degraded=True)但未降级")
    if case.get("expect_degraded") is False and degraded:
        out.append("预期不降级但 degraded=True")
    return out