"""回测框架(设计方案.md §9 阶段2):信号频率 / 胜率 / 盈亏比有数。

规则(简版,全部确定性):
- 买点状态机:下跌笔底落在支撑附近 → 潜在买点激活;后续"放量反包"→ 收盘进场;
  收盘跌破支撑 → 失效
- 离场:卖点确认(放量滞涨/缩量上攻)/ 单笔止损(默认 5%)/ 收盘跌破当前支撑
- 不用纯目标价;期末强制平仓
"""
from __future__ import annotations

import pandas as pd

from .signals import signals as sigmod
from .signals.fractal import process as fractal_process

STOP_PCT = 0.05  # 单笔止损(§5.1: 5~8%,取保守 5%)


def backtest(df: pd.DataFrame, stop_pct: float = STOP_PCT) -> dict:
    df = df.reset_index(drop=True)
    proc = fractal_process(df)
    strokes = proc["strokes"]
    fractals = proc["fractals"]

    trades: list[dict] = []
    setup_buy_count = setup_sell_count = confirmed_buy_count = 0
    in_pos = False
    entry_price = entry_idx = 0.0
    entry_date = ""
    buy_active = False
    buy_support = None

    for i in range(1, len(df)):
        sig = sigmod.detect_signal(df, strokes, i, fractals)
        close = float(df["close"].iloc[i])
        date = str(df["trade_date"].iloc[i])

        if sig["setup"] == "买点":
            setup_buy_count += 1
        elif sig["setup"] == "卖点":
            setup_sell_count += 1

        if not in_pos:
            if sig["setup"] == "买点":
                if not buy_active:
                    buy_active = True
                    buy_support = sig["support"]
                # 失效: 收盘跌破支撑
                if buy_support and close < buy_support:
                    buy_active = False
                elif buy_active and sig["confirm_buy"]:
                    in_pos = True
                    entry_price = close
                    entry_idx = i
                    entry_date = date
                    confirmed_buy_count += 1
                    buy_active = False
            else:
                buy_active = False  # 结构变化,setup 结束
        else:
            reason = None
            if sig["setup"] == "卖点" and sig["confirm_sell"]:
                reason = "卖点确认"
            elif close <= entry_price * (1 - stop_pct):
                reason = f"止损{stop_pct * 100:.0f}%"
            elif sig.get("support") and close < sig["support"]:
                reason = "跌破支撑"
            if reason:
                pnl = (close / entry_price - 1) * 100
                trades.append({
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 4),
                    "exit_date": date,
                    "exit_price": round(close, 4),
                    "pnl_pct": round(pnl, 2),
                    "days": i - entry_idx,
                    "reason": reason,
                })
                in_pos = False

    if in_pos:  # 期末强制平仓
        close = float(df["close"].iloc[-1])
        pnl = (close / entry_price - 1) * 100
        trades.append({
            "entry_date": entry_date,
            "entry_price": round(entry_price, 4),
            "exit_date": str(df["trade_date"].iloc[-1]),
            "exit_price": round(close, 4),
            "pnl_pct": round(pnl, 2),
            "days": len(df) - 1 - entry_idx,
            "reason": "期末强制平仓",
        })

    return _metrics(df, strokes, setup_buy_count, setup_sell_count, confirmed_buy_count, trades)


def _metrics(df, strokes, setup_buy, setup_sell, confirmed_buy, trades):
    n = len(trades)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    years = max(len(df) / 250, 1e-9)

    ret = 1.0
    for t in trades:
        ret *= (1 + t["pnl_pct"] / 100)

    return {
        "symbol": df["symbol"].iloc[0] if "symbol" in df.columns else "",
        "period": f'{df["trade_date"].iloc[0]} ~ {df["trade_date"].iloc[-1]}',
        "trading_days": len(df),
        "strokes": len(strokes),
        "setup_buy_count": setup_buy,
        "setup_sell_count": setup_sell,
        "confirmed_buy_count": confirmed_buy,
        "trades": trades,
        "trade_count": n,
        "signal_freq_per_year": round(n / years, 1),
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "avg_win": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0.0,
        "pl_ratio": round(sum(t["pnl_pct"] for t in wins) / abs(sum(t["pnl_pct"] for t in losses)), 2)
        if losses and sum(t["pnl_pct"] for t in losses) else None,
        "total_return_pct": round((ret - 1) * 100, 2),
        "avg_hold_days": round(sum(t["days"] for t in trades) / n, 1) if n else 0,
    }
