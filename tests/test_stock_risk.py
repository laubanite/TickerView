# -*- coding: utf-8 -*-
"""个股风险状态机单测(方案 §7-P2:纯函数全覆盖 + 结构性"无建议"守卫)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alphaprism.fetchers.stock_qt import stock_type  # noqa: E402
from alphaprism.planner.stock_risk import (  # noqa: E402
    DISCLAIMER, evaluate_stock_risk, render_stock_lines)


def _ctx(**kw) -> dict:
    base = {"symbol": "300308", "name": "测试股", "price": 10.0, "pct_chg": 0.5,
            "m20": 9.0, "m60": 8.5, "vol_ratio": 1.0, "low20_close": 8.8,
            "low250_close": 7.0, "peak250": 12.0, "atr20_pct": 3.0,
            "consec_down": 0, "cost": None, "suspended": False,
            "at_limit_up": False, "at_limit_down": False}
    base.update(kw)
    return base


def _ids(result: dict) -> list[str]:
    return [r["id"] for r in result["rules"]]


# ---- R2 [已验证] 破位:量比 1.5 边界 ----
def test_r2_break_with_volume_hits():
    r = evaluate_stock_risk(_ctx(price=8.9, vol_ratio=1.5))
    assert "R2" in _ids(r) and r["level"] == "风险"


def test_r2_no_volume_no_hit():
    r = evaluate_stock_risk(_ctx(price=8.9, vol_ratio=1.49))
    assert "R2" not in _ids(r)


def test_r2_above_m20_no_hit():
    assert "R2" not in _ids(evaluate_stock_risk(_ctx(price=9.1, vol_ratio=2.0)))


# ---- R3/R4 [已验证] 退出阶梯 ----
def test_r3_below_20d_low():
    r = evaluate_stock_risk(_ctx(price=8.7))
    assert "R3" in _ids(r) and r["level"] == "风险"


def test_r4_below_250d_low_is_severe():
    r = evaluate_stock_risk(_ctx(price=6.9))
    assert "R4" in _ids(r) and r["level"] == "严重"


# ---- R5 成本风控:-8% 边界 ----
def test_r5_cost_minus8_boundary():
    r = evaluate_stock_risk(_ctx(price=9.2, cost=10.0))  # -8.0% → 命中风险档
    assert "R5" in _ids(r) and r["level"] == "风险"


def test_r5_breakeven_watch():
    r = evaluate_stock_risk(_ctx(price=9.99, cost=10.0))
    assert "R5" in _ids(r) and r["level"] == "关注"


def test_r5_above_cost_no_hit():
    assert "R5" not in _ids(evaluate_stock_risk(_ctx(price=10.5, cost=10.0)))


# ---- v3 自适应层(2026-09-04 用户拍板):R6/R7 固定阈值退役 ----
def test_r10_weak_count_levels():
    # 唐奇安破位计数:≥2 关注 / ≥3 风险 / 1 次不触发
    r1 = evaluate_stock_risk(_ctx(weak_count=1))
    assert "R10" not in _ids(r1)
    r2 = evaluate_stock_risk(_ctx(weak_count=2))
    assert "R10" in _ids(r2) and r2["level"] == "关注"
    r3 = evaluate_stock_risk(_ctx(weak_count=3))
    assert "R10" in _ids(r3) and r3["level"] == "风险"


def test_r11_volatility_regime_percentile():
    # 自身分位制:≥90 → 关注;<90 不触发;无分位时固定阈值兜底
    r = evaluate_stock_risk(_ctx(atr20_pct=6.0, atr_pctile=95))
    assert "R11" in _ids(r) and r["level"] == "关注"
    assert "R11" not in _ids(evaluate_stock_risk(_ctx(atr20_pct=6.0, atr_pctile=50)))
    r2 = evaluate_stock_risk(_ctx(atr20_pct=6.0, atr_pctile=None))
    assert "R11" in _ids(r2)  # 历史不足 → 固定阈值兜底


def test_r12_trend_structure():
    # 空头排列 + 价格在20日线下 + 250日区间下四分位 → 关注;分位高/多头不触发
    bear = _ctx(price=8.9, m5=8.0, m20=9.0, m60=9.5, p250_pos=0.10)
    assert "R12" in _ids(evaluate_stock_risk(bear))
    high_pos = _ctx(price=8.9, m5=8.0, m20=9.0, m60=9.5, p250_pos=0.60)
    assert "R12" not in _ids(evaluate_stock_risk(high_pos))
    bull = _ctx(price=8.9, m5=9.2, m20=9.0, m60=8.5, p250_pos=0.10)
    assert "R12" not in _ids(evaluate_stock_risk(bull))


def test_suspended_guards_all_rules():
    # v3 停牌守卫:陈旧价不作任何趋势/切位判定
    r = evaluate_stock_risk(_ctx(suspended=True, price=6.9))
    assert r["rules"] == [] and r["level"] == "正常"


# ---- R8/R9 事件与结构状态 ----
def test_r8_panic_events_only():
    r = evaluate_stock_risk(_ctx(pct_chg=-3.5, vol_ratio=1.6, consec_down=2))
    assert r["rules"] == [] and len(r["events"]) == 2


def test_r9_limit_down_risk_limit_up_event():
    r = evaluate_stock_risk(_ctx(at_limit_down=True, at_limit_up=False))
    assert "R9" in _ids(r)
    r2 = evaluate_stock_risk(_ctx(at_limit_up=True))
    assert "R9" not in _ids(r2) and any("涨停" in e for e in r2["events"])


def test_r9_suspended_event_only():
    r = evaluate_stock_risk(_ctx(suspended=True))
    assert r["rules"] == [] and any("停牌" in e for e in r["events"])


# ---- 档位聚合与空态 ----
def test_level_takes_max():
    r = evaluate_stock_risk(_ctx(price=6.9, atr20_pct=7.0))  # R4 严重 + R7 关注
    assert r["level"] == "严重" and r["risk_level_panel"] == "升级"


def test_empty_price_safe():
    r = evaluate_stock_risk(_ctx(price=None))
    assert r["level"] == "正常"


# ---- v4 深入分析增量层(2026-09-04 深夜 II:多周期/证据天平/情景推演) ----
def test_classify_day_path():
    from alphaprism.planner.stock_risk import _classify_day_path
    # 高开冲高后回落收在低点 → 冲高回落(300308 2026-09-04 实况形态)
    assert _classify_day_path(1.4, -1.3, -2.5, 0.7, 3.2) == "冲高回落"
    assert _classify_day_path(-1.2, 1.8, -0.2, 3.0, 3.5) == "探底回升"
    assert _classify_day_path(0.1, 2.0, -0.3, 2.0, 2.0) == "单边走高"
    assert _classify_day_path(-0.2, -2.2, -0.1, 0.3, 2.2) == "单边走低"
    assert _classify_day_path(0.0, 0.2, -1.0, 1.0, 4.0) == "宽幅震荡"
    assert _classify_day_path(0.0, 0.2, -1.0, 1.0, 1.0) == "窄幅整理"


def test_conflict_verdict_matrix():
    from alphaprism.planner.stock_risk import _conflict_verdict
    assert _conflict_verdict("bear", True)[0] == "反抽段"
    assert _conflict_verdict("bear", False)[0] == "共振走弱"
    assert _conflict_verdict("bull", True)[0] == "共振走强"
    assert _conflict_verdict("bull", False)[0] == "回调段"
    # 候选短语必须非空(弱模型只准选不准编)
    for kind in ("bear", "bull", "mixed"):
        for bull in (True, False):
            _, _, cands = _conflict_verdict(kind, bull)
            assert cands, (kind, bull)


def test_evidence_balance_sides():
    from alphaprism.planner.stock_risk import _evidence_balance
    # 空头+缩量反弹 → 走弱侧非空;企稳侧含止跌/缓冲
    weak, firm = _evidence_balance({"price": 8.9, "m5": 8.0, "m20": 9.0, "m60": 9.5,
                                    "vol_ratio": 0.68, "pct_chg": 0.12,
                                    "weak_count": 2, "low20_close": 8.8,
                                    "low250_close": 7.0, "consec_down": 2}, None)
    assert weak and firm
    assert any("持续走弱" in w for w in weak)
    assert any("止跌" in f for f in firm)


def test_scenarios_forward_mapping():
    from alphaprism.planner.stock_risk import _scenarios
    # 价在20日低上方 → 减仓/清仓两个前瞻分支
    scen = _scenarios({"price": 9.5, "m20": 10.0, "low20_close": 8.8,
                       "low250_close": 7.0, "cost": 10.0}, None)
    joined = ";".join(scen)
    assert "减仓位失守" in joined and "大位失守" in joined and "成本风控" in joined
    # v5:情景推演携带映射动作(减仓至半仓/清仓,与 §14 回测映射同源)
    assert "减仓至半仓" in joined and "清仓" in joined
    # 价跌破20日低后 → 回补分支(风险全解除)
    scen2 = _scenarios({"price": 8.5, "m20": 10.0, "low20_close": 8.8,
                        "low250_close": 7.0, "cost": 10.0}, None)
    joined2 = ";".join(scen2)
    assert "回补至满仓" in joined2
    # 映射之外的操作动词仍然禁止(规则中文名"减仓位失守"含"减仓"属专有名词,不在列)
    for w in ("加仓", "买入", "卖出", "抄底", "追高"):
        assert w not in joined and w not in joined2


def test_deep_skeleton_renders_and_no_codes():
    from alphaprism.planner.stock_risk import _deep_skeleton, evaluate_stock_risk
    import re
    ctx = {"price": 8.9, "pct_chg": 0.12, "m5": 8.0, "m20": 9.0, "m60": 9.5,
           "vol_ratio": 0.68, "low20_close": 8.8, "low250_close": 7.0,
           "peak250": 12.0, "p250_pos": 0.15, "weak_count": 2, "consec_down": 2,
           "atr20_pct": 4.0, "atr_pctile": 55, "rebound_pct": 10.0,
           "symbol": "300308", "name": "测试股", "cost": None}
    r = evaluate_stock_risk(ctx)
    m30 = {"day": "2026-09-04", "last": 8.9, "m5": 8.85, "m20": 8.7,
           "above_m20": True, "path": "冲高回落", "tail": "走弱",
           "open_gap": 1.4, "fade": -2.5, "recover": 0.7,
           "day_high": 9.1, "day_low": 8.8, "day_open": 9.0}
    md = _deep_skeleton(ctx, r, m30)
    for sec in ("多周期矛盾定性", "证据天平", "情景推演映射", "核心观察点"):
        assert sec in md, sec
    assert "反抽段" in md and "冲高回落" in md
    assert not re.search(r"\bR\d{1,2}\b", md), "深入骨架出现代号"
    # m30=None 降级路径
    md2 = _deep_skeleton(ctx, r, None)
    assert "30分层数据不可用" in md2


# ---- 一句话:结构性排除买卖建议 ----
FORBIDDEN = ("加仓", "买入", "卖出", "减仓", "清仓", "目标价", "止盈", "建议类别")


def test_sentence_has_no_advice_words():
    ctx = _ctx(cost=10.0, consec_down=2)
    line1, line2 = render_stock_lines(ctx, evaluate_stock_risk(ctx))
    for word in FORBIDDEN:
        assert word not in line1 and word not in line2, word


def test_sentence_none_price_safe():
    line1, _ = render_stock_lines(_ctx(price=None), evaluate_stock_risk(_ctx(price=None)))
    assert "无有效行情" in line1


def test_disclaimer_wording():
    # v5.1(用户要求缩短):长口径回测细节移出免责行,只保留定位句
    assert "风险监控" in DISCLAIMER and "不构成收益承诺" in DISCLAIMER
    assert "回撤改善" not in DISCLAIMER and "方案§14" not in DISCLAIMER
    assert "不构成买卖建议" not in DISCLAIMER


# ---- A 档风险解读:校验器 ----
def test_validator_clean_text_passes():
    from alphaprism.planner.stock_risk import _validate_stock_analysis
    # 观察档(价远离切位)无需动作锚点,干净叙事直接过
    ctx = _ctx(price=89.20, m20=87.87, peak250=1258.0, low250_close=31.26)
    ctx["recent5"] = [{"date": "2026-09-03", "close": 888.0, "pct_chg": 0.5}]
    result = evaluate_stock_risk(ctx)
    md = ("**风险等级:关注**\n\n当前位于 M20 上方 1.50%,距 250 日高点回撤 92.9%。\n"
          "近 5 个已收盘交易日整体震荡,250 日低点尚未触及。\n"
          "观察点:M20 位于 87.87,现价 89.20。")
    ok, why = _validate_stock_analysis(md, ctx, result)
    assert ok, why


def test_validator_bans_advice_words():
    from alphaprism.planner.stock_risk import _validate_stock_analysis
    ctx = _ctx()
    ok, why = _validate_stock_analysis("建议在 89.20 附近加仓。", ctx, None)
    assert not ok and "加仓" in why


def test_validator_catches_fabricated_price():
    from alphaprism.planner.stock_risk import _validate_stock_analysis
    ctx = _ctx(price=89.20, m20=87.87, low250_close=312.6)
    ok, why = _validate_stock_analysis("若跌破 76.50 则进入下一档。", ctx, None)
    assert not ok and "76.5" in why


def test_validator_integer_periods_not_flagged():
    from alphaprism.planner.stock_risk import _numbers_in_text
    # 整数周期词(M20/250日/近5日)不是价位,不进校验
    assert _numbers_in_text("M20 上方,近 250 日低点,近 5 日") == []


# ---- v5 直接建议(2026-09-05 回测放行):档位→动作映射 + 校验反转 ----
def test_advice_actions_mapping():
    from alphaprism.planner.stock_risk import _ADVICE_ACTIONS, _advice_word
    assert _ADVICE_ACTIONS["正常"][0] == "持有"
    assert _ADVICE_ACTIONS["关注"][0] == "警惕"
    assert _ADVICE_ACTIONS["风险"][0] == "减仓"
    assert _ADVICE_ACTIONS["严重"][0] == "清仓"
    # 回补是条件动作:仅昨日被减过仓(风险/严重)且今日风险全解除
    assert _advice_word("正常", None)[0] == "持有"
    assert _advice_word("正常", "正常")[0] == "持有"
    assert _advice_word("正常", "关注")[0] == "持有"
    assert _advice_word("正常", "风险") == ("回补", "风险全解除,按映射回补至满仓")
    assert _advice_word("正常", "严重") == ("回补", "风险全解除,按映射回补至半仓")


def test_validator_action_word_required_and_unique():
    from alphaprism.planner.stock_risk import _validate_stock_analysis
    # 风险档:结论必须含「程序建议:减仓」锚点;缺 → 拒
    ctx = _ctx(price=8.5, m20=9.0, low20_close=8.8)          # 破位 → 风险
    result = evaluate_stock_risk(ctx)
    assert result["level"] == "风险"
    ok, why = _validate_stock_analysis("趋势走弱,注意防守。", ctx, result)
    assert not ok and "程序建议:减仓" in why
    # 含锚点 → 过(减仓位失守专名在情景句中出现不误杀)
    ok2, why2 = _validate_stock_analysis(
        "证据天平倒向走弱侧,程序建议:减仓。\n若收盘跌破 7.0 触发大位失守,按映射清仓。",
        ctx, result)
    assert ok2, why2
    # 锚点被改成映射之外的动作 → 拒(缺今日锚点,或映射外动作结论,两者皆拦)
    ok3, why3 = _validate_stock_analysis("程序建议:清仓。", ctx, result)
    assert not ok3


def test_validator_observation_level_no_anchor_required():
    from alphaprism.planner.stock_risk import _validate_stock_analysis
    ctx = _ctx()                                             # 健康多头 → 正常/关注
    result = evaluate_stock_risk(ctx)
    ok, why = _validate_stock_analysis("证据均衡,继续观察。", ctx, result)
    assert ok, why


# ---- 类型判定 ----
def test_stock_type_prefix():
    assert stock_type("510300") == "etf"
    assert stock_type("159915") == "etf"
    assert stock_type("300308") == "stock"
    assert stock_type("600519") == "stock"
    assert stock_type("000001") == "stock"
    assert stock_type("837172") == "stock"


# ---- qt 解析(离线 fixture:2026-09-04 探针实测的中际旭创真实应答行) ----
def test_qt_line_parse_fixture():
    from alphaprism.fetchers.stock_qt import _parse_qt_reply

    raw = ('v_sz300308="51~中际旭创~300308~814.00~813.00~825.01~211067~116306~94760'
           '~814.00~40~813.99~1~813.98~3~813.90~9~813.88~3~814.12~3~814.53~1~814.80~3'
           '~814.81~1~814.85~5~~20260904161451~1.00~0.12~834.96~808.90'
           '~814.00/211067/17378183806~211067~1737818~1.90~46.88~~834.96~808.90~3.21'
           '~9034.89~9588.18~24.06~975.60~650.40~0.80~43~823.35~35.12~88.80~~2.37'
           '~1737818.3806~1343.1000~165~ A A~GP-A-CYB~33.66~-5.17~0.16~51.31~32.01'
           '~1416.88~336.00~-13.68~-11.51~-27.58~1109937003~1177909641~62.32~34.55'
           '~1109937003~~~121.35~-0.05~~~CNY~0~~815.00~-183~";')
    out = _parse_qt_reply(raw, {"300308"})
    s = out["300308"]
    assert s and s["name"] == "中际旭创"
    assert s["price"] == 814.00 and s["prev_close"] == 813.00
    assert s["limit_up"] == 975.60 and s["limit_down"] == 650.40  # 昨收×1.2 探针核实
    assert s["pct_chg"] == 0.12 and s["volume_hand"] == 211067
    assert not s["suspended"] and not s["at_limit_up"] and not s["at_limit_down"]
    assert _parse_qt_reply("v_sz000000=\"\"", {"000000"})["000000"] is None
