"""信号分析入口:df → 结构 + 最新一日信号。"""
from __future__ import annotations

import pandas as pd

from . import signals as sigmod
from . import structure as struct
from .fractal import process as fractal_process


def analyze(df: pd.DataFrame, idx: int | None = None) -> dict:
    """对 df 计算结构,并对第 idx 根(默认最新)K 线判定信号。"""
    proc = fractal_process(df)
    strokes = proc["strokes"]
    state = struct.summarize(strokes)
    i = len(df) - 1 if idx is None else idx
    det = sigmod.detect_signal(df, strokes, i, proc["fractals"])
    det["structure"] = state
    det["text"] = sigmod.signal_text(det, state["state"])
    det["merged_count"] = len(proc["merged"])
    det["fractal_count"] = len(proc["fractals"])
    det["stroke_count"] = len(strokes)
    return det
