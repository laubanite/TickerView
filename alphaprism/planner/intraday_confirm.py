"""盘后增量1 · 跨日确认推进(纯规则,零 LLM,交易系统设计 §3.5 / 产品架构 §五·5.1)。

对已归档的"右侧初现"存档做时间确认(策略 §3.5 时间路径):
突破后 N 个交易日收盘不破突破位(以归档锚定价为下限位)→ 升"右侧确认";
任一收盘跌破 → 不确认(证伪/维持)。结果写 signal_log(kind='activate')。
数据源 = 库内 etf_kline_daily(不联网)——这是"单日诚实、跨日确认"的盘后执行点。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from ..db import connect, init_db

logger = logging.getLogger(__name__)

CONFIRM_DAYS = 5          # 时间确认窗口(初值,待校准)


def _daily_df(conn, symbol: str):
    """从库内日K构造 df(供 intraday._daily_facts 复用,不联网)。"""
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, volume FROM etf_kline_daily"
        " WHERE symbol=? ORDER BY trade_date DESC LIMIT 260", (symbol,)).fetchall()
    if not rows:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    return df.sort_values("trade_date").reset_index(drop=True)


def _confirm_one(conn, arch: dict) -> str | None:
    """单条右侧初现存档:返回 '右侧确认' / '维持' / '证伪' / None(数据不足)。

    突破位 = 归档锚定价(突破日现价,当日固定,交易系统设计 §2.4)——收盘不破即
    时间确认(§3.5);用当前价重算会因价格漂移误判,禁用。
    """
    sym = str(arch["symbol"])
    t0 = str(arch["trade_date"])
    anchor = arch.get("anchor_price")
    if not anchor:
        return None
    hold_line = float(anchor)
    after = conn.execute(
        "SELECT close FROM etf_kline_daily WHERE symbol=? AND trade_date > ? "
        "ORDER BY trade_date ASC LIMIT ?", (sym, t0, CONFIRM_DAYS)).fetchall()
    if not after:
        return None                       # 未到窗口
    closes = [float(r["close"]) for r in after]
    if min(closes) >= hold_line:
        return "右侧确认"
    if closes[0] < hold_line:
        return "证伪"
    return "维持"


def run_confirmation(trade_date: str | None = None) -> list[dict]:
    """对全部待确认的右侧初现存档跑跨日确认,结果写 signal_log。返回结果列表。"""
    conn = connect()
    init_db(conn)
    out: list[dict] = []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM advice_archive WHERE state_word='右侧初现'")] or \
               [dict(r) for r in conn.execute(
            "SELECT * FROM advice_archive WHERE state_word LIKE '%右侧%'")]
        for arch in rows:
            verdict = _confirm_one(conn, arch)
            if verdict in ("右侧确认", "证伪"):
                conn.execute(
                    "INSERT INTO signal_log (trade_date, symbol, kind, signal, state,"
                    " price, note) VALUES (?,?,?,?,?,?,?)",
                    (str(datetime.now().date()), str(arch["symbol"]),
                     "activate" if verdict == "右侧确认" else "fail",
                     verdict, "激活" if verdict == "右侧确认" else "失效",
                     float(arch["anchor_price"] or 0), f"跨日确认(窗口{CONFIRM_DAYS}日)"))
                out.append({"symbol": arch["symbol"], "verdict": verdict,
                            "trade_date": arch["trade_date"]})
        conn.commit()
    finally:
        conn.close()
    return out