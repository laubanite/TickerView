"""量价关系指标(设计方案.md §4,阶段3;依据《量价关系》陈凯 提取)。

规则来源:docs/volume-price-rules.md。全部确定性计算,阈值参数化可调。

书中关键修正(2026-08-12):
- 缩量上攻 ≠ 一律卖点(放量整理后缩量=慢牛);缩量仅作观察
- 缩量突破 ≠ 假突破(一字板惜售/慢牛为真);改为"存疑"
- 顶背离主形式 = 量增价不涨(放量滞涨),需持续 4~5 日
- 底背离弱于顶背离,仅观察
"""
from __future__ import annotations

import pandas as pd

# 阈值(与 docs/volume-price-rules.md 一致)
VOL_UP_MULT = 1.5            # 显著放量: 量 > 1.5×20日均量(书中 1.3~2.4 倍区间)
VOL_DOWN_MULT = 0.7          # 显著缩量: 量 < 0.7×20日均量(书中回调到位需到前高量 1/3)
SKY_MULT = 2.0               # 天量: 量 > 2.0×20日均量
FLOOR_MULT = 0.4             # 地量: 量 < 0.4×20日均量
STALL_DAYS = 4               # 量增价不涨(放量滞涨)需持续 ≥4 日(书中 4~5 日)
STALL_PRICE_MOVE = 3.0       # 放量滞涨期间价格波动 <3%
HIGH_TURNOVER_LIMIT = 10.0   # 高位换手率 >10% ≈ 临界(书中)
M_HEAD_VOL_FACTOR = 0.9      # M头: 第二头量 ≤ 第一头量×0.9
LOOKBACK = 30                # 背离比较窗口(日)

# --- 书补充规则阈值(阶段3,与 docs/volume-price-rules.md 一致) ---
WAVE_DECLINE_MIN = 4         # 逐波量能递减: 至少 4 波才比较(要求持续弱化)
WAVE_DECLINE_PAIRS = 3       # 逐波量能递减: 连续 3 次过渡(量,涨幅)双递减=衰竭
WAVE_RECENT = 60             # 逐波量能递减: 最后一波结束须在 60 根内(避免陈旧)
RETEST_VOL_FACTOR = 1.0 / 3  # 二踩图: 回调量缩到前高量 1/3(书中 10亿→3亿)
RETEST_HOLD_TOL = 0.02       # 二踩图: 回踩低点≥第一波高点的容差(允许微幅下穿 2%)
RETEST_RECENT = 60           # 二踩图: 第二波结束须在 60 根内
COIL_SCAN = 12               # 收敛图: 扫描最近分型数
COIL_MIN_TOPS = 3            # 收敛图: 至少 3 个高点(递减)
COIL_MIN_BOTTOMS = 2         # 收敛图: 至少 2 个低点(递增)
COIL_NARROW_RATIO = 0.10     # 收敛图: 末端宽 ≤ 宽端 10%
COIL_MAX_VOL_RATIO = 1.2     # 收敛图: 区间内不放量(段均量 ≤ 前基线×1.2)
COIL_RECENT = 15             # 收敛图: 末端距当前 ≤15 根(避免陈旧)
ROUND_MIN_BARS = 250         # 圆弧底/顶: 跨度 >1 年(约 250 交易日)
ROUND_TROUGH_MARGIN = 0.97   # 圆弧底: 中段均价 < 两端×0.97(中段是明显低谷)
ROUND_CREST_MARGIN = 1.03    # 圆弧顶: 中段均价 > 两端×1.03(中段是明显峰)
ROUND_MID_SHRINK = 0.8       # 圆弧: 中段均量 ≤ 左段×0.8(漫长缩量)
ROUND_RIGHT_VOL_MULT = 1.8   # 圆弧: 右侧段均量 ≥ 中段×1.8(右侧放量,收紧以筛出真圆弧)
ROUND_UP_NEUTRAL = 1.02      # 圆弧顶: 右侧段均价 ≤ 中段×1.02(放量不涨)
RANGE_DISORDER_WINDOW = 60   # 量价震荡: 观察窗口(日)
RANGE_DISORDER_PRICE = 0.10  # 量价震荡: 60日收盘区间宽度 <10%(用收盘价,抗单日毛刺)
RANGE_DISORDER_VOL_RATIO = 10.0  # 量价震荡: 量极差 ≥10 倍(书中 ~10 倍)
IRREG_SPIKE_MULT = 3.0       # 不规则放量: 突放 ≥3 倍(书中 3 倍突放)
IRREG_RECOVER_MULT = 1.5     # 不规则放量: 1~3 日后量比回落 <1.5 倍
IRREG_WINDOW = 5             # 不规则放量: 扫描窗口(日)
M_HEAD_TOL = 0.02            # M头: 第二头高度容差(接近第一头才算 M)
M_HEAD_NECK_BREAK_VOL = 1.5  # M头: 颈线放量破位需量比 ≥1.5 倍
M_HEAD_RECENT = 30           # M头: 第二头结束须在 30 根内(破位是即时动作,收紧防陈旧)


def vol_ratio(df: pd.DataFrame, idx: int, window: int = 20) -> float | None:
    """量比:当日量 / 前 window 日均量。"""
    if idx < window:
        return None
    avg = float(df["volume"].iloc[idx - window:idx].mean())
    return float(df["volume"].iloc[idx]) / avg if avg else None


def is_vol_up(df: pd.DataFrame, idx: int, window: int = 20) -> bool:
    r = vol_ratio(df, idx, window)
    return bool(r is not None and r > VOL_UP_MULT)


def is_vol_down(df: pd.DataFrame, idx: int, window: int = 20) -> bool:
    r = vol_ratio(df, idx, window)
    return bool(r is not None and r < VOL_DOWN_MULT)


def is_sky_volume(df: pd.DataFrame, idx: int, window: int = 20) -> bool:
    """天量: 量 > 2.0×20日均量。"""
    r = vol_ratio(df, idx, window)
    return bool(r is not None and r > SKY_MULT)


def is_floor_volume(df: pd.DataFrame, idx: int, window: int = 20) -> bool:
    """地量: 量 < 0.4×20日均量,或 20 日内最低量。"""
    r = vol_ratio(df, idx, window)
    if not (r is not None and r < FLOOR_MULT):
        return False
    return True


def turnover_tier(turnover: float | None) -> str:
    """换手率分档(书中 W3):<1% 不活跃,1~2% 普通,2~5% 活跃,5~7% 非常活跃,>7% 异常活跃。"""
    if turnover is None:
        return "-"
    if turnover < 1:
        return "不活跃"
    if turnover < 2:
        return "普通"
    if turnover < 5:
        return "活跃"
    if turnover < 7:
        return "非常活跃"
    return "异常活跃"


def is_high_turnover(df: pd.DataFrame, idx: int) -> bool:
    """高位换手率 >10% ≈ 临界点(书中 W18)。"""
    if "turnover" not in df.columns:
        return False
    t = df["turnover"].iloc[idx]
    if t is None or pd.isna(t):
        return False
    return bool(float(t) > HIGH_TURNOVER_LIMIT)


def is_volume_stall(df: pd.DataFrame, idx: int, stall_days: int = STALL_DAYS) -> bool:
    """量增价不涨(放量滞涨,书中 W17 第一层次背离):
    最近 stall_days 日持续放量(日均量 > 1.2×前 20 日均量)但价格几乎不动(波动 <3%)。
    """
    if idx < stall_days + 20:
        return False
    recent = df["volume"].iloc[idx - stall_days + 1:idx + 1]
    base = df["volume"].iloc[idx - stall_days + 1 - 20:idx - stall_days + 1].mean()
    if not base or recent.mean() <= base * 1.2:
        return False
    move = (df["close"].iloc[idx] / df["close"].iloc[idx - stall_days + 1] - 1) * 100
    return bool(abs(move) < STALL_PRICE_MOVE)


def top_divergence(df: pd.DataFrame, idx: int, lookback: int = LOOKBACK) -> bool:
    """价新高但量萎缩(第二层次背离,书中 W15 M头 / 衰竭场景)。

    注意:书中警示此形态可能是慢牛(放量整理后缩量新高),仅作观察不单独触发卖点。
    """
    if idx < lookback:
        return False
    win = df.iloc[idx - lookback:idx + 1]
    highs = win["high"].values
    vols = win["volume"].values
    prev_pos = int(highs[:-1].argmax())
    return bool(highs[-1] > highs[prev_pos] and vols[-1] < vols[prev_pos] * M_HEAD_VOL_FACTOR)


def bottom_divergence(df: pd.DataFrame, idx: int, lookback: int = LOOKBACK) -> bool:
    """价新低但量不再萎缩/放大(书中 R6)。仅观察,弱于顶背离。"""
    if idx < lookback:
        return False
    win = df.iloc[idx - lookback:idx + 1]
    lows = win["low"].values
    vols = win["volume"].values
    prev_pos = int(lows[:-1].argmin())
    return bool(lows[-1] < lows[prev_pos] and vols[-1] >= vols[prev_pos] * 0.8)


def compute_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD:返回 (DIF, DEA)。"""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea


def macd_top_divergence(df: pd.DataFrame, idx: int, lookback: int = LOOKBACK) -> bool:
    """MACD 顶背离(书中附录一 W36): 价创新高 + DIF 未创新高 + 连续 2 根 DIF 向下。"""
    if idx < lookback + 5:
        return False
    dif, _ = compute_macd(df)
    win_h = df["high"].iloc[idx - lookback:idx + 1].values
    win_d = dif.iloc[idx - lookback:idx + 1].values
    prev_pos = int(win_h[:-1].argmax())
    if not (win_h[-1] > win_h[prev_pos] and win_d[-1] < win_d[prev_pos]):
        return False
    return bool(dif.iloc[idx] < dif.iloc[idx - 1] < dif.iloc[idx - 2])


# --------------------------------------------------------------------------- 结构相关量价规则(书补充)

def _stroke_slice(df: pd.DataFrame, stroke) -> pd.DataFrame:
    """笔覆盖的原始 K 线(按起止分型日期切片,含端点)。"""
    return df[
        (df["trade_date"] >= stroke.start.date) & (df["trade_date"] <= stroke.end.date)
    ]


def _bars_since(df: pd.DataFrame, idx: int, date: str) -> int | None:
    """当前根距某日期经过的 K 线根数(未找到返回 None)。"""
    pos = df.index[df["trade_date"] == date]
    return int(idx - pos[0]) if len(pos) else None


def wave_decline(df: pd.DataFrame, strokes: list, idx: int) -> bool:
    """逐波量能递减(书中"逐波量能递减"):连续上涨波次(量,涨幅)双递减=力度衰竭。

    取最近 WAVE_DECLINE_MIN 条上升笔,比较相邻两波的日均量与涨幅;
    连续 WAVE_DECLINE_PAIRS 个过渡均"量减+涨减"→ 衰竭预警。仅观察,不单独触发。
    要求最后一波在 WAVE_RECENT 根内结束,避免陈旧结构反复触发。
    """
    ups = [s for s in strokes if s.direction == "up"]
    if len(ups) < WAVE_DECLINE_MIN:
        return False
    ups = ups[-WAVE_DECLINE_MIN:]
    since = _bars_since(df, idx, ups[-1].end.date)
    if since is None or since > WAVE_RECENT:
        return False
    vols, gains = [], []
    for s in ups:
        seg = _stroke_slice(df, s)
        if seg.empty:
            return False
        vols.append(float(seg["volume"].mean()))
        gains.append(float(s.end.high / s.start.low - 1) * 100)
    declines = 0
    for i in range(len(ups) - 1):
        if vols[i] > vols[i + 1] and gains[i] > gains[i + 1]:
            declines += 1
        else:
            declines = 0
    return declines >= WAVE_DECLINE_PAIRS


def double_test(df: pd.DataFrame, strokes: list, idx: int) -> bool:
    """二踩图(书中二踩图):回调量缩到前高量 1/3 + 回踩低点 ≥ 第一波高点。

    结构:上升笔(第一波)→ 下降笔(回调)→ 再上。回调缩量到前高量 1/3、
    回踩不破第一波高点 = 突破后的健康回踩确认。买点辅助(观察,不独立开仓)。
    要求第二波在 RETEST_RECENT 根内结束。
    """
    ups = [s for s in strokes if s.direction == "up"]
    if len(ups) < 2:
        return False
    since = _bars_since(df, idx, ups[-1].end.date)
    if since is None or since > RETEST_RECENT:
        return False
    first, second = ups[-2], ups[-1]
    downs = [
        s for s in strokes if s.direction == "down"
        and s.start.date >= first.end.date and s.end.date <= second.start.date
    ]
    if not downs:
        return False
    retrace = downs[-1]
    first_seg = _stroke_slice(df, first)
    retrace_seg = _stroke_slice(df, retrace)
    if first_seg.empty or retrace_seg.empty:
        return False
    peak_vol = float(first_seg["volume"].max())  # 前高量 = 第一波峰值日量
    if peak_vol <= 0:
        return False
    retrace_vol = float(retrace_seg["volume"].mean())
    retrace_low = float(retrace.end.low)
    first_high = float(first.end.high)
    ok_vol = retrace_vol <= peak_vol * RETEST_VOL_FACTOR
    ok_hold = retrace_low >= first_high * (1 - RETEST_HOLD_TOL)
    return ok_vol and ok_hold


def coiling(df: pd.DataFrame, fractals: list, idx: int) -> dict:
    """收敛图(书中"收敛图/突破预备"):≥3高2低、末端宽≤10%、区间内不放量、试探上沿带量。

    高递减 + 低递增 = 收敛三角;末端宽/宽端宽 ≤ COIL_NARROW_RATIO;收敛期内段均量
    不超过收敛段前 20 日均量(不放量);最新价贴上沿 + 放量 = 试探上沿带量。
    返回 {"formed", "probe"},均仅观察(突破需另等放量确认)。
    """
    if not fractals or idx < 25:
        return {"formed": False, "probe": False}
    tops = [f for f in fractals if f.ftype == "top"][-COIL_SCAN:]
    bottoms = [f for f in fractals if f.ftype == "bottom"][-COIL_SCAN:]
    if len(tops) < COIL_MIN_TOPS or len(bottoms) < COIL_MIN_BOTTOMS:
        return {"formed": False, "probe": False}
    if not all(tops[i].high > tops[i + 1].high for i in range(len(tops) - 1)):
        return {"formed": False, "probe": False}
    if not all(bottoms[i].low < bottoms[i + 1].low for i in range(len(bottoms) - 1)):
        return {"formed": False, "probe": False}
    wide = float(tops[0].high - bottoms[0].low)
    narrow = float(tops[-1].high - bottoms[-1].low)
    if wide <= 0 or narrow / wide > COIL_NARROW_RATIO:
        return {"formed": False, "probe": False}
    # 收敛段内不放量:段均量 ≤ 段前 20 日均量×COIL_MAX_VOL_RATIO
    seg = df[(df["trade_date"] >= bottoms[0].date) & (df["trade_date"] <= tops[-1].date)]
    pos = df.index[df["trade_date"] == tops[-1].date]
    if seg.empty or len(pos) == 0 or idx - int(pos[0]) > COIL_RECENT:
        return {"formed": False, "probe": False}
    if len(seg) >= 20:
        base = float(seg["volume"].iloc[:20].mean())
        vol_mean = float(seg["volume"].iloc[20:].mean()) if len(seg) > 20 else float(seg["volume"].mean())
    else:
        base = float(df["volume"].iloc[max(0, pos[0] - 20):pos[0]].mean())
        vol_mean = float(seg["volume"].mean())
    if not base or vol_mean > base * COIL_MAX_VOL_RATIO:
        return {"formed": False, "probe": False}
    # 试探上沿带量:最新收盘贴上沿 + 放量
    close = float(df["close"].iloc[idx])
    probe = bool(close >= tops[-1].high * 0.99 and is_vol_up(df, idx))
    return {"formed": True, "probe": probe}


def round_bottom(df: pd.DataFrame, idx: int) -> bool:
    """圆弧底(书中):跨度>1年 + 中段漫长缩量 + 右侧放量回升。仅观察。"""
    if idx < ROUND_MIN_BARS:
        return False
    return _round_shape(df, idx, bottom=True)


def round_top(df: pd.DataFrame, idx: int) -> bool:
    """圆弧顶(书中):跨度>1年 + 右侧放量不涨。仅观察。"""
    if idx < ROUND_MIN_BARS:
        return False
    return _round_shape(df, idx, bottom=False)


def _round_shape(df: pd.DataFrame, idx: int, bottom: bool) -> bool:
    """圆弧判定公共逻辑:窗口三等分(左/中/右)。"""
    win = df.iloc[idx - ROUND_MIN_BARS + 1:idx + 1]
    n = len(win)
    third = n // 3
    left, mid, right = win.iloc[:third], win.iloc[third:2 * third], win.iloc[2 * third:]
    if any(seg.empty for seg in (left, mid, right)):
        return False
    lc, mc, rc = left["close"].mean(), mid["close"].mean(), right["close"].mean()
    lv, mv, rv = left["volume"].mean(), mid["volume"].mean(), right["volume"].mean()
    if mv <= 0 or rv <= 0:
        return False
    if bottom:
        # U 形:中段明显低谷(<两端×0.97);中段是缩量段(≤左段×0.8,且≤右段);
        # 右侧放量(≥中段×1.8)且有效回升(右均价>左均价×1.02)
        if not (mc < lc * ROUND_TROUGH_MARGIN and mc < rc * ROUND_TROUGH_MARGIN):
            return False
        if not (mv <= lv * ROUND_MID_SHRINK and mv <= rv):
            return False
        if not (rv >= mv * ROUND_RIGHT_VOL_MULT and rc > lc * 1.02):
            return False
        return True
    # 圆弧顶:∩ 形:中段明显峰(>两端×1.03);右侧放量(≥中段×1.8)但价不涨(≤中段×1.02)
    if not (mc > lc * ROUND_CREST_MARGIN and mc > rc * ROUND_CREST_MARGIN):
        return False
    if not (rv >= mv * ROUND_RIGHT_VOL_MULT):
        return False
    return bool(rc <= mc * ROUND_UP_NEUTRAL)


def range_disorder(df: pd.DataFrame, idx: int) -> bool:
    """量价震荡状态(书中"量价震荡"):区间内量价无序 + 量极差 ~10 倍。

    三个条件齐备才判"量价无序",避免把普通横盘误判:
    ① 60 日收盘区间窄(区间震荡);② 量极差 ≥10 倍;③ 放量日里"放量价不涨"
    的占比 ≥50%(量不配合价 = 无序,书中第一层次背离)。此状态下禁用其他量价信号。
    """
    if idx < RANGE_DISORDER_WINDOW + 1:
        return False
    win = df.iloc[idx - RANGE_DISORDER_WINDOW + 1:idx + 1].reset_index(drop=True)
    lo = float(win["close"].min())
    hi = float(win["close"].max())
    if lo <= 0 or (hi - lo) / lo > RANGE_DISORDER_PRICE:
        return False
    vmax, vmin = float(win["volume"].max()), float(win["volume"].min())
    if vmin <= 0 or vmax / vmin < RANGE_DISORDER_VOL_RATIO:
        return False
    # 量价无序:放量日(量>中位×1.3)里"放量却不涨"占 ≥50%
    med = float(win["volume"].median())
    prev = win["close"].shift(1)
    up = win["close"] > prev * 1.005
    vhigh = win["volume"] > med * 1.3
    events = int(vhigh.sum())
    if events == 0:
        return False
    return bool(int((vhigh & ~up).sum()) / events >= 0.5)


def irregular_volume(df: pd.DataFrame, idx: int) -> bool:
    """不规则放量过滤(书中"规则/不规则放量"):3倍突放 + 1~3 日回落 = 庄股特征。

    最近 IRREG_WINDOW 日内出现突放日(量比≥3),随后 1~3 日内量比回落到
    IRREG_RECOVER_MULT 以下 → 突放为一锤子买卖,量价信号不可信,过滤。
    """
    if idx < 25:
        return False
    for j in range(max(1, idx - IRREG_WINDOW + 1), idx):
        r = vol_ratio(df, j)
        if r is None or r < IRREG_SPIKE_MULT:
            continue
        for k in range(j + 1, min(j + 4, idx + 1)):
            rk = vol_ratio(df, k)
            if rk is not None and rk < IRREG_RECOVER_MULT:
                return True
    return False


def m_head_break(df: pd.DataFrame, strokes: list, idx: int) -> bool:
    """M头颈线放量破位(书中 M头 完整版):第二头缩量(<第一头×0.9) + 收盘跌破颈线 + 放量≥1.5×。

    卖点确认。第二头高度需接近第一头(M_HEAD_TOL 内),否则是更低高点、不算 M。
    """
    ups = [s for s in strokes if s.direction == "up"]
    if len(ups) < 2:
        return False
    since = _bars_since(df, idx, ups[-1].end.date)
    if since is None or since > M_HEAD_RECENT:
        return False
    first, second = ups[-2], ups[-1]
    h1, h2 = float(first.end.high), float(second.end.high)
    if h2 < h1 * (1 - M_HEAD_TOL):
        return False
    seg1, seg2 = _stroke_slice(df, first), _stroke_slice(df, second)
    if seg1.empty or seg2.empty:
        return False
    v1 = float(seg1["volume"].max())
    v2 = float(seg2["volume"].max())
    if not (v1 > 0 and v2 < v1 * M_HEAD_VOL_FACTOR):
        return False
    downs = [
        s for s in strokes if s.direction == "down"
        and s.start.date >= first.end.date and s.end.date <= second.start.date
    ]
    if not downs:
        return False
    neck = float(downs[-1].end.low)
    close = float(df["close"].iloc[idx])
    vr = vol_ratio(df, idx)
    if not (close < neck and vr is not None and vr >= M_HEAD_NECK_BREAK_VOL):
        return False
    return True


def vp_indicators(df: pd.DataFrame, idx: int, strokes: list | None = None,
                  fractals: list | None = None) -> dict:
    """汇总量价指标。strokes/fractals 传入后启用结构相关规则(逐波/二踩/收敛/M头)。"""
    r = vol_ratio(df, idx)
    turn = df["turnover"].iloc[idx] if "turnover" in df.columns else None
    coil = coiling(df, fractals, idx) if fractals else {"formed": False, "probe": False}
    return {
        "vol_ratio": round(r, 2) if r is not None else None,
        "is_vol_up": is_vol_up(df, idx),
        "is_vol_down": is_vol_down(df, idx),
        "sky_volume": is_sky_volume(df, idx),
        "floor_volume": is_floor_volume(df, idx),
        "volume_stall": is_volume_stall(df, idx),
        "high_turnover": is_high_turnover(df, idx),
        "top_divergence": top_divergence(df, idx),
        "bottom_divergence": bottom_divergence(df, idx),
        "macd_top_div": macd_top_divergence(df, idx),
        "turnover_tier": turnover_tier(turn),
        # 结构相关量价规则(书补充)
        "wave_decline": wave_decline(df, strokes, idx) if strokes else False,
        "double_test": double_test(df, strokes, idx) if strokes else False,
        "coil": coil["formed"],
        "coil_probe": coil["probe"],
        "round_bottom": round_bottom(df, idx),
        "round_top": round_top(df, idx),
        "range_disorder": range_disorder(df, idx),
        "irregular_volume": irregular_volume(df, idx),
        "m_head_break": m_head_break(df, strokes, idx) if strokes else False,
    }
