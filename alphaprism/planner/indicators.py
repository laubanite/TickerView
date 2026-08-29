"""技术指标计算(盘中技术快照用):MA / KDJ / MACD。

口径与全系统一致:
- MACD:EMA adjust=False(与 signals/volprice.compute_macd 相同),柱 = 2×(DIF-DEA)
- KDJ:标准 9,3,3,K/D 从 50 递归(与 checker.kdj_j 同口径,补齐 K/D/J 三元组)
输入为收盘价/最高/最低序列;数据不足返回 None,不抛异常。
"""
from __future__ import annotations

import pandas as pd


def ma(closes, n: int) -> float | None:
    """最近 n 日均线值;数据不足返回 None。"""
    s = list(closes)
    if len(s) < n:
        return None
    return float(pd.Series(s).tail(n).mean())


def kdj(highs, lows, closes, n: int = 9) -> dict:
    """标准 9 日 KDJ 最新 (K, D, J)。数据不足返回全 None。"""
    hs, ls, cs = list(highs), list(lows), list(closes)
    if len(cs) < n:
        return {"K": None, "D": None, "J": None}
    k, d = 50.0, 50.0
    for i in range(len(cs) - n + 1):
        hn = max(hs[i:i + n])
        ln = min(ls[i:i + n])
        rsv = (cs[i + n - 1] - ln) / (hn - ln) * 100 if hn > ln else 50.0
        k = 2 / 3 * k + 1 / 3 * rsv
        d = 2 / 3 * d + 1 / 3 * k
    j = 3 * k - 2 * d
    return {"K": round(k, 2), "D": round(d, 2), "J": round(j, 2)}


def macd(closes, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """最新 MACD:DIF/DEA/柱 + 柱趋势(hist_prev) + 最近 3 根内金叉/死叉(cross)。

    柱 = 2×(DIF-DEA)(行情软件口径,正=红柱 负=绿柱)。
    cross: "金叉" | "死叉" | None(最近 3 根内无交叉)。
    """
    s = pd.Series(list(closes))
    if len(s) < slow + 1:
        return {"dif": None, "dea": None, "hist": None,
                "hist_prev": None, "cross": None}
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = 2 * (dif - dea)
    cross = None
    for i in range(len(dif) - 1, max(len(dif) - 3, 1), -1):
        if dif.iloc[i] >= dea.iloc[i] and dif.iloc[i - 1] < dea.iloc[i - 1]:
            cross = "金叉"
            break
        if dif.iloc[i] <= dea.iloc[i] and dif.iloc[i - 1] > dea.iloc[i - 1]:
            cross = "死叉"
            break
    return {
        "dif": round(float(dif.iloc[-1]), 3),
        "dea": round(float(dea.iloc[-1]), 3),
        "hist": round(float(hist.iloc[-1]), 3),
        "hist_prev": round(float(hist.iloc[-2]), 3) if len(hist) > 1 else None,
        "cross": cross,
    }


def macd_state(m: dict) -> str:
    """MACD 状态文案(事实性标签,不引申多空):
    柱 >0 = 红柱、<0 = 绿柱(零轴上下=柱符号本身),柱长变化 = 柱扩大/缩小,
    近 3 根内交叉 = 金叉/死叉。输出如 "红柱·柱扩大" / "金叉·红柱·柱扩大"。

    2026-08-25 修复:原实现用舍入后的 DIF/DEA 比较定"多头/空头",30分
    DIF=-0.006=DEA 被判"多头"而 柱=-0.001 实为绿柱(见 8.24 光伏报告误标)。
    柱符号即动量方向,废除"多头/空头·柱"混排,避免标签与柱状态矛盾。
    """
    if m.get("dif") is None or m.get("dea") is None:
        return "-"
    hist, prev = m.get("hist"), m.get("hist_prev")
    if hist is None:
        return "-"
    color = "红柱" if hist > 0 else "绿柱" if hist < 0 else "柱≈0"
    trend = ""
    if prev is not None:
        trend = "柱扩大" if abs(hist) >= abs(prev) else "柱缩小"
    # 零轴语境(参考《kdj指标macd指标分析》):DIFF/DEA 同正=多头环境,同负=空头环境
    dif, dea = m.get("dif"), m.get("dea")
    if dif is not None and dea is not None:
        if dif > 0 and dea > 0:
            zero = "零上"
        elif dif < 0 and dea < 0:
            zero = "零下"
        else:
            zero = "穿越"
    else:
        zero = ""
    parts = []
    if m.get("cross"):
        parts.append(m["cross"])
    parts.append(color)
    if trend:
        parts.append(trend)
    if zero:
        parts.append(zero)
    return "·".join(parts)


def atr(highs, lows, closes, n: int = 20) -> float | None:
    """最近 n 根真实波幅均值 ATR(TR = max(H-L, |H-Cprev|, |L-Cprev|))。

    供交易系统设计的 ATR20 相关阈值使用(止损距离 / 恐慌跌幅 / 位置量化)。
    数据不足返回 None,不抛异常。
    """
    hs, ls, cs = list(highs), list(lows), list(closes)
    if len(cs) < n + 1:
        return None
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = hs[i], ls[i], cs[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    return round(float(sum(trs[-n:]) / n), 4)