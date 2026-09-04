# -*- coding: utf-8 -*-
"""标准 K 线加载:data/kline_cache 前复权 CSV(腾讯前复权口径,与聚宽前复权一致)。

约定:
- 输入 CSV 列: trade_date, open, high, low, close, volume, amount(可选), pct_chg(可选)
- 统一输出 DataFrame,按 trade_date 升序,列名小写;
- 前复权已由数据源保证(腾讯 fetch_daily 前复权),引擎不自行复权;
- 提供 slice(start, end) 用于切窗(含端点)。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

REQUIRED = {"trade_date", "open", "high", "low", "close", "volume"}


class DataError(RuntimeError):
    pass


def load_daily_csv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise DataError(f"数据文件不存在: {p}")
    df = pd.read_csv(p)
    cols = {str(c).strip().lower(): str(c) for c in df.columns}
    missing = REQUIRED - set(cols)
    if missing:
        raise DataError(f"缺少列 {sorted(missing)}: {list(cols)}")
    df = df.rename(columns={cols[c]: c for c in cols})
    df["trade_date"] = df["trade_date"].astype(str).str[:10]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("trade_date").reset_index(drop=True)
    df = df[df["close"] > 0]
    return df


def slice_window(df: pd.DataFrame, start: str = "", end: str = "") -> pd.DataFrame:
    d = df
    if start:
        d = d[d["trade_date"] >= start]
    if end:
        d = d[d["trade_date"] <= end]
    return d.reset_index(drop=True)


def from_records(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)