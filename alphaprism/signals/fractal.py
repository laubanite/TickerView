"""分型→笔 结构核心(缠论简化版,设计方案.md §3.1-3.3)。

全部确定性计算、可复核,不引入黑箱:
- 包含处理:相邻 K 线[最高,最低]完全覆盖则合并;上升方向取"高高"(高点取大、低点取大),
  下降方向取"低低"(高点取小、低点取小)。方向由前两根未包含 K 线关系决定。
- 分型:合并后 K 线上找顶/底分型(中间那根最高价>两边最高价 且 最低价>两边最低价);
  相邻同类型保留更极端。
- 笔:相邻顶↔底连线,中间至少隔 5 根合并后 K 线(索引差 >= 5),上升/下降笔交替。
"""
from __future__ import annotations

import pandas as pd
from dataclasses import dataclass


@dataclass
class MergedBar:
    idx: int          # 在合并序列中的位置
    high: float
    low: float
    open: float
    close: float
    start_date: str   # 合并覆盖的原始 K 线起止日期
    end_date: str


@dataclass
class Fractal:
    idx: int          # 合并序列中的位置
    ftype: str        # 'top' | 'bottom'
    high: float
    low: float
    date: str         # 分型中间 K 线日期


@dataclass
class Stroke:
    start: Fractal
    end: Fractal
    direction: str    # 'up' | 'down'


# --------------------------------------------------------------------------- 包含处理

def _is_contained(a: MergedBar, b: MergedBar) -> bool:
    return (a.high >= b.high and a.low <= b.low) or (b.high >= a.high and b.low <= a.low)


def _is_up_direction(merged: list[MergedBar]) -> bool:
    if len(merged) < 2:
        return True
    a, b = merged[-2], merged[-1]
    if b.high > a.high:
        return True
    if b.high < a.high:
        return False
    return b.low >= a.low


def merge_bars(df: pd.DataFrame) -> list[MergedBar]:
    """K线包含处理:合并相邻完全覆盖的 K 线,返回合并后的序列。"""
    merged: list[MergedBar] = []
    for rec in df.to_dict("records"):
        bar = MergedBar(
            idx=len(merged), high=float(rec["high"]), low=float(rec["low"]),
            open=float(rec["open"]), close=float(rec["close"]),
            start_date=rec["trade_date"], end_date=rec["trade_date"],
        )
        if not merged:
            merged.append(bar)
            continue
        prev = merged[-1]
        if _is_contained(prev, bar):
            up = _is_up_direction(merged)
            if up:
                merged[-1] = MergedBar(
                    idx=prev.idx,
                    high=max(prev.high, bar.high), low=max(prev.low, bar.low),
                    open=prev.open, close=bar.close,
                    start_date=prev.start_date, end_date=bar.end_date,
                )
            else:
                merged[-1] = MergedBar(
                    idx=prev.idx,
                    high=min(prev.high, bar.high), low=min(prev.low, bar.low),
                    open=prev.open, close=bar.close,
                    start_date=prev.start_date, end_date=bar.end_date,
                )
        else:
            bar.idx = len(merged)
            merged.append(bar)
    return merged


# --------------------------------------------------------------------------- 分型

def find_fractals(merged: list[MergedBar]) -> list[Fractal]:
    """在合并后 K 线上找顶/底分型。"""
    fractals: list[Fractal] = []
    for i in range(1, len(merged) - 1):
        left, mid, right = merged[i - 1], merged[i], merged[i + 1]
        if mid.high > left.high and mid.high > right.high and mid.low > left.low and mid.low > right.low:
            fractals.append(Fractal(idx=i, ftype="top", high=mid.high, low=mid.low, date=mid.end_date))
        elif mid.low < left.low and mid.low < right.low and mid.high < left.high and mid.high < right.high:
            fractals.append(Fractal(idx=i, ftype="bottom", high=mid.high, low=mid.low, date=mid.end_date))
    return fractals


# --------------------------------------------------------------------------- 笔

def build_strokes(merged: list[MergedBar], fractals: list[Fractal], min_gap: int = 5) -> list[Stroke]:
    """由分型序列构建笔(链式)。

    同类型分型保留更极端后,从当前端点向后找第一个"间隔 >= min_gap 根合并 K 线"
    的对立分型成笔,再从新端点继续 —— 保证上升/下降笔严格交替。
    """
    if not fractals:
        return []
    seq: list[Fractal] = []
    for f in fractals:
        if seq and f.ftype == seq[-1].ftype:
            if f.ftype == "top" and f.high > seq[-1].high:
                seq[-1] = f
            elif f.ftype == "bottom" and f.low < seq[-1].low:
                seq[-1] = f
        else:
            seq.append(f)

    strokes: list[Stroke] = []
    if len(seq) < 2:
        return strokes

    # 增量构建(标准缠论笔确认):
    # start = 当前笔起点,last = 当前笔临时终点(同类型更极端则延伸);
    # 出现"与 last 对立"的分型且与 last 间隔够 → 确认 start→last 成笔,然后翻转方向。
    start, last = seq[0], seq[1]
    for f in seq[2:]:
        if f.ftype == last.ftype:
            if (f.ftype == "top" and f.high > last.high) or (f.ftype == "bottom" and f.low < last.low):
                last = f  # 同类型更极端 → 延伸终点
        elif f.idx - last.idx >= min_gap:
            if last.idx - start.idx >= min_gap:
                direction = "up" if start.ftype == "bottom" else "down"
                strokes.append(Stroke(start=start, end=last, direction=direction))
            start, last = last, f
    return strokes


# --------------------------------------------------------------------------- 总入口

def process(df: pd.DataFrame) -> dict:
    """日线 DataFrame → {merged, fractals, strokes}。df 需含 trade_date/open/high/low/close。"""
    d = df.copy().sort_values("trade_date").reset_index(drop=True)
    merged = merge_bars(d)
    fractals = find_fractals(merged)
    strokes = build_strokes(merged, fractals)
    return {"merged": merged, "fractals": fractals, "strokes": strokes}
