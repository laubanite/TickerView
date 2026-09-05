# -*- coding: utf-8 -*-
"""etf_kline 日线 bar 解析回归(2026-09-04 个股 7 列 bar 修复)。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alphaprism.fetchers.etf_kline import _tencent_bars_to_df


def test_stock_7col_bar():
    """个股 bar 实测 7 字段(date,o,c,h,l,v,amount)——不再抛
    "6 columns passed, passed data had 7 columns"。"""
    bars = [["2026-09-04", "1330.00", "1345.00", "1350.00", "1320.00",
             "45416.00", "60403840.00"]]
    df = _tencent_bars_to_df(bars, "600519")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["close"] == 1345.00 and row["volume"] == 45416.00
    assert row["trade_date"] == "2026-09-04"


def test_etf_6col_bar_unchanged():
    bars = [["2026-09-04", "4.616", "4.621", "4.65", "4.60", "8414655"]]
    df = _tencent_bars_to_df(bars, "510300")
    assert len(df) == 1 and df.iloc[0]["close"] == 4.621


def test_ragged_bar_padded():
    bars = [["2026-09-04", "1.0", "1.1", "1.2", "0.9"]]  # 5 字段
    df = _tencent_bars_to_df(bars, "000001")
    assert len(df) == 1 and df.iloc[0]["volume"] is None or df.iloc[0]["volume"] != df.iloc[0]["volume"]


def test_empty_bars():
    assert _tencent_bars_to_df([], "510300").empty


def test_two_rows_sorted_and_pct():
    bars = [["2026-09-03", "10.0", "10.5", "10.6", "9.9", "100"],
            ["2026-09-04", "10.5", "10.0", "10.7", "9.95", "120", "extra"]]
    df = _tencent_bars_to_df(bars, "600519")
    assert list(df["trade_date"]) == ["2026-09-03", "2026-09-04"]
    assert df.iloc[1]["pct_chg"] == -4.7619
