"""简版买卖点 + 量价确认(设计方案.md §3.5-3.6, §4;阶段3 依《量价关系》修正;阶段6 分层关键位)。

分层关键位(2026-08-17,#2):
- 结构位(笔端点)= 大级别攻守 / 失效判据(跌破结构支撑=破位)
- 近端位(均线/前高前低/密集区/缺口)= 买卖点锚点,信号跟随价格动
- 买点(观察)= 回踩近端支撑(±2%)+ 结构位未破位;激活 = 放量反包(R1)
- 卖点(观察)= 触及近端压力(±2%)+ 结构位未破位;激活 = 量价确认(量增价不涨≥4日/高换手>10%/MACD顶背离/M头破位)
- 底背离仅观察,不单独作买点
"""
from __future__ import annotations

import pandas as pd

from . import structure as struct
from . import volprice

NEAR_PCT = 0.02  # 关键位容差


def _bullish_engulf(df: pd.DataFrame, idx: int) -> bool:
    if idx == 0:
        return False
    return float(df["close"].iloc[idx]) > float(df["high"].iloc[idx - 1])


def detect_signal(df: pd.DataFrame, strokes: list, idx: int, fractals: list | None = None) -> dict:
    """对第 idx 根 K 线判定信号(只用到 <= idx 的数据)。"""
    date = df["trade_date"].iloc[idx]
    close = float(df["close"].iloc[idx])

    confirmed = [s for s in strokes if s.end.date <= date]
    base = {"date": date, "close": close, "setup": "观望", "confirm_buy": False,
            "confirm_sell": False, "support": None, "resistance": None,
            "structure_support": None, "structure_resistance": None, "near": None}
    if not confirmed:
        base["note"] = "尚无已确认的笔"
        return base
    last = confirmed[-1]

    # 分层关键位(2026-08-17):结构位(笔端点)=大级别攻守/失效判据;近端位(均线/前高前低/密集区)=买卖点锚点
    levels = struct.key_levels(confirmed)
    structure_support, structure_resistance = levels["support"], levels["resistance"]
    near = struct.near_levels(df, fractals, idx)
    near_support, near_resistance = near["swing"]["support"], near["swing"]["resistance"]

    engulf = _bullish_engulf(df, idx)
    vp = volprice.vp_indicators(df, idx, confirmed, fractals)
    is_vol_up = vp["is_vol_up"]

    # 买点 = 回踩近端支撑;卖点 = 触及近端压力;两者同时触发取距离更近者
    setup = "观望"
    note = "价格离开近端关键位"
    if near_support and close <= near_support[0] * (1 + NEAR_PCT):
        setup = "买点"
        note = (f"跌破近端支撑 {near_support[0]:.3f}(买点失效)"
                if close < near_support[0] * (1 - NEAR_PCT)
                else f"回踩近端支撑 {near_support[0]:.3f}({near_support[1]})")
    if near_resistance and close >= near_resistance[0] * (1 - NEAR_PCT):
        d_buy = (close - near_support[0]) / near_support[0] if near_support else 1e9
        d_sell = (near_resistance[0] - close) / near_resistance[0]
        if setup == "买点":
            if d_sell < d_buy:
                setup = "卖点"
                note = f"触及近端压力 {near_resistance[0]:.3f}({near_resistance[1]})"
        else:
            setup = "卖点"
            note = f"触及近端压力 {near_resistance[0]:.3f}({near_resistance[1]})"
    # 结构位失效覆盖(大级别破位)
    if setup == "买点" and structure_support and close < structure_support * (1 - NEAR_PCT):
        note = f"跌破结构支撑 {structure_support:.3f}(买点失效)"
    elif setup == "卖点" and structure_resistance and close > structure_resistance * (1 + NEAR_PCT):
        note = f"突破结构压力 {structure_resistance:.3f}(卖点失效)"

    # 量价确认(书中修正):买=放量反包;卖=量增价不涨/高换手/MACD顶背离/M头破位
    # 量价震荡 / 不规则放量 = 量价信号不可信状态,禁用量价确认(书中"状态机禁用其他信号")
    suppress = vp["range_disorder"] or vp["irregular_volume"]
    confirm_buy = (engulf and is_vol_up) and not suppress
    confirm_sell = (
        vp["volume_stall"] or vp["high_turnover"] or vp["macd_top_div"] or vp["m_head_break"]
    ) and not suppress

    extra = []
    if vp["volume_stall"]:
        extra.append("量增价不涨")
    if vp["high_turnover"]:
        extra.append("高换手")
    if vp["macd_top_div"]:
        extra.append("MACD顶背离")
    if vp["m_head_break"]:
        extra.append("M头破位")
    if vp["top_divergence"]:
        extra.append("缩量新高")
    if vp["bottom_divergence"]:
        extra.append("底背离")
    if vp["wave_decline"]:
        extra.append("逐波递减")
    if vp["double_test"]:
        extra.append("二踩确认")
    if vp["coil"]:
        extra.append("收敛预备" + ("上沿带量" if vp["coil_probe"] else ""))
    if vp["round_bottom"]:
        extra.append("圆弧底")
    if vp["round_top"]:
        extra.append("圆弧顶")
    if vp["sky_volume"]:
        extra.append("天量")
    if vp["floor_volume"]:
        extra.append("地量")
    if vp["is_vol_down"]:
        extra.append("缩量")
    if vp["range_disorder"]:
        extra.append("量价震荡(禁量价确认)")
    if vp["irregular_volume"]:
        extra.append("不规则放量(过滤)")
    if extra:
        note += " | " + "/".join(extra)

    # 当前价是否仍落在触发位(供显示/日志)
    at_level = (
        (setup == "买点" and near_support and close <= near_support[0] * (1 + NEAR_PCT))
        or (setup == "卖点" and near_resistance and close >= near_resistance[0] * (1 - NEAR_PCT))
    )

    base.update({
        "setup": setup,
        "confirm_buy": confirm_buy,
        "confirm_sell": confirm_sell,
        # 支撑/压力 = 近端位(可操作);结构位单独暴露(失效判据)
        "support": near_support[0] if near_support else structure_support,
        "resistance": near_resistance[0] if near_resistance else structure_resistance,
        "structure_support": structure_support,
        "structure_resistance": structure_resistance,
        "last_stroke_dir": last.direction,
        "last_stroke_from": last.start.date,
        "last_stroke_to": last.end.date,
        "engulf": engulf,
        "at_level": at_level,
        "near": near,
        "vp": vp,
        "note": note,
    })
    return base


def display_signal(sig: dict) -> str:
    """日报显示用:setup 已随价格锚定近端位,直接返回;跌破触发位标记「已破」。"""
    setup = sig.get("setup", "观望")
    if setup == "观望":
        return "观望"
    close = float(sig.get("close") or 0)
    ns = sig.get("support")
    ss = sig.get("structure_support")
    nr = sig.get("resistance")
    sr = sig.get("structure_resistance")
    if setup == "买点" and (
            (ns and close < ns * (1 - NEAR_PCT)) or (ss and close < ss * (1 - NEAR_PCT))):
        return "买点(已破)"
    if setup == "卖点" and (
            (nr and close > nr * (1 + NEAR_PCT)) or (sr and close > sr * (1 + NEAR_PCT))):
        return "卖点(已破)"
    return setup


def classify_signal(sig: dict) -> tuple[str, str]:
    """信号粗状态 → (signal, state)。state: 观察 / 激活 / 破位 / 无。

    - setup 非观望即有效信号(近端位触发,价格已贴近)
    - confirm 后升级为「激活」(买点=放量反包确认,卖点=量价确认)
    - 跌破近端支撑 / 结构支撑,或突破近端压力 / 结构压力 → 「破位」(三件套的失效条件)
    信号日志与推送用。
    """
    setup = sig.get("setup")
    close = float(sig.get("close") or 0)
    support = sig.get("support")
    resistance = sig.get("resistance")
    structure_support = sig.get("structure_support")
    structure_resistance = sig.get("structure_resistance")
    if setup == "买点":
        if (structure_support and close < structure_support) or (
                support and close < support * (1 - NEAR_PCT)):
            return "买点", "破位"
        return "买点", ("激活" if sig.get("confirm_buy") else "观察")
    if setup == "卖点":
        if (structure_resistance and close > structure_resistance) or (
                resistance and close > resistance * (1 + NEAR_PCT)):
            return "卖点", "破位"
        return "卖点", ("激活" if sig.get("confirm_sell") else "观察")
    return "观望", "无"


def signal_text(sig: dict, state: dict) -> str:
    """三件套输出(§3.6):结构证据 / 触发条件 / 失效条件。"""
    st = state.get("state", "?")
    ssup = f"{sig.get('structure_support'):.3f}" if sig.get("structure_support") else "-"
    sres = f"{sig.get('structure_resistance'):.3f}" if sig.get("structure_resistance") else "-"
    nsup = f"{sig.get('support'):.3f}" if sig.get("support") else "-"
    nres = f"{sig.get('resistance'):.3f}" if sig.get("resistance") else "-"
    vp = sig.get("vp", {})
    vp_note = []
    for name, label in (
        ("is_vol_up", "放量"), ("is_vol_down", "缩量"),
        ("volume_stall", "量增价不涨"), ("high_turnover", "高换手"),
        ("macd_top_div", "MACD顶背离"), ("m_head_break", "M头破位"),
        ("top_divergence", "缩量新高"), ("bottom_divergence", "底背离"),
        ("wave_decline", "逐波递减"), ("double_test", "二踩确认"),
        ("coil", "收敛预备"), ("round_bottom", "圆弧底"), ("round_top", "圆弧顶"),
        ("range_disorder", "量价震荡"), ("irregular_volume", "不规则放量"),
    ):
        if vp.get(name):
            vp_note.append(label)
    vp_s = "/".join(vp_note) if vp_note else f"量价中性(换手{vp.get('turnover_tier', '-')})"

    if sig["setup"] == "买点":
        trigger = "回踩近端支撑 + 放量反包(R1) → 进场"
        invalidate = f"收盘跌破近端支撑 {nsup} 或结构支撑 {ssup} 则放弃"
    elif sig["setup"] == "卖点":
        trigger = "触及近端压力 + 量增价不涨≥4日/高换手>10%/MACD顶背离 → 减仓"
        invalidate = f"放量站上近端压力 {nres} / 结构压力 {sres} 则失效"
    else:
        trigger = "等待价格回到近端支撑/压力位"
        invalidate = "-"

    return (
        f"**结构证据**:{st}结构;{sig.get('last_stroke_dir', '-')}笔 "
        f"{sig.get('last_stroke_from', '-')}→{sig.get('last_stroke_to', '-')}"
        f"(结构支撑 {ssup} / 压力 {sres};近端支撑 {nsup} / 压力 {nres}) | 量价:{vp_s}\n"
        f"**触发条件**:{trigger}\n"
        f"**失效条件**:{invalidate}"
    )
