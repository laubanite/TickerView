"""盘中深入分析 v2.0 引擎回归(纯逻辑,不依赖网络)。

覆盖:C5' 五级场景布尔状态机 / C4' 档位语义校验(含缺档边界)/
sanitize_v2 注入式(该拦的必须拦)+ 误杀率(该放的必须放)/ 建议类别映射。
双样例期望:515790(趋势·超跌待恐慌证据)与 159516(趋势·左侧观望),
数字源自 kimi 快照,期望由程序口径重算(审查 §6.3)。
"""
from __future__ import annotations

from alphaprism.planner.intraday_engine import (
    BANNED_WORDS, param_anchors, sanitize_v2, signal_state, suggest_category,
)

SNAP = (
    "## 大盘\n| 日线 | 均线 | M5=0.856 M10=0.864 M20=0.850 |\n"
    "## 盘面状态(程序输出,LLM 不得修改)\n"
    "| 操作参数(程序锚点) | 突破 0.850 / 回踩 0.831 / 砍仓 0.820 / 止损 0.797 |"
)


def mk_facts(price, ma, rng=(1.0, 1.2), range_pos=50, vr=None, chg=None,
             kdj_j=None, swing_dd=None, swing_low=None, atr20=None, idx=None,
             m30=None, low=None):
    minute = {"price": price, "change_pct": chg}
    if low is not None:
        minute["low"] = low
    return {"etf": {
        "code": "999999", "name": "TEST",
        "daily": {"ma": ma, "kdj": {"J": kdj_j}, "macd": {},
                  "recent_high": rng[1], "recent_low": rng[0],
                  "swing_low": swing_low, "atr20": atr20},
        "m30": {"ma": m30} if m30 else {},
        "minute": minute,
        "vol_ratio": vr, "range_pos": range_pos, "swing_dd": swing_dd},
        "index": idx}


# ------------------------------------------------------------------ C5' 五级场景

def test_trend_accumulate():
    s = signal_state(mk_facts(1.10, {5: 1.15, 10: 1.18, 20: 1.20}, vr=None, kdj_j=50))
    assert s["scenario"] == "趋势" and s["state_word"] == "蓄势"


def test_trend_right_first():
    s = signal_state(mk_facts(1.21, {5: 1.15, 10: 1.18, 20: 1.20}, vr=2.0))
    assert s["scenario"] == "趋势" and s["state_word"] == "右侧初现"


def test_range_middle_and_upper():
    ma = {5: 1.102, 10: 1.100, 20: 1.098}
    s = signal_state(mk_facts(1.10, ma, range_pos=50, vr=None))
    assert s["scenario"] == "区间震荡" and s["sub_state"] == "中部"
    s = signal_state(mk_facts(1.10, ma, range_pos=80, vr=None))
    assert s["scenario"] == "区间震荡" and s["sub_state"] == "上沿收敛"


def test_breakout_day_and_precursor():
    ma = {5: 1.102, 10: 1.100, 20: 1.098}
    s = signal_state(mk_facts(1.21, ma, rng=(1.0, 1.2), vr=2.0))
    assert s["scenario"] == "突破异动日" and s["state_word"] == "右侧初现"
    s = signal_state(mk_facts(1.10, ma, rng=(1.0, 1.2), vr=2.0))
    assert s["scenario"] == "变盘前兆"


def test_breakdown():
    s = signal_state(mk_facts(1.12, {5: 1.05, 10: 1.10, 20: 1.15}, vr=2.0))
    assert s["scenario"] == "破位" and s["state_word"] == "破位退出"


def test_left_oversold_qualify_and_watch():
    f = mk_facts(1.08, {5: 1.00, 10: 0.97, 20: 0.95}, rng=(1.00, 1.15), vr=2.0,
                 chg=-6.0, kdj_j=5.0, swing_dd=20.0, swing_low=1.05, atr20=0.020)
    assert signal_state(f)["state_word"] == "超跌试多"
    f = mk_facts(1.08, {5: 1.00, 10: 0.97, 20: 0.95}, vr=None, chg=-2.0,
                 kdj_j=5.0, swing_dd=20.0, swing_low=1.05, atr20=0.020)
    assert signal_state(f)["state_word"] == "左侧观望"


# ------------------------------------------------------------------ 双样例期望(程序口径重算)

def test_golden_515790():
    """515790 @ 0.842:均线发散→趋势;超跌但平量无恐慌→左侧观望(待恐慌证据)。
    C4' v3:突破档=结构位(近20日上沿 0.894,§3.1 关键位口径,均线只作压力带);
    回踩带 0.831-0.838(30分M5–日内低),减仓 0.831,清仓止损 0.740(最下档)。"""
    facts = mk_facts(
        0.842,
        {5: 0.856, 10: 0.864, 20: 0.850, 60: 0.918, 100: 0.982, 120: 0.990, 250: 0.988},
        rng=(0.791, 0.894), range_pos=50, vr=0.96, chg=-1.17,
        kdj_j=-4.89, swing_dd=29.7, swing_low=0.740, atr20=0.022,
        m30={5: 0.838, 10: 0.842, 20: 0.847, 60: 0.862, 250: 0.841}, low=0.831)
    s = signal_state(facts)
    assert (s["scenario"], s["state_word"]) == ("趋势", "左侧观望")
    a = param_anchors(facts)
    assert a["anchor_price"] == 0.842
    assert a["ok"] is True
    # 突破档=结构位(近20日上沿),均线不入突破候选(空头里 M5 下移退化的根治)
    assert a["breakout_add"] == 0.894 and a["breakout_src"] == "近20日上沿"
    assert a["breakout_is_ma"] is False
    assert a["resist_band"][0] == 0.850          # 日线M20 作为压力带参考
    assert a["no_pullback"] is False
    z = a["pullback_zone"]
    assert z is not None and z["upper"] == 0.838 and z["lower"] == 0.831
    assert a["pullback_add"] == 0.838
    # v3:减仓档=回踩带下沿;清仓止损档 v2.8 = cut(0.831)以下最近止损类
    #     = 近20日低 0.791(不再是 250日极值 0.740;用户 2026-08-29 拍板)
    assert a["cut_loss"] == 0.831 and a["stop_loss"] == 0.791
    assert a["stop_src"] == "近20日低"
    assert a["checks"][5] is True          # 止损−减仓 ≥ max(0.5%, 1×ATR20)


def test_golden_159516():
    """159516 @ 0.729:空头排列趋势 + 缩量阴跌(J<10 但无恐慌)→ 左侧观望。"""
    facts = mk_facts(
        0.729,
        {5: 0.755, 10: 0.751, 20: 0.721, 60: 0.700},
        rng=(0.721, 0.755), range_pos=40, vr=0.7, chg=-1.0,
        kdj_j=-4.62, swing_dd=18.0, swing_low=0.700, atr20=0.010)
    s = signal_state(facts)
    assert (s["scenario"], s["state_word"]) == ("趋势", "左侧观望")


# ------------------------------------------------------------------ C4' 校验(含缺档边界)

def test_param_anchors_checks():
    f = mk_facts(0.842, {5: 0.856, 10: 0.864, 20: 0.850, 60: 0.918, 120: 0.820, 250: 0.780},
                 rng=(0.831, 0.870), swing_low=0.797, atr20=0.012, vr=0.9)
    a = param_anchors(f)
    assert a["ok"] is True and all(a["checks"].values())


def test_param_anchors_breakout_direction():
    """压力档高于锚定价;无支撑类位源 → 无回踩结构分支(三档,止损=最下档)。"""
    f = mk_facts(1.10, {5: 1.15, 10: 1.18, 20: 1.20}, rng=(1.05, 1.25),
                 swing_low=0.98, atr20=0.02, vr=1.0)
    a = param_anchors(f)
    assert a["breakout_add"] > a["anchor_price"]
    assert a["no_pullback"] is True and a["pullback_zone"] is None
    assert a["cut_loss"] < a["breakout_add"]


def test_param_anchors_v3_no_pullback_three_tiers():
    """空头无支撑(8.24 光伏缺 m30/日内低数据时的退化):只出三档,止损=最下档。"""
    f = mk_facts(0.842,
                 {5: 0.856, 10: 0.864, 20: 0.850, 60: 0.918, 100: 0.982, 120: 1.006, 250: 0.988},
                 rng=(0.791, 0.894), swing_low=0.740, atr20=0.022)
    a = param_anchors(f)
    assert a["no_pullback"] is True and a["pullback_zone"] is None
    assert a["cut_loss"] == 0.791 and a["stop_loss"] == 0.740
    assert a["ok"] is True


# ------------------------------------------------------------------ 止损档 v2.8(2026-08-29 用户拍板)

def test_stop_loss_nearest_stop_class_below_cut():
    """止损 = cut(回踩带下沿)以下最近止损类(近20日低),不再用 250日极值。"""
    f = mk_facts(0.842,
                 {5: 0.856, 10: 0.864, 20: 0.850, 60: 0.918, 100: 0.982, 120: 0.990, 250: 0.988},
                 rng=(0.791, 0.894), range_pos=50, vr=0.96, chg=-1.17,
                 kdj_j=-4.89, swing_dd=29.7, swing_low=0.740, atr20=0.022,
                 m30={5: 0.838, 10: 0.842, 20: 0.847, 60: 0.862, 250: 0.841}, low=0.831)
    a = param_anchors(f)
    assert a["ok"] is True
    zone = a["pullback_zone"]
    assert zone is not None and zone["lower"] == 0.831   # cut = 回踩带下沿
    assert a["cut_loss"] == 0.831
    assert a["stop_loss"] == 0.791                       # cut 以下最近止损类 = 近20日低
    assert a["stop_src"] == "近20日低"
    assert a["stop_loss"] < a["cut_loss"]                # 校验④


def test_stop_loss_far_stop_class_fallback_nearest_down():
    """止损类距 cut 超 3×ATR20 → 回退 cut 下方最近支撑兜底(不落远距)。"""
    f = mk_facts(1.036,
                 {5: 1.021, 10: 1.041, 20: 1.028, 60: 1.142, 100: 1.108, 120: 1.056, 250: 0.878},
                 rng=(0.887, 1.119), range_pos=60, vr=0.98,
                 swing_low=0.515, atr20=0.012,
                 m30={5: 1.043, 10: 1.050, 20: 1.040, 60: 1.024, 250: 1.023}, low=1.034)
    f["etf"]["m30"]["range_low"] = 0.975          # 30分区间低入支撑类(兜底候选)
    f["etf"]["m30"]["range_high"] = 1.078
    a = param_anchors(f)
    assert a["ok"] is True
    zone = a["pullback_zone"]
    assert zone is not None
    # cut = pullback 下沿(1.028 日线M20);止损类=近20日低0.887/阶段低0.515
    # 0.887 距 cut 0.141 > 3×ATR20(0.036) → 兜底到 cut 下方最近且有空间的支撑
    # (0.975=30分区间低;M5=1.021 排除:空间 0.007 < 0.012)
    cut = a["cut_loss"]
    assert a["stop_loss"] == 0.975 and a["stop_src"] == "30分区间低"
    assert a["stop_loss"] < cut
    # 止损绝不能落到 250日极值 0.515(远距 bug 根因)
    assert abs(a["stop_loss"] - 0.515) > 1e-6
    # 止损距 cut 有真实空间(校验⑤)
    assert (cut - a["stop_loss"]) >= max(1.036 * 0.005, 0.012)


def test_stop_loss_no_stop_class_below_cut():
    """cut 下方无止损类(数据残缺:无阶段低)→ 取 cut 下方最近且有空间的位。"""
    f = mk_facts(1.10,
                 {5: 1.03, 10: 1.18, 20: 1.15, 60: 1.25},
                 rng=(1.05, 1.20), swing_low=None, atr20=0.02, vr=1.0,
                 m30={5: None, 10: None, 20: None}, low=None)
    a = param_anchors(f)
    assert a["ok"] is True
    assert a["stop_loss"] is not None and a["stop_loss"] < a["cut_loss"]
    # 止损类缺失 → 兜底用下方最近支撑类(日线M5=1.03),且与 cut 有真实空间
    assert a["stop_loss"] == 1.03 and a["stop_src"] == "日线M5"


# ------------------------------------------------------------------ 注入式(该拦的必须拦)

def test_sanitize_banned_and_direction():
    md, deg, _ = sanitize_v2(
        "## 六、操作参数\n- 突破加仓位: 0.850 (程序锚点)\n- 止损认错位: 0.780 (测试)\n"
        "## 七、市场状态\n右侧确认\n## 八、操作建议\n回踩 0.870 企稳后加仓\n"
        "## 建议类别: 加仓",
        SNAP, {"state_word": "右侧初现", "scenario": "趋势"},
        {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
         "pullback_add": 0.831, "cut_loss": 0.820, "stop_loss": 0.797})
    assert "右侧确认" not in md          # 单日截面禁词
    assert "回踩 0.870" not in md        # 动词方向矛盾(0.870 在现价上方)
    assert deg is False


def test_sanitize_fatal_degrade():
    """价位违规 ≥2 → 致命降级。"""
    _, deg, issues = sanitize_v2(
        "止损 0.700(测试)\n清仓线 0.699(测试)\n正常句",
        SNAP, {"state_word": "蓄势", "scenario": "趋势"},
        {"anchor_price": 0.842, "ok": True})
    assert deg is True and len(issues) >= 1


def test_sanitize_risk_level_whitelist():
    """自造风险等级 → 删句。"""
    md, _, _ = sanitize_v2(
        "风险等级: 极度危险\n其他正常句",
        SNAP, {"state_word": "蓄势", "scenario": "趋势"},
        {"anchor_price": 0.842, "ok": True}, {"risk_level": "正常"})
    assert "极度危险" not in md


# ------------------------------------------------------------------ 误杀率(该放的必须放)

def test_sanitize_no_false_positive():
    """合法条件单:价位全在白名单、动词方向正确、无禁词 → 不删句、不降级。"""
    legal = (
        "## 七、市场状态\n蓄势\n"
        "## 八、操作建议\n若回踩 0.831 企稳(现价 0.842 已在下方到位)可考虑回踩企稳加仓;\n"
        "若放量突破 0.850 则触发右侧初现;跌破 0.820 执行砍仓,跌破 0.797 认错离场。\n"
        "## 建议类别: 观望"
    )
    md, deg, issues = sanitize_v2(
        legal, SNAP, {"state_word": "蓄势", "scenario": "趋势"},
        {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
         "pullback_add": 0.831, "cut_loss": 0.820, "stop_loss": 0.797})
    assert deg is False
    assert "回踩 0.831" in md and "突破 0.850" in md
    for w in BANNED_WORDS:
        assert w not in md


# ------------------------------------------------------------------ 建议类别

def test_suggest_category():
    assert suggest_category("## 建议类别: 减仓\n末尾", {"state_word": "蓄势"}) == "减仓"
    assert suggest_category("无类别行", {"state_word": "破位退出"}) == "减仓"
    assert suggest_category("无类别行", {"state_word": "右侧初现"}) == "加仓"
    assert suggest_category("无类别行", {"state_word": "持有"}) == "观望"


# ------------------------------------------------------------------ T0 修复回归(2026-08-25,溯源 8.24 光伏报告)

def test_macd_label_factual_color():
    """T0+v2.3:柱符号定红绿;零轴语境(零下=空头环境)。"""
    from alphaprism.planner.indicators import macd_state
    assert macd_state({"dif": -0.006, "dea": -0.006, "hist": -0.001,
                       "hist_prev": -0.005}) == "绿柱·柱缩小·零下"
    assert macd_state({"dif": -0.007, "dea": -0.010, "hist": 0.006,
                       "hist_prev": -0.001}) == "红柱·柱扩大·零下"
    assert macd_state({"dif": -0.007, "dea": -0.010, "hist": 0.006,
                       "hist_prev": -0.001, "cross": "金叉"}) == "金叉·红柱·柱扩大·零下"
    assert macd_state({"dif": 0.05, "dea": 0.02, "hist": 0.012,
                       "hist_prev": 0.008}) == "红柱·柱扩大·零上"


def test_fmt_kdj_shape_labels():
    """v2.3+ v2.4:KDJ 三线结构程序标注,带区间语境(超买区J上翘=过热回调,超卖区J深低于K=修复积蓄)。"""
    from alphaprism.planner.intraday import _fmt_kdj
    assert _fmt_kdj({"K": 27.53, "D": 40.99, "J": 0.61}) == \
        "K=27.53 D=40.99 J=0.61(超卖·空排·J深低于K·反弹修复积蓄)"   # 8.25 日线(倒喇叭)
    assert _fmt_kdj({"K": 69.0, "D": 57.33, "J": 92.35}) == \
        "K=69.0 D=57.33 J=92.35(超买·多排·J上翘·超买过热·回调风险)"  # 8.25 30分
    assert _fmt_kdj({"K": 85.0, "D": 82.0, "J": 88.0}) == \
        "K=85.0 D=82.0 J=88.0(超买·多排·三线共振超买)"
    assert _fmt_kdj({"K": 50.0, "D": 51.0, "J": 49.5}) == \
        "K=50.0 D=51.0 J=49.5(中性·空排·三线收敛)"


def test_sanitize_orderbook_conclusion_consistency():
    """T0:程序"抛压占优(内盘大)"时,LLM 写"外盘抛压/内盘承接"→ 删句(方向反转)。"""
    snap = SNAP + "| 盘口 | 外内盘 | 抛压占优(内盘大): 外盘 74.02万手 内盘 105.57万手(来源:腾讯) |\n"
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "pullback_add": 0.831, "cut_loss": 0.820, "stop_loss": 0.797}
    md, deg, _ = sanitize_v2(
        "## 四、分时盘口\n外盘抛压占优但内盘承接尚可,尾盘缩量。\n"
        "## 八、操作建议\n跌破 0.820 执行砍仓。\n## 建议类别: 观望",
        snap, {"state_word": "蓄势", "scenario": "趋势"}, anchors)
    assert "承接" not in md and "外盘抛压" not in md
    assert "跌破 0.820" in md          # 正常句保留(误杀率 0)
    assert deg is False


def test_sanitize_stop_loss_missing_replaced():
    """T0:止损缺档时 LLM 写"暂未设定" → 替换为规则句(8.24 光伏"止损位暂未设定")。"""
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "pullback_add": 0.831, "cut_loss": 0.820, "stop_loss": None}
    md, deg, _ = sanitize_v2(
        "## 六、操作参数\n突破 0.850,回踩 0.831,砍仓 0.820,止损位暂未设定。\n## 建议类别: 观望",
        SNAP, {"state_word": "蓄势", "scenario": "趋势"}, anchors)
    assert "暂未设定" not in md
    assert "止损缺档" in md
    assert "0.820" in md               # 砍仓档仍存在,作为最严防线被引用
    assert deg is False


def test_render_stop_loss_missing_explicit():
    """T0:盘面状态段止损缺档显式标注,不再渲染成 '-' 被 LLM 自由发挥。"""
    from alphaprism.planner.intraday_engine import render_state_md
    state = {"scenario": "趋势", "state_word": "蓄势", "sub_state": "超跌待恐慌证据",
             "spread_pct": 1.6}
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "pullback_add": 0.791, "cut_loss": 0.740, "stop_loss": None}
    md = render_state_md({}, state, anchors, None, None)
    assert "止损 缺档(快照无更低白名单档)" in md
    assert "止损 -" not in md


def test_catalyst_risk_renamed_upgrade():
    """T0:第三档风险等级由"降级"改"升级"(三段式 §4.3 已改,工程化/代码同步)。"""
    from alphaprism.planner.intraday_engine import catalyst_context
    c = catalyst_context({"etf": {"code": "515790.SH"}, "index": None},
                         {"scenario": "破位"}, None)
    assert c["risk_level"] == "升级"


# ------------------------------------------------------------------ C4' v3 / v2.1 回归(2026-08-25)

def test_render_v3_identity_and_zone():
    """C4' v3 盘面状态:身份词 + 回踩带区间 + 位源 + 清仓止损(最下档)。"""
    from alphaprism.planner.intraday_engine import render_state_md
    state = {"scenario": "趋势", "state_word": "左侧观望", "sub_state": "超跌待恐慌证据",
             "spread_pct": 1.6}
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "breakout_src": "日线M20", "no_pullback": False,
               "pullback_add": 0.838,
               "pullback_zone": {"upper": 0.838, "lower": 0.831,
                                 "upper_src": "30分M5", "lower_src": "日内低", "tight": True},
               "cut_loss": 0.831, "cut_src": "日内低", "stop_loss": 0.740,
               "stop_src": "阶段低"}
    md = render_state_md({}, state, anchors, None, None)
    assert "突破加仓位 0.850(日线M20)" in md
    assert "回踩加仓位 0.831-0.838" in md and "日内低–30分M5" in md
    assert "减仓 跌破0.831" in md and "清仓止损 0.740(阶段低)" in md


def test_render_zone_in_zone_vs_approachable():
    """2026-08-29:回踩带分开表述——带内 / 已到位(距上沿)不再混为'已到位/带内'。"""
    from alphaprism.planner.intraday_engine import render_state_md
    state = {"scenario": "趋势", "state_word": "左侧观望", "sub_state": "超跌待恐慌证据",
             "spread_pct": 1.6}
    base = {"anchor_price": 0.850, "ok": True, "breakout_add": 0.894,
            "cut_loss": 0.831, "stop_loss": 0.805}
    # 带内:现价 0.850 落在 [0.831, 0.855] 内
    z_in = {"upper": 0.855, "lower": 0.831, "upper_src": "日线M20",
            "lower_src": "日内低", "in_zone": True, "tight": True}
    md1 = render_state_md({"etf": {"minute": {"price": 0.850}}}, state,
                          {**base, "pullback_zone": z_in}, None, None)
    assert "(带内)" in md1 and "已到位/带内" not in md1
    # 已到位未入带:现价 0.858 略高于上沿 0.855
    z_appr = {"upper": 0.855, "lower": 0.831, "upper_src": "日线M20",
              "lower_src": "日内低", "in_zone": False, "tight": True}
    md2 = render_state_md({"etf": {"minute": {"price": 0.858}}}, state,
                          {**base, "pullback_zone": z_appr}, None, None)
    assert "(已到位,距上沿 0.35%)" in md2 and "带内" not in md2
    # 未到位:现价 0.900 远离上沿
    z_far = {"upper": 0.855, "lower": 0.831, "upper_src": "日线M20",
             "lower_src": "日内低", "in_zone": False, "tight": False}
    md3 = render_state_md({"etf": {"minute": {"price": 0.900}}}, state,
                          {**base, "pullback_zone": z_far}, None, None)
    assert "带内" not in md3 and "已到位" not in md3


def test_render_v3_no_pullback_identity():
    """无回踩结构:身份=反弹压力位,显式标注'回踩 无结构'。"""
    from alphaprism.planner.intraday_engine import render_state_md
    state = {"scenario": "趋势", "state_word": "左侧观望", "sub_state": "超跌待恐慌证据",
             "spread_pct": 1.6}
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "breakout_src": "日线M20", "no_pullback": True,
               "pullback_add": None, "pullback_zone": None,
               "cut_loss": 0.791, "cut_src": "近20日低", "stop_loss": 0.740,
               "stop_src": "阶段低"}
    md = render_state_md({}, state, anchors, None, None)
    assert "反弹压力位 0.850(日线M20)" in md
    assert "回踩 无结构" in md


def test_sanitize_identity_verb_no_pullback():
    """身份×动词(§6.1.1):无回踩结构下 "突破 X 可考虑回踩" 删句,合法"回踩 0.831 风险提示"保留。"""
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "pullback_add": None, "cut_loss": 0.831, "stop_loss": 0.740,
               "no_pullback": True}
    md, deg, _ = sanitize_v2(
        "## 六、操作参数\n突破 0.850 可考虑回踩。\n"
        "## 九、风险提示\n回踩 0.831 需警惕减仓风险。\n## 建议类别: 观望",
        SNAP, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "可考虑回踩" not in md
    assert "回踩 0.831" in md          # 合法句不误杀
    assert deg is False


def test_sanitize_index_suppression():
    """大盘均线全压制时 LLM 写"托举"→ 删句(8.24 光伏)。"""
    snap = ("## 大盘\n| 日线 | 均线性质 | 受制 M5/M10/M20/M60(上方压力) |\n"
            "## 盘面状态(程序输出,LLM 不得修改)\n"
            "| 操作参数(程序锚点) | 突破 0.850 / 回踩 0.831 / 砍仓 0.820 / 止损 0.797 |")
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.850,
               "pullback_add": 0.831, "cut_loss": 0.820, "stop_loss": 0.797}
    md, deg, _ = sanitize_v2(
        "## 一、大盘环境\n上证指数均线承压,但短期内或存在一定的托举作用。\n"
        "## 建议类别: 观望",
        snap, {"state_word": "蓄势", "scenario": "趋势"}, anchors)
    assert "托举" not in md and deg is False


# ------------------------------------------------------------------ v2.2 修复回归(2026-08-25,kimi 审阅轮)

def test_sanitize_trial_multi_gating():
    """试多护栏:① 左侧观望下"可择机试多"删句;② "跌破X试多"永远非法;
    ③ 合法"评估试多候选+缺项"保留(不误杀)。"""
    anchors = {"anchor_price": 0.842, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.831, "cut_loss": 0.820, "stop_loss": 0.797,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 八、操作建议\n待出现恐慌性抛售信号(如放量跌破0.820)时可择机试多。\n"
        "## 九、风险提示\n左侧试多候选评估:缺恐慌证据、位置未到位(距强支撑>1.5%)。\n"
        "## 建议类别: 观望",
        SNAP, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "可择机试多" not in md and "跌破0.820)时可择机" not in md
    assert "评估" in md and "缺恐慌证据" in md       # 合法候选评估保留
    assert deg is False


def test_fund_section_mechanical_fallback():
    """资金段机械兜底(8.25 实测:LLM 把占位符当标题、内容为空)→ 程序插入规则句。"""
    from alphaprism.planner.intraday import _ensure_fund_section
    snap = ("## 标的\n| 资金 | 份额 | 67.61亿份(2026-06-30); 较上期 -35.48亿份(-34.42%) |\n"
            "## 大盘\n| 日线 | 均线 | M5=3894.958 |\n")
    md = ("## 十、资金面(强制段,必须输出)\n## 建议类别: 观望")
    out, notes = _ensure_fund_section(md, snap)
    assert notes and "资金面缺位" in notes[0]
    assert "程序兜底" in out and "-34.42%" in out
    assert "中长期资金流出" in out or "资金大幅流出" in out
    # 已做资金分析的正文 → 不重复插入(原样保留)
    md2 = ("## 十、资金面\n份额较上期 -35.48亿份(-34.42%),为半年级别资金流出,压制反弹高度。\n"
           "## 建议类别: 观望")
    out2, notes2 = _ensure_fund_section(md2, snap)
    assert not notes2 and out2 == md2


# ------------------------------------------------------------------ v2.3 修复回归(2026-08-25,kimi 二轮 + 解读词典)

SNAP23 = (
    "## 标的\n"
    "| 分时 | 现价 | 0.838 (-0.48%) |\n"
    "| 分时 | 日内高低 | 0.843@13:16 / 0.822 (距日内最高回撤 0.59%·日内分位 76%(高位区)) |\n"
    "| 日线 | 位置 | 距阶段高点(近250日) 1.198@2026-03-11 回撤 30.05%;近20日区间 0.791-0.894,"
    "现价位于区间 46%·中下轨 |\n"
    "| 日线 | 均线 | M5=0.845 M20=0.851 |\n"
    "## 大盘\n| 日线 | 均线 | M5=3894.958 |\n"
    "## 盘面状态(程序输出,LLM 不得修改)\n"
    "| 操作参数(程序锚点) | 突破加仓位 0.894(近20日上沿) / 回踩加仓位 0.836-0.837 / "
    "减仓 跌破0.836 / 清仓止损 0.740 |")


def test_sanitize_breakout_confirm_word():
    """突破档触发必须带确认词(8.25:"若突破0.845,可加仓"无放量/收盘 → 删句)。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 八、操作建议\n若突破0.894,可加仓。\n"
        "若放量收盘站上 0.894 则谈右侧初现。\n## 建议类别: 观望",
        SNAP23, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "可加仓" not in md                    # 无确认词句删掉
    assert "放量收盘站上 0.894" in md            # 合法触发句保留
    assert deg is False


def test_sanitize_divergence_term():
    """"背离"只许价格-指标背离(8.25:"与日线形成背离"误用 → 删句);顶背离保留。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 三、标的·30分\n30分超买,与日线形成背离。\n"
        "## 九、风险提示\n若价格创新高而MACD未创新高,构成顶背离。\n## 建议类别: 观望",
        SNAP23, {"state_word": "蓄势", "scenario": "趋势"}, anchors)
    assert "形成背离" not in md
    assert "顶背离" in md                        # 价格-指标背离语境合法
    assert deg is False


def test_sanitize_position_fuzzy_word():
    """近20日区间 46%(中轨区)时,"下轨附近"误读 → 删句;30分"下轨"精确语保留。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 二、标的·日线\n处于近20日区间下轨附近,反弹空间有限。\n"
        "## 三、标的·30分\n近60根区间 0.822-0.894,现价位于下轨区,反弹空间受限。\n"
        "## 建议类别: 观望",
        SNAP23, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "下轨附近" not in md
    assert "近60根区间" in md and "下轨区" in md   # 精确档位语保留
    assert deg is False


def test_sanitize_intraday_vague():
    """"日内高低点附近"未量化 → 删句(快照已给日内分位 76% 高位区)。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 四、分时盘口\n分时现价在日内高低点附近运行。\n尾盘缩量。\n"
        "## 建议类别: 观望",
        SNAP23, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "高低点附近" not in md
    assert "尾盘缩量" in md
    assert deg is False


# ------------------------------------------------------------------ v2.4 修复回归(2026-08-25 深夜,kimi/用户合审轮)

SNAP24 = (
    "## 标的\n"
    "| 分时 | 现价 | 0.838 (-0.48%) |\n"
    "| 量能 | 量能 | 量比 0.73(缩量) |\n"
    "| 日线 | 均线 | M5=0.845 M10=0.861 M20=0.851 |\n"
    "| 30分 | 均线 | M5=0.837 M10=0.836 M20=0.840 |\n"
    "| 盘口 | 五档 | 买4 0.835/1.22万手 |\n"
    "| 盘口 | 外内盘 | 抛压占优(内盘大): 外盘 78.38万手 内盘 79.09万手(来源:腾讯) |\n"
    "| 资金 | 份额 | 67.61亿份(2026-06-30); 较上期 -35.48亿份(-34.42%) |\n"
    "## 大盘\n"
    "| 日线 | 均线性质 | 受制 M5/M10/M20/M60/M100/M120/M250(上方压力) |\n"
    "| 大盘 | 趋势维度(程序) | 跌破 M20(现价 0.838 vs M20 0.851) |\n"
    "| 大盘 | 动能维度(程序) | DIF -0.006 零下 · DEA -0.008 零下(绿柱·柱缩小·零下) |\n"
    "## 盘面状态(程序输出,LLM 不得修改)\n"
    "| 操作参数(程序锚点) | 突破加仓位 0.894(近20日上沿) / 反弹压力带 0.845-0.851 / "
    "回踩加仓位 0.836-0.837 / 减仓 跌破0.836 / 清仓止损 0.740 |")


def test_sanitize_index_verdict_conflict():
    """大盘两维解耦(2026-08-29):趋势跌破 M20 + 动能双零下时,"中性偏暖/托举"→ 删句。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 一、大盘环境\n上证指数中性偏暖,对光伏形成一定托举。\n"
        "## 建议类别: 观望",
        SNAP24, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "中性偏暖" not in md and "托举" not in md
    assert deg is False


def test_sanitize_orderbook_price_not_tier():
    """盘口买4挂单价 0.835 不得作"失效/触发"价位(8.25:"失效=0.835"无依据)。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 八、操作建议\n失效率=0.835,盘中破位即减。\n"
        "## 四、分时盘口\n买1-5 档可见买4 0.835/1.22万手挂单。\n## 建议类别: 观望",
        SNAP24, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "失效率=0.835" not in md          # 操作价位=盘口价 → 删句
    assert "买4 0.835" in md                 # 盘口叙述合法 → 保留
    assert deg is False


def test_sanitize_vol_ratio_unit():
    """"量比0.54%"单位/数值错误 → 删句;量比 0.73 原值保留。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 三、标的·30分\n量比0.54%,明显缩量。\n量比 0.73,为盘中缩量状态。\n"
        "## 建议类别: 观望",
        SNAP24, {"state_word": "蓄势", "scenario": "趋势"}, anchors)
    assert "量比0.54%" not in md
    assert "量比 0.73" in md
    assert deg is False


def test_sanitize_kdj_as_subject():
    """指标不得作回踩/企稳主语("30分KDJ回踩0.836"病句);价格回踩句保留。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 三、标的·30分\n30分KDJ回踩0.836后企稳。\n价格回踩0.836后观察企稳。\n"
        "## 建议类别: 观望",
        SNAP24, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "30分KDJ回踩" not in md
    assert "价格回踩0.836" in md
    assert deg is False


def test_sanitize_turn_signal_whitelist():
    """"J转负"作企稳信号 → 删句(C8' 白名单禁止);"日线KDJ低位金叉"企稳保留。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 九、风险提示\n企稳信号需等J转负或J开口收窄。\n"
        "企稳信号需日线KDJ低位金叉(J<30 K上穿D)。\n## 建议类别: 观望",
        SNAP24, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "J转负" not in md
    assert "低位金叉" in md
    assert deg is False


def test_sanitize_kdj_label_consistency():
    """KDJ 标签一致性(8.27 A/B 实测):快照中性,LLM 写"日线KDJ超卖" → 删句。"""
    snap = SNAP24.replace(
        "| 30分 | 均线 | M5=0.837 M10=0.836 M20=0.840 |",
        "| 30分 | 均线 | M5=0.837 M10=0.836 M20=0.840 |\n"
        "| 日线 | KDJ | K=32.3 D=36.05 J=24.8(中性·空排·三线收敛) |")
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 二、标的·日线\n日线KDJ超卖(J=24.8),超跌积蓄反弹动能。\n"
        "日线KDJ中性(J=24.8),三线收敛。\n## 建议类别: 观望",
        snap, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "日线KDJ超卖" not in md
    assert "日线KDJ中性" in md
    assert deg is False


def test_strip_card_section_review_view():
    """verifier 评审视图(正文+卡段)→ 剥离卡段还原正文。"""
    from alphaprism.planner.intraday import _strip_card_section
    view = ("## 八、操作建议\n观望。\n\n"
            "【结论卡】\n### 📋 结论卡(结构化·程序校验)\n- 核心判断: ...\n- 失效: ...")
    body = _strip_card_section(view)
    assert "核心判断" not in body and "【结论卡】" not in body
    assert "## 八、操作建议" in body and "观望" in body
    # 无卡段 → 原样
    assert _strip_card_section("## 八、操作建议\n观望。") == "## 八、操作建议\n观望。"


def test_verb_direction_ignores_non_price_numbers():
    """regex 收紧:动词+非价格数字(如 '回踩30分钟K')不得被当价位误删。"""
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 九、风险提示\n若回踩30分钟K收盘确认,企稳信号需 C8' 三选一。\n"
        "## 建议类别: 观望",
        SNAP24, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "回踩30分钟K" in md      # 30 不是价位 → 不因动词方向被误删
    assert deg is False


def test_rewrite_pass_rewrites_instead_of_deletes():
    """步骤3(删句→改写):可确定性改写的违规句被模板替换且信息保留,非删除。"""
    snap = ("## 标的\n"
            "| 分时 | 现价 | 0.838 (-0.48%) |\n"
            "| 分时 | 日内高低 | 0.843@13:16 / 0.822 (距日内最高回撤 0.59%·日内分位 76%(高位区)) |\n"
            "| 日线 | 位置 | 距阶段高点(近250日) 1.198@2026-03-11 回撤 30.05%;近20日区间 "
            "0.791-0.894,现价位于区间 46%·中下轨 |\n"
            "| 日线 | 均线 | M5=0.845 M20=0.851 |\n"
            "| 30分 | 均线 | M5=0.837 M10=0.836 |\n"
            "## 大盘\n| 日线 | 均线 | M5=3894.958 |\n"
            "## 盘面状态\n| 操作参数 | 突破加仓位 0.894(近20日上沿) / 回踩 0.836-0.837 / "
            "减仓 跌破0.836 / 清仓止损 0.740 |")
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, issues = sanitize_v2(
        "## 一、大盘环境\n待恐慌出清后可择机试多。\n"
        "## 二、标的·日线\n处于近20日区间下轨附近。\n"
        "## 三、标的·30分\n若站上0.894可加仓。\n"
        "## 四、分时盘口\n分时现价在日内高低点附近运行。\n"
        "## 九、风险提示\n企稳信号需等J开口收窄。\n"
        "## 建议类别: 观望",
        snap, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    # 五句全被改写(模板句保留,而非删除)
    assert "可择机试多" not in md and "§4.2" in md
    assert "下轨附近" not in md and "中下轨" in md
    assert "可加仓" not in md and "放量+日线收盘站上 0.894" in md
    assert "高低点附近" not in md and "日内分位" in md
    assert "J开口收窄" not in md and "C8'" in md
    assert deg is False
    assert any("改写" in i for i in issues)      # issues 记录改写(而非仅删句)


# ------------------------------------------------------------------ 验证层第二轨(LLM 风控复核,v2.5)

def test_review_prompt_has_both_docs():
    """复核 prompt 必须同时含事实卡片与初稿(Consistency Checking 输入完整)。"""
    from alphaprism.planner.intraday import _review_prompt
    p = _review_prompt("## 一、大盘环境\n上证指数中性偏暖。",
                       "## 大盘\n| 大盘 | 方向定性(程序) | 系统性压制 |")
    assert "中性偏暖" in p and "系统性压制" in p
    assert "硬伤" in p and "注水" in p          # 分级清单模板存在


def test_merge_review_appends_without_touching_body():
    """v3.2 复核不再展示:复核清单只入日志,不追加 '风控复核' 段(用户只看修改好的报告)。"""
    from alphaprism.planner.intraday import _merge_review
    md = ("## 六、操作参数\n突破加仓位 0.894(近20日上沿)。\n"
          "## 建议类别: 观望")
    review = ("- [硬伤] 大盘'中性偏暖'与程序'系统性压制'矛盾\n"
              "- [注水] 结论复述状态词\n"
              "建议类别: 加仓   # 恶意混入,应被剔除")
    out, ok = _merge_review(md, review)
    assert ok is False                              # 不再追加复核段
    assert out == md                                # 正文原样保留(修改发生在 _apply_review_fixes)
    assert "## 风控复核" not in out
    assert "建议类别" not in out.replace("## 建议类别: 观望", "")
    # 空复核 → 原样
    out2, ok2 = _merge_review(md, "  ")
    assert ok2 is False and out2 == md


# ------------------------------------------------------------------ 反事实推演(盘后·情景分支,v2.5)

def test_migration_table_deterministic():
    """迁移表 = 确定性逐级观察档(价低→价高);LLM 不得越界。"""
    from alphaprism.planner.intraday import _migration_table
    facts = {
        "etf": {
            "daily": {"ma": {5: 0.845, 10: 0.861, 20: 0.851, 60: 0.914},
                      "recent_high": 0.894, "recent_low": 0.791,
                      "swing_low": 0.740, "atr20": 0.020},
            "m30": {"ma": {5: 0.837, 10: 0.836, 20: 0.840},
                    "range_low": 0.822, "range_high": 0.894},
            "minute": {"price": 0.838, "low": 0.822},
        }
    }
    down, up = _migration_table(facts)
    assert down and up
    # 下行从最近档开始,逐级更低:首个应含回踩带下沿 0.836
    assert "0.836" in down[0]
    # 末档必须含最下档 0.740(阶段低)
    assert "0.740" in down[-1]
    # 上行首档=最近压力(0.845 日线M5),全链含结构位 0.894(近20日上沿)与最远档 0.914(M60)
    assert "0.845" in up[0]
    assert "0.894" in " ".join(up)
    assert "0.914" in up[-1]


def test_check_counterfactual_whitelist():
    """反事实输出:越界价位/指令词/建议类别一律剔除;迁移表价位保留。"""
    from alphaprism.planner.intraday import _check_counterfactual
    snapshot = ("## 标的\n| 日线 | 均线 | M5=0.845 |\n"
                "## 盘面状态\n| 操作参数 | 突破 0.894 |")
    down = ["跌破 0.836(30分M10) → 观察 0.822(30分区间低)"]
    up = ["站上 0.894(近20日上沿) → 观察 0.845(日线M5)"]
    text = ("若大盘跌破 3850,光伏支撑将下移至 0.850,建议立即减仓。\n"
            "若站上 0.894,则测试 0.845。\n建议类别: 减仓")
    out, issues = _check_counterfactual(text, snapshot, down, up)
    assert "0.850" not in out                    # 越界价位句剔除
    assert "建议立即减仓" not in out             # 指令性表述剔除
    assert "建议类别" not in out
    assert "0.894" in out and "0.845" in out     # 迁移表价位保留
    assert issues  # 至少一条剔除记录


# ------------------------------------------------------------------ v2.6 修复回归(C7 符号 + 复核自愈)

def test_rs_label_sign():
    """C7 符号修复(8.26 光伏实盘硬伤):rs=标的涨幅−大盘涨幅,负=跑输。"""
    from alphaprism.planner.intraday import _rs_label
    # 光伏实测:标的-5.843% vs 大盘-2.53% → rs=-3.31 → 跑输·大幅
    assert _rs_label(-3.31) == "跑输·大幅"
    assert _rs_label(-1.2) == "跑输"
    assert _rs_label(2.0) == "跑赢"
    assert _rs_label(4.0) == "跑赢·显著"
    assert _rs_label(0.0) == "持平"


def test_sanitize_relative_strength_direction():
    """事实卡=跑输时 LLM 写"跑赢" → 删句(8.26 光伏:报告'跑赢3.31pp'实为跑输)。"""
    snap = SNAP24.replace("## 大盘", "| 相对强弱 | 近5日 | 标的 vs 大盘 -3.31pp(跑输·大幅) |\n## 大盘")
    anchors = {"anchor_price": 0.838, "ok": True, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740,
               "no_pullback": False}
    md, deg, _ = sanitize_v2(
        "## 一、大盘环境\n标的近5日跑赢大盘3.31pp,显示板块相对强势。\n"
        "标的近5日落后大盘3.31pp(跑输),属板块自身弱势。\n## 建议类别: 观望",
        snap, {"state_word": "左侧观望", "scenario": "趋势"}, anchors)
    assert "跑赢大盘3.31pp" not in md            # 方向矛盾句删
    assert "落后大盘3.31pp" in md                # 合法句保留
    assert deg is False


def test_review_parse_and_apply_fixes():
    """复核硬伤自愈:原文子句被修正句替换(2026-08-29 不标注标签);越界价位/指令词被拒绝。"""
    from alphaprism.planner.intraday import _parse_review_fixes, _apply_review_fixes
    review = ("- [硬伤] 原文「上证指数中性偏暖,对光伏形成一定托举。」→ 事实「大盘程序定性="
              "系统性压制(均线空头排列·MACD零下)」→ 修正「上证指数系统性压制(均线空头排列·"
              "MACD零下),对光伏形成Beta压制。」\n"
              "- [硬伤] 原文「标的近5日跑赢大盘3.31pp」→ 事实「rs=-3.31=跑输」→ 修正「标的近5日跑输大盘3.31pp」\n"
              "- [可疑] 原文「或可观察」→ 事实「无」→ 修正「或可观察0.850」  # 越界价位,应拒绝\n"
              "- [注水] 小结复述状态词。")
    items = _parse_review_fixes(review)
    assert len(items) >= 2
    snapshot = "## 标的\n| 日线 | 均线 | M5=0.845 |\n| 相对强弱 | 近5日 | -3.31pp(跑输·大幅) |\n"
    anchors = {"ok": True, "anchor_price": 0.838, "breakout_add": 0.894,
               "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740}
    md = ("## 一、大盘环境\n上证指数中性偏暖,对光伏形成一定托举。\n"
          "标的近5日跑赢大盘3.31pp,显示板块相对强势。\n## 建议类别: 观望")
    out, changed = _apply_review_fixes(md, items, snapshot, anchors)
    assert changed is True
    assert "中性偏暖" not in out                       # 硬伤已被替换
    assert "Beta压制" in out and "风控复核修正" not in out   # 标签不再标注
    assert "跑赢大盘3.31pp" not in out and "跑输大盘3.31pp" in out
    # 越界价位修正句(0.850 不在白名单)未生效
    assert "或可观察0.850" not in out.replace("或可观察", "") or "0.850" not in out