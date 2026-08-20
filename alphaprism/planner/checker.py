"""核对引擎(产品方案 §5.1 / §5.3,里程碑3)。

读 RuleModel 的 levels + rules(仅 computable)× 实时行情 → 每只标的的
**结论词 5 档**(平静/接近买点/接近卖点/等待·缺条件/买点触发)+ 大盘门控。

设计约束(与 §4.1 一致):
- 只核对"可计算"部分(levels 价位 × 现价 × 量能 × 大盘门控)。
  不可计算条件(情绪面/消息面)不进这里,由快照层(里程碑后续)处理。
- 大盘门控:作战地图硬规则(如"大盘 J<60 → 所有 ETF 不加仓")命中时,
  自动把所有"接近买点"降级为"等待·缺条件"(§5.1 大盘灯联动)。
- 引擎只算事实,不下单;结论词供人决定。

结论词判定(§5.1 表格):
| 结论词      | 颜色 | 判定 |
| 平静        | 灰   | 远离所有触发点 |
| 接近买点    | 黄   | 距买区/突破点近,且条件满足 |
| 接近卖点    | 橙   | 贴近减仓红线/生命线 |
| 等待·缺条件 | 紫   | 接近买点但被禁(大盘破位/未企稳/信号不满足) |
| 买点触发    | 红闪 | 条件全满足,该动手(人决定) |
另:跌破生命线 → "破位"(清仓风险,最高优先级)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .rulemodel import Instrument, RuleModel

# 距买区下沿/突破点 1.5% 内算"接近买点"
# (作战地图里买区与减仓红线常相邻,如 化工 买区0.855-0.860 红线0.85 仅差0.6%,
#  窗口不能太大,否则买卖侧互相吞没)
BUY_NEAR_PCT = 0.015
VOL_UP = 1.5          # 放量阈值(时间调整量比)
VOL_DOWN = 0.8        # 缩量阈值
GATE_J_OPEN = 60.0    # 大盘 J 值 ≥ 60 → 门控开放(可加仓);< 60 → 关闭

# 结论词常量
CALM = "平静"
NEAR_BUY = "接近买点"
NEAR_SELL = "接近卖点"
WAIT = "等待·缺条件"
BUY_HIT = "买点触发"
BREAK = "破位"

# 结论词 → 颜色/标签(前端渲染用)
CONCLUSION_STYLE = {
    CALM: ("t-normal", "灰"),
    NEAR_BUY: ("t-near", "黄"),
    NEAR_SELL: ("t-warn", "橙"),
    WAIT: ("t-hot", "紫"),
    BUY_HIT: ("t-hit", "红"),
    BREAK: ("t-hit", "红"),
}


@dataclass
class Verdict:
    """单只标的的核对结论。"""
    code: str
    name: str
    type: str = ""                  # 波段仓 | 解套仓
    price: float | None = None
    change_pct: float | None = None
    vol_ratio: float | None = None  # 时间调整量比
    vol_label: str = ""             # 放量/缩量/平量
    conclusion: str = CALM
    gate: bool = True               # 大盘门控是否开放(True=可加仓)
    near: str = ""                  # 接近什么(买区/突破点/减仓红线/生命线)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        style, color = CONCLUSION_STYLE.get(self.conclusion, ("t-normal", "灰"))
        return {
            "code": self.code, "name": self.name, "type": self.type,
            "price": self.price, "change_pct": self.change_pct,
            "vol_ratio": self.vol_ratio, "vol_label": self.vol_label,
            "conclusion": self.conclusion, "color": color, "style": style,
            "gate": self.gate, "near": self.near, "reasons": self.reasons,
        }


# ---------------------------------------------------------------- 量能标签

def vol_label(vr: float | None) -> str:
    """量比 → 放量/缩量/平量(阈值复用 monitor:>1.5 放量、<0.8 缩量)。"""
    if vr is None:
        return ""
    if vr >= VOL_UP:
        return "放量"
    if vr <= VOL_DOWN:
        return "缩量"
    return "平量"


# ---------------------------------------------------------------- 价位提取

def _level(instr: Instrument, *names: str) -> float | None:
    """按名称取第一个价位(买区下沿/突破点/减仓红线/生命线…)。"""
    for lv in instr.levels:
        if lv.name in names:
            return lv.price
    return None


def _buy_zone(instr: Instrument) -> tuple[float | None, float | None]:
    return _level(instr, "买区下沿"), _level(instr, "买区上沿")


# ---------------------------------------------------------------- 核心判定

def classify(price: float | None, buy_lo: float | None, buy_hi: float | None,
             breakout: float | None, redline: float | None, lifeline: float | None,
             vr: float | None, gate_open: bool) -> tuple[str, str]:
    """核心结论词判定(纯函数,可测)。

    返回 (结论词, 接近什么)。判定优先级:
      破位(跌破生命线) > 买区/突破点(买侧) > 接近卖点(跌出买区后贴近红线) > 平静。
    买侧优先:价格在买区/突破点附近时按买侧判,不因红线接近而误判卖点
    (作战地图里买区与减仓红线常相邻,如 化工 买区0.855-0.860 红线0.85)。
    """
    if price is None:
        return CALM, ""
    # 1) 跌破生命线 → 破位(清仓风险,最高优先级)
    if lifeline and price < lifeline:
        return BREAK, f"生命线 {lifeline}"

    # 2) 买侧核心:真正进入买区 / 贴近突破点(对称窗口,突破后明显远离不算"接近")
    in_buy_zone = bool(buy_lo and buy_hi and buy_lo <= price <= buy_hi)
    at_breakout = bool(breakout
                       and breakout * (1 - BUY_NEAR_PCT) <= price <= breakout * (1 + BUY_NEAR_PCT))

    if in_buy_zone:
        confirm = vr is not None and vr <= VOL_DOWN   # 缩量企稳
        near = f"买区 {buy_lo}-{buy_hi}"
    elif at_breakout:
        confirm = vr is not None and vr >= VOL_UP     # 放量突破
        near = f"突破点 {breakout}"
    else:
        # 3) 卖侧:跌破减仓红线(硬线)→ 接近卖点
        if redline and price <= redline:
            return NEAR_SELL, f"减仓红线 {redline}"
        # 4) 接近买区(买区下沿下方 1.5% 内,且未破红线)
        near_buy_zone = bool(buy_lo and buy_hi
                             and buy_lo * (1 - BUY_NEAR_PCT) <= price < buy_lo)
        if not near_buy_zone:
            return CALM, ""
        confirm = False
        near = f"买区 {buy_lo}-{buy_hi}"

    if confirm and gate_open:
        return BUY_HIT, near
    if gate_open:
        return NEAR_BUY, near
    return WAIT, near


def check_instrument(instr: Instrument, price: float | None, vr: float | None,
                     gate_open: bool, change_pct: float | None = None) -> Verdict:
    """单只标的核对。返回 Verdict(不抛异常)。"""
    buy_lo, buy_hi = _buy_zone(instr)
    breakout = _level(instr, "突破点")
    redline = _level(instr, "减仓红线")
    lifeline = _level(instr, "生命线")
    conclusion, near = classify(price, buy_lo, buy_hi, breakout, redline, lifeline, vr, gate_open)
    reasons = []
    if not instr.levels:
        reasons.append("无关键价位(未计划)")
    elif price is None:
        reasons.append("无实时行情")
    v = Verdict(
        code=instr.code, name=instr.name, type=instr.type,
        price=price, change_pct=change_pct, vol_ratio=vr,
        vol_label=vol_label(vr), conclusion=conclusion,
        gate=gate_open, near=near, reasons=reasons,
    )
    return v


# ---------------------------------------------------------------- 大盘门控

def kdj_j(closes: list[float], n: int = 9) -> float | None:
    """标准 9 日 KDJ 的 J 值(用于大盘门控:J<60 → 关闭)。

    收盘价序列(至少 n 根)。K/D 用平滑因子 1/3。返回最新 J;数据不足返回 None。
    """
    if len(closes) < n:
        return None
    k, d = 50.0, 50.0
    for i in range(len(closes) - n + 1):
        window = closes[i:i + n]
        ln = min(window)
        hn = max(window)
        rsv = (closes[i + n - 1] - ln) / (hn - ln) * 100 if hn > ln else 50.0
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    return 3 * k - 2 * d


def gate_open_from_index_closes(closes: list[float], gate_rule_condition: str | None = None) -> bool:
    """按大盘门控规则判门控是否开放。

    规则条件含"J 值回落到 60 以下"→ 用 J 值;无法解析/无数据 → 默认开放(不误伤)。
    返回 True=可加仓,False=大盘破位禁买。
    """
    if not closes or not gate_rule_condition:
        return True
    if "J" not in gate_rule_condition:
        return True
    j = kdj_j(closes)
    if j is None:
        return True
    return j >= GATE_J_OPEN
