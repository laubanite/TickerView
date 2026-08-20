"""盘中监测(阶段5):交易时段每 15 分钟,多周期触发判定 + 去重推送。

日线=上下文(支撑/压力/结构/均线/近期突破),30分钟=择时(结构方向),当日量能=确认(时间调整量比)。
触发(状态变化才推,避免刷屏):
- buy_confirm      📈 买点确认: 日线买点 + 30m 转上 + 当日量比>1.5
- pullback_confirm 📈 回踩确认: 近5日放量突破 + 现价贴近MA10/20 + 当日缩量(量比<0.8)
- buy_invalid      ⚠️ 买点失效: 日线买点 + 盘中跌破支撑(放弃买入,观望)
- sell_confirm     📉 卖点确认: 日线卖点 + 30m 转下 + 放量
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time

import pandas as pd

from .config import Config
from .db import connect, init_db
from .fetchers import fuyao
from .fetchers.etf_kline import fetch_30m
from .push import send

logger = logging.getLogger(__name__)

SESSION_WINDOWS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
VOL_UP = 1.5          # 放量阈值(时间调整量比)
VOL_DOWN = 0.8        # 缩量阈值
PULLBACK_NEAR = 0.03  # 现价距均线 3% 内算"贴近"
BREAK_LOOKBACK = 5    # 近 N 日算"近期放量突破"
MA_PERIODS = (10, 20)
TRADING_MINUTES = 240  # 全天交易分钟数(4小时)


def is_trading_session(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    t = now.time()
    return any(start <= t <= end for start, end in SESSION_WINDOWS)


def _trading_minutes(now: datetime | None = None) -> int:
    """今天已过交易分钟数(用于时间调整量比)。"""
    now = now or datetime.now()
    t = now.time()
    if time(9, 30) <= t <= time(11, 30):
        return int(t.hour * 60 + t.minute - (9 * 60 + 30))
    if time(13, 0) <= t <= time(15, 0):
        return 120 + int(t.hour * 60 + t.minute - 13 * 60)
    return 0


def _daily_context(daily_df: pd.DataFrame) -> dict:
    """日线上下文:结构/支撑/压力/setup/均线/近期放量突破/20日均换手。"""
    from .signals.analyze import analyze
    from .signals.signals import classify_signal

    res = analyze(daily_df)
    cs, st = classify_signal(res)
    setup = cs if st != "无" else "观望"
    closes = daily_df["close"]
    vol = daily_df["volume"]
    turn = daily_df["turnover"] if "turnover" in daily_df.columns else None
    ma = {p: float(closes.tail(p).mean()) if len(closes) >= p else None for p in MA_PERIODS}
    # 近期放量突破:近 BREAK_LOOKBACK 日 收盘>压力 且 量比>1.5
    breakout = False
    resistance = res.get("resistance")
    if resistance and len(vol) >= 25:
        base = float(vol.iloc[-25:-5].mean())
        for i in range(-BREAK_LOOKBACK, 0):
            if base and vol.iloc[i] / base > VOL_UP and float(closes.iloc[i]) > resistance * 0.995:
                breakout = True
                break
    return {
        "setup": setup,
        "support": res.get("support"),
        "resistance": resistance,
        "structure": res["structure"]["state"]["state"],
        "ma10": ma[10], "ma20": ma[20],
        "breakout_recent": breakout,
        "avg20_turn": float(turn.tail(20).mean()) if turn is not None and len(turn.dropna()) >= 20 else None,
    }


def _m30_direction(symbol: str) -> str | None:
    """最近 30m 结构方向(up/down)。30m 列名为 ts,映射为 trade_date 供 fractal 用。"""
    df = fetch_30m(symbol, count=160)
    if df.empty:
        return None
    from .signals.fractal import process as fp

    proc = fp(df.rename(columns={"ts": "trade_date"}))
    return proc["strokes"][-1].direction if proc["strokes"] else None


def _vol_ratio(snap: dict, ctx: dict, now: datetime | None = None) -> float | None:
    """时间调整量比:今日换手率 / (20日均换手 × 已过分钟/全天分钟)。"""
    today_turn = snap.get("turnover_ratio_pct")
    avg_turn = ctx.get("avg20_turn")
    if not today_turn or not avg_turn:
        return None
    minutes = _trading_minutes(now)
    if minutes <= 0:
        return None
    expected = avg_turn * minutes / TRADING_MINUTES
    return today_turn / expected if expected > 0 else None


def _evaluate(sym: str, ctx: dict, snap: dict, m30_dir: str | None, vr: float | None) -> list[str]:
    """触发判定(纯函数)。"""
    price = float(snap["last_price"])
    setup, support = ctx["setup"], ctx["support"]
    out = []
    if setup == "买点" and m30_dir == "up" and vr is not None and vr >= VOL_UP:
        out.append("buy_confirm")
    if ctx["breakout_recent"] and ctx.get("ma20") and vr is not None and vr <= VOL_DOWN:
        near = min((abs(price / m - 1) for m in (ctx["ma10"], ctx["ma20"]) if m), default=1.0)
        if near <= PULLBACK_NEAR:
            out.append("pullback_confirm")
    if setup == "买点" and support and price < support:
        out.append("buy_invalid")
    if setup == "卖点" and m30_dir == "down" and vr is not None and vr >= VOL_UP:
        out.append("sell_confirm")
    return out


_TRIGGER_LABEL = {
    "buy_confirm": "📈 买点确认", "pullback_confirm": "📈 回踩确认",
    "buy_invalid": "⚠️ 买点失效", "sell_confirm": "📉 卖点确认",
}
_TRIGGER_NOTE = {
    "buy_confirm": "日线买点+30m转上+放量",
    "pullback_confirm": "放量突破后缩量回踩均线",
    "buy_invalid": "盘中跌破支撑,放弃买入(观望)",
    "sell_confirm": "日线卖点+30m走坏+放量",
}


def _trigger_message(sym: str, t: str, ctx: dict, snap: dict) -> tuple[str, str]:
    price = snap.get("last_price")
    sup = f"支撑 {ctx['support']}" if ctx.get("support") else ""
    res = f"压力 {ctx['resistance']}" if ctx.get("resistance") else ""
    return (f"{_TRIGGER_LABEL.get(t, t)} {sym}",
            f"现价 {price} | {sup} / {res}\n{_TRIGGER_NOTE.get(t, '')} | {ctx.get('structure', '')}结构")


def _llm_trigger_note(sym: str, t: str, ctx: dict, snap: dict, cfg) -> str:
    """盘中触发 LLM 解读(短超时,失败返回空,推送不阻塞)。"""
    from .llm import chat

    price = snap.get("last_price")
    prompt = (f"你是A股中长线交易助手。触发「{_TRIGGER_LABEL.get(t, t)}」(ETF {sym},现价 {price},"
              f"{ctx.get('structure', '')}结构,支撑{ctx.get('support')}/压力{ctx.get('resistance')})。"
              f"用一句话说清含义和该怎么做,30字内。")
    return (chat([{"role": "user", "content": prompt}], cfg=cfg, timeout=15, max_tokens=80) or "").strip()


def _check_symbol(conn, cfg, sym: str, do_push: bool, now: datetime | None = None) -> list[dict]:
    daily_df = pd.read_sql_query(
        "SELECT trade_date, open, high, low, close, volume, turnover FROM etf_kline_daily "
        "WHERE symbol=? ORDER BY trade_date", conn, params=(sym,))
    if len(daily_df) < 30:
        return []
    ctx = _daily_context(daily_df)
    snap = fuyao.fetch_fund_snapshot(sym)
    if not snap or snap.get("last_price") is None:
        return []
    m30_dir = _m30_direction(sym)
    vr = _vol_ratio(snap, ctx, now)
    triggers = _evaluate(sym, ctx, snap, m30_dir, vr)
    llm_enrich = bool(cfg.get("monitor", "llm_enrich", default=True))
    today = date.today().isoformat()
    pushed = []
    for t in triggers:
        row = conn.execute(
            "SELECT active FROM monitor_trigger WHERE symbol=? AND trigger=?", (sym, t)).fetchone()
        if row is None or row["active"] == 0:  # 状态变化才推
            if do_push and cfg.get("push", "enabled", default=False):
                title, body = _trigger_message(sym, t, ctx, snap)
                if llm_enrich:
                    note = _llm_trigger_note(sym, t, ctx, snap, cfg)
                    if note:
                        body = f"{body}\n💡 {note}"
                send(title, body, level="alert", cfg=cfg)
            conn.execute(
                "INSERT OR REPLACE INTO monitor_trigger (symbol, trigger, trade_date, active) "
                "VALUES (?,?,?,1)", (sym, t, today))
            pushed.append({"symbol": sym, "trigger": t})
    # 解除未再触发的
    if triggers:
        ph = ",".join("?" * len(triggers))
        conn.execute(f"UPDATE monitor_trigger SET active=0 WHERE symbol=? AND trigger NOT IN ({ph})",
                     (sym, *triggers))
    else:
        conn.execute("UPDATE monitor_trigger SET active=0 WHERE symbol=?", (sym,))
    conn.commit()
    return pushed


def run_monitor(cfg: Config | None = None, push: bool = True) -> list[dict]:
    """盘中监测:非交易日/非交易时段直接返回 [];返回本次新触发列表。"""
    cfg = cfg or Config()
    if not is_trading_session():  # 先查本地时段,避免每15分钟都调日历接口
        logger.info("非交易时段,盘中监测跳过")
        return []
    if not fuyao.is_trading_day():
        logger.info("非交易日,盘中监测跳过")
        return []
    conn = connect()
    init_db(conn)
    new_triggers: list[dict] = []
    try:
        for item in cfg.watchlist:
            sym = str(item["symbol"])
            try:
                new_triggers.extend(_check_symbol(conn, cfg, sym, push))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] 盘中监测失败: %s", sym, exc)
    finally:
        conn.close()
    if new_triggers:
        logger.info("盘中触发 %d 条: %s", len(new_triggers),
                    ", ".join(f"{t['symbol']}:{t['trigger']}" for t in new_triggers))
    return new_triggers
