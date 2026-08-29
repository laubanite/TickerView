# -*- coding: utf-8 -*-
"""合成规则矩阵生成器:参数化形态 → facts dict。

每个形态对应《交易系统设计》一个规则分支(场景机 C5' / 档位 C4' / 止损 v2.8)。
命名规则:<族>-<分支>,注释标注覆盖的规则。

使用方法:
    from gen import MATRIX
    for name, facts, expect in MATRIX:
        ...
expect = {ok: bool}  —— ok=False 表示该形态预期档位不完整(数据残缺合法场景)。
"""
from __future__ import annotations

# --------------------------------------------------------------------------- builder


def mk(price, ma, m30_ma=None, rng=(1.0, 1.2), range_pos=50, vr=None, chg=None,
       kdj_j=None, swing_dd=None, swing_low=None, atr20=None, idx=None,
       m30_range=None, low=None, kdj=None, macd=None) -> dict:
    """构造 facts dict(与 test_intraday_engine.mk_facts 同构,可独立使用)。"""
    minute = {"price": price, "change_pct": chg}
    if low is not None:
        minute["low"] = low
    m30 = {"ma": dict(m30_ma)} if m30_ma else {}
    if m30_range:
        m30["range_low"], m30["range_high"] = m30_range
    return {"etf": {
        "code": "SYN", "name": "SYN",
        "daily": {"ma": dict(ma),
                  "kdj": {"J": kdj_j} if kdj is None else dict(kdj),
                  "macd": dict(macd) if macd else {},
                  "recent_high": rng[1], "recent_low": rng[0],
                  "swing_low": swing_low, "atr20": atr20},
        "m30": m30,
        "minute": minute,
        "vol_ratio": vr, "range_pos": range_pos, "swing_dd": swing_dd},
        "index": idx}


# --------------------------------------------------------------------------- 形态矩阵
# 每个条目: (名称, facts, expect)。expect.ok=False = 合法数据残缺(档位不完整)。

MATRIX: list[tuple[str, dict, dict]] = []

# ========== 族 A:趋势·多头(均线发散向上)==========

# A1 蓄势:多头排列、无放量、回踩中(§8.1 趋势→蓄势)
#   下方需有 支撑(均线) + 止损类(近20日低/阶段低) 才能出完整档位
MATRIX.append(("A1-蓄势",
               mk(1.10, {5: 1.15, 10: 1.18, 20: 1.20, 60: 1.05},
                  m30_ma={5: 1.08, 10: 1.09, 20: 1.10},
                  rng=(0.95, 1.30), swing_low=0.88, atr20=0.03,
                  low=1.04, vr=None, kdj_j=50),
               {"ok": True}))

# A2 右侧初现:均线发散 + 放量站上近20日上沿(§8.1)
#   上方需有压力位(M60 高于现价)→ 突破档=结构位,full 四档
MATRIX.append(("A2-右侧初现",
               mk(1.21, {5: 1.15, 10: 1.18, 20: 1.20, 60: 1.28},
                  m30_ma={5: 1.15, 10: 1.16, 20: 1.17, 60: 1.25},
                  rng=(0.95, 1.20), swing_low=0.88, atr20=0.03,
                  low=1.05, vr=2.0),
               {"ok": True}))

# ========== 族 B:区间震荡(均线粘合)==========

# B1 中部:粘合 + 平量 + 区间中部(§8.1 区间震荡→中部)
MATRIX.append(("B1-区间中部",
               mk(1.10, {5: 1.102, 10: 1.100, 20: 1.098}, rng=(1.00, 1.20),
                  range_pos=50, vr=None),
               {"ok": True}))

# B2 上沿收敛:粘合 + 平量 + 区间 70%+
MATRIX.append(("B2-上沿收敛",
               mk(1.10, {5: 1.102, 10: 1.100, 20: 1.098}, rng=(1.00, 1.20),
                  range_pos=80, vr=None),
               {"ok": True}))

# B3 下沿测试:粘合 + 平量 + 区间 ≤30%
MATRIX.append(("B3-下沿测试",
               mk(1.10, {5: 1.102, 10: 1.100, 20: 1.098}, rng=(1.00, 1.20),
                  range_pos=20, vr=None),
               {"ok": True}))

# B4 突破异动日:粘合 + 放量站上上沿(首日)(§8.1)
#   同上:上方 M60 高于现价,保证四档完整
MATRIX.append(("B4-突破异动日",
               mk(1.21, {5: 1.102, 10: 1.100, 20: 1.098, 60: 1.28},
                  m30_ma={5: 1.15, 10: 1.16, 20: 1.17, 60: 1.25},
                  rng=(0.95, 1.20), swing_low=0.88, atr20=0.03,
                  low=1.05, vr=2.0),
               {"ok": True}))

# B5 变盘前兆:粘合 + 放量未站上(§8.1)
MATRIX.append(("B5-变盘前兆",
               mk(1.10, {5: 1.102, 10: 1.100, 20: 1.098}, rng=(1.0, 1.2), vr=2.0),
               {"ok": True}))

# ========== 族 C:破位/空头 ==========

# C1 破位:放量跌破 M20(§8.1 破位→破位退出)
MATRIX.append(("C1-放量破位",
               mk(1.12, {5: 1.05, 10: 1.10, 20: 1.15}, vr=2.0),
               {"ok": True}))

# C2 空头无支撑:均线全在价上 + 无 m30/日内低 → no_pullback 三档(8.24 光伏退化)
MATRIX.append(("C2-空头三档",
               mk(0.842, {5: 0.856, 10: 0.864, 20: 0.850, 60: 0.918,
                          100: 0.982, 120: 1.006, 250: 0.988},
                  rng=(0.791, 0.894), swing_low=0.740, atr20=0.022),
               {"ok": True}))

# ========== 族 D:左侧/超跌(趋势场景·超跌家族)==========

# D1 超跌试多:J<10 + 恐慌证据 + 位置到位 + 大盘不拦(§4.2 全准入)
MATRIX.append(("D1-超跌试多",
               mk(1.08, {5: 1.00, 10: 0.97, 20: 0.95}, rng=(1.00, 1.15),
                  vr=2.0, chg=-6.0, kdj_j=5.0, swing_dd=20.0,
                  swing_low=1.05, atr20=0.020),
               {"ok": True}))

# D2 左侧观望:超跌但无恐慌证据(平量)(§4.2 缺项)
MATRIX.append(("D2-左侧观望",
               mk(1.08, {5: 1.00, 10: 0.97, 20: 0.95}, rng=(1.00, 1.15),
                  vr=None, chg=-2.0, kdj_j=5.0, swing_dd=20.0,
                  swing_low=1.05, atr20=0.020),
               {"ok": True}))

# ========== 族 E:止损档 v2.8 分支(2026-08-29)==========

# E1 止损正常:cut 以下正好有近20日低(主路径)
MATRIX.append(("E1-止损近沿",
               mk(1.00, {5: 0.99, 10: 1.01, 20: 0.97, 60: 0.95},
                  m30_ma={5: 0.98, 10: 0.985, 20: 0.99},
                  rng=(0.96, 1.10), swing_low=0.80, atr20=0.01, low=0.975,
                  m30_range=(0.95, 1.05)),
               {"ok": True}))

# E2 止损类过远:近20日低距 cut >3×ATR20 → 兜底最近支撑(30分区间低)
MATRIX.append(("E2-止损兜底远",
               mk(1.00, {5: 0.99, 10: 1.01, 20: 0.97, 60: 0.95},
                  m30_ma={5: 0.98, 10: 0.985, 20: 0.99},
                  rng=(0.90, 1.10), swing_low=0.75, atr20=0.010, low=0.975,
                  m30_range=(0.95, 1.05)),
               {"ok": True}))

# E3 无止损类:recent_low/swing_low 均缺失 → 支撑类兜底
MATRIX.append(("E3-无止损类",
               mk(1.00, {5: 0.99, 10: 1.01, 20: 0.97, 60: 0.95},
                  m30_ma={5: 0.98, 10: 0.985, 20: 0.99},
                  rng=(None, 1.10), swing_low=None, atr20=0.010, low=0.975,
                  m30_range=(0.95, 1.05)),
               {"ok": True}))

# E4 无回踩结构止损:no_pullback + 止损类在 cut 下(最近止损类)
MATRIX.append(("E4-空头止损",
               mk(1.00, {5: 1.05, 10: 1.08, 20: 1.10, 60: 1.12},
                  rng=(0.90, 1.20), swing_low=0.80, atr20=0.01),
               {"ok": True}))

# E5 止损兜底后仍有真实空间(校验⑤):支撑与 cut 空间 ≥ max(0.5%, ATR20)
MATRIX.append(("E5-兜底空间",
               mk(1.00, {5: 0.99, 10: 1.01, 20: 0.97, 60: 0.95},
                  m30_ma={5: 0.98, 10: 0.985, 20: 0.99},
                  rng=(0.94, 1.10), swing_low=0.70, atr20=0.010, low=0.975,
                  m30_range=(0.95, 1.05)),
               {"ok": True}))

# ========== 族 F:数据残缺/边界(预期 ok=False)==========

# F1 现价缺失 → 档位不完整
MATRIX.append(("F1-现价缺失",
               mk(None, {5: 1.15, 10: 1.18, 20: 1.20}),
               {"ok": False}))

# F2 无上方结构/无支撑(joint 数据残缺)→ 档位不完整
MATRIX.append(("F2-档位不足",
               mk(1.00, {5: 1.05, 10: 1.08, 20: 1.10}),
               {"ok": False}))


# ===========================================================================
# sanitize_v2 注入矩阵(规则 0-25 每条一个正例一个反例)
# 结构: (名称, {analysis, snapshot, state, anchors, must_keep[], must_remove[],
#                expect_degraded})
# ===========================================================================

# 基准快照(含大盘两维行 + 盘口 + 盘面状态,供 sanitize 各规则解析)
SNAP_SANITIZE = (
    "## 标的\n"
    "| 分时 | 现价 | 0.838 (-0.48%) |\n"
    "| 分时 | 日内高低 | 0.843@13:16 / 0.822 (现价距日内最高回撤 0.59%·日内分位 76%(高位区)) |\n"
    "| 量能 | 量能 | 量比 0.73(缩量); 换手 2.43% |\n"
    "| 日线 | 位置 | 距阶段高点(近250日) 1.198@2026-03-11 回撤 30.05%;近20日区间 "
    "0.791-0.894,现价位于区间 46%·中下轨 |\n"
    "| 日线 | 均线 | M5=0.845 M10=0.861 M20=0.851 M60=0.914 M100=0.975 M120=0.997 M250=0.989 |\n"
    "| 日线 | KDJ | K=32.3 D=36.05 J=24.8(中性·空排·三线收敛) |\n"
    "| 30分 | 均线 | M5=0.837 M10=0.836 M20=0.840 |\n"
    "| 30分 | KDJ | K=27.53 D=40.99 J=0.61(超卖·空排·J深低于K·反弹修复积蓄) |\n"
    "| 30分 | MACD | DIF=0.001 DEA=0.0 柱=0.001(红柱·柱扩大·穿越) |\n"
    "| 盘口 | 外内盘 | 抛压占优(内盘大): 外盘 78.38万手 内盘 79.09万手(来源:腾讯) |\n"
    "| 资金 | 份额 | 67.61亿份(2026-06-30); 较上期 -35.48亿份(-34.42%) |\n"
    "| 相对强弱 | 近5日 | 标的 vs 大盘 -3.31pp(跑输·大幅) |\n"
    "## 大盘\n"
    "| 大盘 | 趋势维度(程序) | 跌破 M20(现价 0.838 vs M20 0.851) |\n"
    "| 大盘 | 动能维度(程序) | DIF -0.006 零下 · DEA -0.008 零下(绿柱·柱缩小·零下) |\n"
    "## 盘面状态(程序输出,LLM 不得修改)\n"
    "| 操作参数(程序锚点) | 突破加仓位 0.894(近20日上沿) / 反弹压力带 0.845-0.851 / "
    "回踩加仓位 0.836-0.837 / 减仓 跌破0.836 / 清仓止损 0.740 |"
)

STATE_SANITIZE = {"state_word": "左侧观望", "scenario": "趋势"}

ANCHORS_SANITIZE = {"anchor_price": 0.838, "ok": True,
                    "breakout_add": 0.894, "breakout_src": "近20日上沿",
                    "breakout_is_ma": False, "no_pullback": False,
                    "pullback_add": 0.836, "cut_loss": 0.836, "cut_src": "日内低",
                    "stop_loss": 0.740, "stop_src": "阶段低", "resist_band": []}

S_CASES: list[tuple[str, dict]] = []

# S1 禁词:单日截面禁"右侧确认"(规则 4/BANNED_WORDS)
S_CASES.append(("S1-禁词右侧确认", {
    "analysis": "## 七、市场状态\n右侧确认,趋势良好。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["建议类别"], "must_remove": ["右侧确认"], "expect_degraded": False}))

# S2 动词方向矛盾:现价 0.838 上方价 0.894 不得"回踩"(规则 3)
S_CASES.append(("S2-动词方向", {
    "analysis": "## 八、操作建议\n若回踩 0.894 企稳可加仓。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["建议类别"], "must_remove": ["回踩 0.894"], "expect_degraded": False}))

# S3 价位白名单:0.850 不在快照/锚点 → 删句(规则 2 单违;0.836 在别行保留)
S_CASES.append(("S3-价位白名单", {
    "analysis": "## 九、风险提示\n若跌破 0.850 则减仓。\n支撑看 0.836。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["0.836"], "must_remove": ["0.850"], "expect_degraded": False}))

# S4 双价违规 → 致命降级(规则 2:≥2 违)
S_CASES.append(("S4-双价降级", {
    "analysis": "## 九、风险提示\n止损 0.700,清仓线 0.699。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": [], "must_remove": [], "expect_degraded": True}))

# S5 风险等级白名单(规则 5)
S_CASES.append(("S5-风险等级", {
    "analysis": "## 四、分时盘口\n风险等级: 极度危险,注意规避。\n正常句保留。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["正常句保留"], "must_remove": ["极度危险"], "expect_degraded": False}))

# S6 盘口外内盘方向反转(规则 7):程序"抛压占优(内盘大)",LLM 写"内盘承接";尾盘缩量分句保留
S_CASES.append(("S6-外内盘方向", {
    "analysis": "## 四、分时盘口\n内盘承接尚可。\n尾盘缩量。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["尾盘缩量"], "must_remove": ["内盘承接"], "expect_degraded": False}))

# S7 MACD 柱色矛盾(规则 8):快照无"绿柱"于大盘,30分是红柱 → 称绿柱删
S_CASES.append(("S7-MACD柱色", {
    "analysis": "## 三、标的·30分\n30分红柱扩大,动能转强。\n绿柱新现,衰退。\n## 建议类别: 观望",
    "snapshot": "## 标的\n| 30分 | MACD | DIF=0.001 DEA=0.0 柱=0.001(红柱·柱扩大·穿越) |\n## 大盘\n",
    "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["红柱扩大"], "must_remove": ["绿柱新现"], "expect_degraded": False}))

# S8 止损缺档写"暂未设定"(规则 9):stop_loss=None → 替换规则句
S_CASES.append(("S8-止损缺档", {
    "analysis": "## 六、操作参数\n止损位暂未设定。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE,
    "anchors": {**ANCHORS_SANITIZE, "stop_loss": None},
    "must_keep": ["止损缺档"], "must_remove": ["暂未设定"], "expect_degraded": False}))

# S9 无回踩结构禁右侧语(规则 10):no_pullback=True 时"突破加仓"删
S_CASES.append(("S9-无回踩结构", {
    "analysis": "## 八、操作建议\n放量突破 0.894 可加仓。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE,
    "anchors": {**ANCHORS_SANITIZE, "no_pullback": True, "pullback_add": None},
    "must_keep": [], "must_remove": ["突破", "加仓"], "expect_degraded": False}))

# S10 大盘压制语境禁"托举"(规则 11):快照需含"受制…上方压力"均线性质行才触发
S_CASES.append(("S10-大盘托举", {
    "analysis": "## 一、大盘环境\n上证指数承压,但短期或有托举。\n## 建议类别: 观望",
    "snapshot": (SNAP_SANITIZE.replace(
        "## 大盘\n| 大盘 | 趋势维度(程序)",
        "## 大盘\n| 日线 | 均线性质 | 受制 M5/M10/M20/M60/M100/M120/M250(上方压力) |\n"
        "| 大盘 | 趋势维度(程序)")),
    "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["建议类别"], "must_remove": ["托举"], "expect_degraded": False}))

# S11 试多越级(规则 12 + _rewrite_pass 步骤3):左侧观望下"可择机试多"改写为 §4.2 观察句
S_CASES.append(("S11-试多越级", {
    "analysis": "## 八、操作建议\n待恐慌出清后可择机试多。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["§4.2", "观察"], "must_remove": ["可择机试多"], "expect_degraded": False}))

# S12 突破触发缺确认词(规则 14):"突破 0.894 加仓"无放量/收盘 → 删
S_CASES.append(("S12-突破确认词", {
    "analysis": "## 八、操作建议\n若突破 0.894,可加仓。\n若放量收盘站上 0.894 则谈加仓。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["放量收盘站上"], "must_remove": ["若突破 0.894,可加仓"], "expect_degraded": False}))

# S13 背离误用(规则 15):"与日线形成背离"删;顶背离保留
S_CASES.append(("S13-背离术语", {
    "analysis": "## 三、标的·30分\n30分超买,与日线形成背离。\n## 九、风险提示\n若创新高而MACD未创新高,构成顶背离。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["顶背离"], "must_remove": ["形成背离"], "expect_degraded": False}))

# S14 位置模糊词(规则 16):46% 中轨区 → "下轨附近"删
S_CASES.append(("S14-位置模糊", {
    "analysis": "## 二、标的·日线\n处于近20日区间下轨附近,反弹空间有限。\n近20日区间 46% 位运行。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["46%"], "must_remove": ["下轨附近"], "expect_degraded": False}))

# S15 日内模糊(规则 17):"日内高低点附近"删
S_CASES.append(("S15-日内模糊", {
    "analysis": "## 四、分时盘口\n分时现价在日内高低点附近运行。\n尾盘缩量。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["尾盘缩量"], "must_remove": ["高低点附近"], "expect_degraded": False}))

# S16 量比引用(规则 18):快照 0.73,写"量比0.54%"删
S_CASES.append(("S16-量比单位", {
    "analysis": "## 三、标的·30分\n量比0.54%,明显缩量。\n量比 0.73,为盘中缩量状态。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["0.73"], "must_remove": ["0.54%"], "expect_degraded": False}))

# S17 操作价位=盘口价(规则 19):买4 0.835 不得作失效位;盘口行须独立一行
S_CASES.append(("S17-盘口价当操作位", {
    "analysis": "## 八、操作建议\n失效=0.835,盘中破位即减。\n## 四、分时盘口\n买4 0.835/1.22万手挂单。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE + "\n| 盘口 | 五档 | 买4 0.835/1.22万手 |\n",
    "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["买4 0.835"], "must_remove": ["失效=0.835"], "expect_degraded": False}))

# S18 大盘两维矛盾(规则 20):趋势跌破 M20 + 双零下 → "中性偏暖/托举"删
S_CASES.append(("S18-大盘两维", {
    "analysis": "## 一、大盘环境\n上证指数中性偏暖,对标的形成托举。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": [], "must_remove": ["中性偏暖", "托举"], "expect_degraded": False}))

# S19 内外盘结论重复(规则 21):第二句被删/补限定
S_CASES.append(("S19-内外盘复读", {
    "analysis": "## 四、分时盘口\n抛压占优(内盘大)。\n抛压占优(内盘大),注意风险。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["套利机制", "参考性有限"], "must_remove": None, "expect_degraded": False}))

# S20 企稳自造 J 表述(规则 22):"J转负"作企稳删
S_CASES.append(("S20-企稳白名单", {
    "analysis": "## 九、风险提示\n企稳需等J转负或J开口收窄。\n企稳需日线KDJ低位金叉(J<30 K上穿D)。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["低位金叉"], "must_remove": ["J转负"], "expect_degraded": False}))

# S21 指标作主语(规则 23):"30分KDJ回踩0.836"病句删
S_CASES.append(("S21-指标主语", {
    "analysis": "## 三、标的·30分\n30分KDJ回踩0.836后企稳。\n价格回踩0.836后观察。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["价格回踩"], "must_remove": ["KDJ回踩"], "expect_degraded": False}))

# S22 相对强弱方向(规则 24):卡=跑输,LLM 写"跑赢"删
S_CASES.append(("S22-相对强弱", {
    "analysis": "## 一、大盘环境\n标的近5日跑赢大盘3.31pp。\n标的近5日落后大盘3.31pp。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["落后大盘3.31pp"], "must_remove": ["跑赢大盘3.31pp"], "expect_degraded": False}))

# S23 KDJ 标签一致性(规则 25):快照中性,写"日线KDJ超卖"删
S_CASES.append(("S23-KDJ标签", {
    "analysis": "## 二、标的·日线\n日线KDJ超卖,超跌蓄势。\n日线KDJ中性,三线收敛。\n## 建议类别: 观望",
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["日线KDJ中性"], "must_remove": ["日线KDJ超卖"], "expect_degraded": False}))

# S24 合法句零误杀(反例):全部合法 → 不删不降级
S_CASES.append(("S24-零误杀", {
    "analysis": ("## 一、大盘环境\n大盘趋势跌破 M20,动能双线零下,系统性压制。\n"
                 "## 二、标的·日线\n现价位于近20日区间 46% 位,KDJ中性。\n"
                 "## 三、标的·30分\n30分KDJ超卖(J=0.61),短期或有修复。\n"
                 "## 四、分时盘口\n尾盘缩量,量比 0.73 平量。\n"
                 "## 五、关键价位\n上方压力 0.894(近20日上沿),下方支撑 0.836。\n"
                 "## 六、操作参数\n突破加仓位 0.894 (程序锚点)\n"
                 "## 七、市场状态\n左侧观望\n## 建议类别: 观望"),
    "snapshot": SNAP_SANITIZE, "state": STATE_SANITIZE, "anchors": ANCHORS_SANITIZE,
    "must_keep": ["左侧观望", "0.894", "46%"], "must_remove": [], "expect_degraded": False}))


if __name__ == "__main__":
    print(f"MATRIX: {len(MATRIX)} 形态; SANITIZE_CASES: {len(S_CASES)} 用例")
    for n, _, _ in MATRIX:
        print("  M", n)
    for n, _ in S_CASES:
        print("  S", n)