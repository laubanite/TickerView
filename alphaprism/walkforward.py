"""Walk-forward 验证(设计方案.md §9 阶段2 验收:信号稳定性)。

规则为固定参数、不按窗口调参,故 walk-forward = 滚动窗口回测:
检验胜率 / 盈亏比 / 收益在不同时间段是否稳定,而非集中在某段行情。
"""
from __future__ import annotations

import pandas as pd

from .backtest import backtest


def walk_forward(df: pd.DataFrame, window: int = 250, step: int = 42) -> list[dict]:
    """滚动窗口回测。window=交易日窗口(默认 1 年),step=滑动步长(默认 2 个月)。"""
    df = df.reset_index(drop=True)
    rows = []
    i = 0
    while i + window <= len(df):
        seg = df.iloc[i:i + window].reset_index(drop=True)
        r = backtest(seg)
        rows.append({
            "start": seg["trade_date"].iloc[0],
            "end": seg["trade_date"].iloc[-1],
            "trades": r["trade_count"],
            "win_rate": r["win_rate"],
            "pl_ratio": r["pl_ratio"],
            "return_pct": r["total_return_pct"],
        })
        i += step
    return rows


def by_year(df: pd.DataFrame) -> list[dict]:
    """分自然年回测。"""
    df = df.reset_index(drop=True)
    df["year"] = df["trade_date"].str[:4]
    rows = []
    for year, seg in df.groupby("year"):
        r = backtest(seg.reset_index(drop=True))
        rows.append({
            "year": year,
            "trades": r["trade_count"],
            "win_rate": r["win_rate"],
            "pl_ratio": r["pl_ratio"],
            "return_pct": r["total_return_pct"],
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    """稳定性摘要。"""
    if not rows:
        return {}
    wrs = [r["win_rate"] for r in rows]
    rets = [r["return_pct"] for r in rows]
    pos = sum(1 for r in rets if r > 0)
    return {
        "windows": len(rows),
        "mean_win_rate": round(sum(wrs) / len(wrs), 3),
        "min_win_rate": round(min(wrs), 3),
        "max_win_rate": round(max(wrs), 3),
        "positive_windows": f"{pos}/{len(rows)}",
        "mean_return_pct": round(sum(rets) / len(rets), 1),
    }
