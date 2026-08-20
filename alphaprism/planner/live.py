"""核对引擎实时接线(里程碑3):RuleModel × 实时行情 → 每只标的 Verdict。

- 实时快照:同花顺 fuyao.fetch_fund_snapshot(现价/涨跌/换手率)
- 时间调整量比:复用 monitor._vol_ratio(今日换手 vs 20日均换手 × 已过分钟)
- 大盘门控:上证指数日线 → KDJ J 值(作战地图门控规则命中则关闭)
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from ..config import Config
from ..db import connect
from ..fetchers import fuyao
from ..fetchers.etf_kline import fetch_daily
from .checker import check_instrument, gate_open_from_index_closes
from .rulemodel import RuleModel

logger = logging.getLogger(__name__)

INDEX_SYMBOL = "sh000001"        # 上证指数(腾讯符号,非 ETF 的 sz 前缀)
INDEX_NAME = "上证指数"
INDEX_DAYS = 60                  # 门控 J 值需约 9+ 根,取 60 根足够


def _index_closes() -> list[float]:
    """上证指数近 N 日收盘价(腾讯日线,sh000001)。失败返回 []。"""
    try:
        from ..fetchers.etf_kline import HEADERS, _get_json

        start = (pd.Timestamp.today() - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
        end = (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        js = _get_json(url, {"param": f"{INDEX_SYMBOL},day,{start},{end},640,qfq"})
        data = (js.get("data") or {}).get(INDEX_SYMBOL) or {}
        bars = data.get("qfqday") or data.get("day") or []
        closes = [float(b[2]) for b in bars if len(b) > 2]
        return closes[-INDEX_DAYS:]
    except Exception as exc:  # noqa: BLE001
        logger.warning("上证指数日线获取失败(门控默认开放): %s", exc)
        return []


def _vol_ratio(snap: dict, avg_turn: float | None, now: datetime | None = None) -> float | None:
    """时间调整量比:今日换手率 / (20日均换手 × 已过分钟/全天分钟)。"""
    from ..monitor import _trading_minutes, TRADING_MINUTES

    today_turn = snap.get("turnover_ratio_pct")
    if not today_turn or not avg_turn:
        return None
    minutes = _trading_minutes(now)
    if minutes <= 0:
        return None
    expected = avg_turn * minutes / TRADING_MINUTES
    return today_turn / expected if expected > 0 else None


def _avg20_turn(symbol: str) -> float | None:
    """20 日均换手率(从本地库读,无则 None)。"""
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT AVG(turnover) AS a FROM (SELECT turnover FROM etf_kline_daily "
                "WHERE symbol=? AND turnover IS NOT NULL ORDER BY trade_date DESC LIMIT 20)",
                (symbol,)).fetchone()
            return float(row["a"]) if row and row["a"] is not None else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] 20日均换手读取失败: %s", symbol, exc)
        return None


def check_live(model: RuleModel, cfg: Config | None = None) -> dict:
    """实时核对整份作战地图 → {gate:{...}, verdicts:[...]}。

    大盘门控:读作战地图 market_gate.rules 第一条含 J 的规则条件 + 上证 J 值。
    每只标的:实时快照 + 量比 → 结论词。
    """
    cfg = cfg or Config()
    # 大盘门控
    gate_rule = model.global_.market_gate.rules[0] if model.global_.market_gate.rules else None
    gate_condition = gate_rule.condition if gate_rule else None
    closes = _index_closes()
    gate_open = gate_open_from_index_closes(closes, gate_condition)
    j = None
    from .checker import kdj_j
    if closes:
        j = kdj_j(closes)

    verdicts = []
    for instr in model.instruments:
        try:
            snap = fuyao.fetch_fund_snapshot(instr.code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 快照失败: %s", instr.code, exc)
            snap = None
        vr = None
        price = None
        change = None
        if snap and snap.get("last_price") is not None:
            price = float(snap["last_price"])
            change = snap.get("price_change_ratio_pct")
            avg_turn = _avg20_turn(instr.code)
            vr = _vol_ratio(snap, avg_turn)
        verdicts.append(check_instrument(instr, price, vr, gate_open, change))

    return {
        "gate": {
            "open": gate_open,
            "j": round(j, 2) if j is not None else None,
            "rule": gate_condition or "",
            "action": gate_rule.action if gate_rule else "",
        },
        "verdicts": [v.to_dict() for v in verdicts],
    }
