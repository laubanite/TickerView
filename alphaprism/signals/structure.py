"""结构状态与关键价位(设计方案.md §3.4)。

- 结构状态:最近上升笔高点对比 + 下降笔低点对比 → 上升 / 下降 / 震荡
- 关键价位:最近顶分型高点 = 压力 / 突破位;最近底分型低点 = 支撑 / 防守位
- 震荡区间(简版中枢):最近 2~3 笔价格区间重叠 → [下沿, 上沿]
"""
from __future__ import annotations

from .fractal import Stroke


def structure_state(strokes: list[Stroke], lookback: int = 6) -> dict:
    """结构状态:上升 / 下降 / 震荡 / 结构未明。"""
    if len(strokes) < 4:
        return {"state": "结构未明", "detail": f"仅 {len(strokes)} 笔,不足判断"}
    ups = [s for s in strokes[-lookback:] if s.direction == "up"]
    downs = [s for s in strokes[-lookback:] if s.direction == "down"]
    if len(ups) < 2 or len(downs) < 2:
        return {"state": "结构未明", "detail": "近窗口上升/下降笔不足"}

    higher_high = ups[-1].end.high > ups[-2].end.high
    higher_low = downs[-1].end.low > downs[-2].end.low
    lower_high = ups[-1].end.high < ups[-2].end.high
    lower_low = downs[-1].end.low < downs[-2].end.low

    if higher_high and higher_low:
        state = "上升"
    elif lower_high and lower_low:
        state = "下降"
    else:
        state = "震荡"

    return {
        "state": state,
        "last_up_high": ups[-1].end.high,
        "prev_up_high": ups[-2].end.high,
        "last_down_low": downs[-1].end.low,
        "prev_down_low": downs[-2].end.low,
    }


def key_levels(strokes: list[Stroke]) -> dict:
    """最近顶分型高点 = 压力 / 突破位;最近底分型低点 = 支撑 / 防守位。"""
    if not strokes:
        return {"support": None, "resistance": None}
    last = strokes[-1]
    if last.direction == "up":
        resistance = last.end.high  # 顶
        prev_down = next((s for s in reversed(strokes[:-1]) if s.direction == "down"), None)
        support = prev_down.end.low if prev_down else last.start.low
    else:
        support = last.end.low  # 底
        prev_up = next((s for s in reversed(strokes[:-1]) if s.direction == "up"), None)
        resistance = prev_up.end.high if prev_up else last.start.high
    return {"support": support, "resistance": resistance}


def range_box(strokes: list[Stroke], count: int = 3) -> dict | None:
    """简版中枢:最近 count 笔的价格区间重叠 → [下沿, 上沿];无重叠返回 None。"""
    recent = strokes[-count:]
    if len(recent) < 2:
        return None
    lows = [min(s.start.low, s.end.low) for s in recent]
    highs = [max(s.start.high, s.end.high) for s in recent]
    lower = max(lows)
    upper = min(highs)
    if upper > lower:
        return {"lower": lower, "upper": upper}
    return None


def summarize(strokes: list[Stroke]) -> dict:
    """结构概要:状态 + 关键价位 + 中枢 + 最近笔。"""
    return {
        "state": structure_state(strokes),
        "levels": key_levels(strokes),
        "range": range_box(strokes),
        "last_stroke": _describe_stroke(strokes[-1]) if strokes else None,
    }


def _describe_stroke(s: Stroke) -> dict:
    val = s.end.low if s.end.ftype == "bottom" else s.end.high
    return {
        "direction": s.direction,
        "from_date": s.start.date,
        "to_date": s.end.date,
        "end_type": s.end.ftype,
        "end_value": val,
    }


# --------------------------------------------------------------------------- 近端关键位(分层方案 #2,2026-08-17)

MA_PERIODS = (5, 10, 20, 60, 120, 250)   # 均线系统(用户确认全周期)
SWING_WINDOW = 60        # 近端前高/前低窗口(交易日)
VOL_ZONE_WINDOW = 120    # 密集成交区窗口
VOL_BIN_PCT = 0.01       # 密集区箱宽(现价 1%)
GAP_WINDOW = 60          # 缺口扫描窗口
GAP_MAX = 3              # 最多展示最近缺口数


def _recent_ma(df: pd.DataFrame) -> dict:
    """均线 MA5/10/20/60/120/250(仅用 <= 末尾的数据;样本不足返回 None)。"""
    closes = df["close"]
    return {p: round(float(closes.tail(p).mean()), 4) if len(closes) >= p else None
            for p in MA_PERIODS}


def _swing_levels(df: pd.DataFrame, fractals: list[Fractal], close: float) -> dict:
    """近端前高/前低:近 SWING_WINDOW 交易日内、已确认(非最新一根)的分型极值。
    支撑 = 现价下方最近的底分型低点;压力 = 现价上方最近的顶分型高点。
    返回 {"support": (值, 日期) 或 None, "resistance": (值, 日期) 或 None}。"""
    dates = list(df["trade_date"])
    window_dates = set(dates[-SWING_WINDOW:]) if len(dates) >= SWING_WINDOW else set(dates)
    last_date = dates[-1] if dates else ""
    bottoms = [f for f in fractals
               if f.ftype == "bottom" and f.date in window_dates
               and f.date < last_date and f.low < close]
    tops = [f for f in fractals
            if f.ftype == "top" and f.date in window_dates
            and f.date < last_date and f.high > close]
    support = max(bottoms, key=lambda f: f.low) if bottoms else None
    resistance = min(tops, key=lambda f: f.high) if tops else None
    return {
        "support": (round(float(support.low), 4), support.date) if support else None,
        "resistance": (round(float(resistance.high), 4), resistance.date) if resistance else None,
    }


def _volume_zone(df: pd.DataFrame, close: float) -> dict | None:
    """密集成交区(近似):近 VOL_ZONE_WINDOW 日成交量加权价格分布峰区。
    ⚠️ ETF 无真实筹码,仅供量能聚集带参考。样本过少/区间过窄返回 None。"""
    s = df.tail(VOL_ZONE_WINDOW)
    if len(s) < 20:
        return None
    vol = s["volume"].to_numpy(dtype=float)
    px = s["close"].to_numpy(dtype=float)
    lo, hi = float(px.min()), float(px.max())
    if hi <= lo or vol.max() <= 0:
        return None
    width = max(close * VOL_BIN_PCT, (hi - lo) / 40)  # 至少 40 箱
    import numpy as np
    n_bins = int(np.ceil((hi - lo) / width)) + 1
    hist = np.zeros(n_bins)
    for i in range(len(px)):
        b = min(int((px[i] - lo) / width), n_bins - 1)
        hist[b] += vol[i]
    peak = int(hist.argmax())
    peak_w = hist[peak]
    if peak_w <= 0:
        return None
    i, j = peak, peak
    while i > 0 and hist[i - 1] >= 0.5 * peak_w:
        i -= 1
    while j < n_bins - 1 and hist[j + 1] >= 0.5 * peak_w:
        j += 1
    return {
        "lower": round(lo + i * width, 4),
        "upper": round(lo + (j + 1) * width, 4),
        "peak_bins": j - i + 1,
    }


def _recent_gaps(df: pd.DataFrame) -> list[dict]:
    """近 GAP_WINDOW 日跳空缺口(上跳/下跳);ETF 少见,最多 GAP_MAX 个。"""
    s = df.tail(GAP_WINDOW + 1)
    if len(s) < 2:
        return []
    gaps = []
    for i in range(1, len(s)):
        prev_h, prev_l = float(s["high"].iloc[i - 1]), float(s["low"].iloc[i - 1])
        h, l = float(s["high"].iloc[i]), float(s["low"].iloc[i])
        if l > prev_h:
            gaps.append({"type": "up", "low": round(prev_h, 4), "high": round(l, 4),
                         "date": s["trade_date"].iloc[i]})
        elif h < prev_l:
            gaps.append({"type": "down", "low": round(h, 4), "high": round(prev_l, 4),
                         "date": s["trade_date"].iloc[i]})
    return gaps[-GAP_MAX:]


def near_levels(df: pd.DataFrame, fractals: list[Fractal] | None = None,
                idx: int | None = None) -> dict:
    """近端关键位(分层方案):均线 + 近端前高前低 + 密集成交区 + 缺口。
    只用 <= idx 的数据(点内);idx 省略 = 最新一根。供信号锚点与展示用。"""
    window = df.iloc[:idx + 1] if idx is not None else df
    if window.empty:
        return {"ma": {}, "swing": {"support": None, "resistance": None},
                "volume_zone": None, "gaps": []}
    fractals = fractals or find_fractals(merge_bars(window))
    close = float(window["close"].iloc[-1])
    return {
        "ma": _recent_ma(window),
        "swing": _swing_levels(window, fractals, close),
        "volume_zone": _volume_zone(window, close),
        "gaps": _recent_gaps(window),
    }
