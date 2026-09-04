# -*- coding: utf-8 -*-
"""strategy_v2:分层策略解读器(YAML → 每 bar 条件求值 → 动作)。

- 条件全部确定性(无 eval),注册表见 _CONDS;
- 每 bar 判定顺序 = YAML 中 signals 排列顺序(退出优先由 YAML 保证);
- 上下文 ctx 含:bar 值、预计算序列(indicators/series)、状态
  (break_level/halted/since_right_initial/since_pullback)、分层持仓;
- 信号求值失败/未知类型 → StrategyError(显式失败,不静默)。
"""
from __future__ import annotations

import yaml


class StrategyError(RuntimeError):
    pass


# ---------------------------------------------------------------- 条件注册表

def _cross_above_series(ctx, cond):
    """收盘 > 序列值(cross_above_series / close_above_series 语义:严格大于)。"""
    v = ctx["series"].get(cond["ref"])
    return v is not None and ctx["close"] > v


def _close_below_series(ctx, cond):
    v = ctx["series"].get(cond["ref"])
    return v is not None and ctx["close"] < v


def _close_above_series(ctx, cond):
    return _cross_above_series(ctx, cond)


def _adx_above(ctx, cond):
    v = ctx["series"].get(cond["ref"])
    return v is not None and v > float(cond["threshold"])


def _vol_ratio_above(ctx, cond):
    base = ctx["series"].get(cond["ref"])
    return base is not None and base > 0 and ctx["volume"] / base >= float(cond["threshold"])


def _vol_ratio_below(ctx, cond):
    base = ctx["series"].get(cond["ref"])
    return base is not None and base > 0 and ctx["volume"] / base <= float(cond["threshold"])


def _close_above_ma(ctx, cond):
    v = ctx["series"].get(cond["ref"])
    return v is not None and ctx["close"] > v


def _close_below_ma(ctx, cond):
    v = ctx["series"].get(cond["ref"])
    return v is not None and ctx["close"] < v


def _close_below_rolling(ctx, cond):
    """收盘 < 滚动最低收盘(ref 已预计算);min_bars 由预计算层保证(NaN→False)。"""
    v = ctx["series"].get(cond["ref"])
    return v is not None and ctx["close"] < v


def _kdj_golden_cross(ctx, cond):
    k, d, j = ctx["series"].get("kdj_k"), ctx["series"].get("kdj_d"), ctx["series"].get("kdj_j")
    pk, pd = ctx["series"].get("kdj_k_prev"), ctx["series"].get("kdj_d_prev")
    j_max = float(cond.get("j_max", 30))
    if None in (k, d, j, pk, pd):
        return False
    return (pd is not None and pk <= pd and k > d and j < j_max)


def _days_since(ctx, cond):
    key = f"since_{cond['since']}"
    if not ctx["state"].get(f"fired_{cond['since']}"):
        return False                    # 从未触发过 → 条件不成立(防绕过入场序列)
    v = ctx["state"].get(key, 9999)
    if int(cond["ge"]) > v:
        return False
    if "le" in cond and int(cond["le"]) < v:
        return False
    return True


def _after_signal(ctx, cond):
    return bool(ctx["state"].get(f"fired_{cond['ref']}"))


def _layers_held(ctx, cond):
    return ctx["layers"].shares(cond["layer"]) > 0


def _layers_any(ctx, cond):
    """任一指定层有持仓(退出信号持有门控:空仓时止损/砍仓不吞信号)。"""
    return any(ctx["layers"].shares(n) > 0 for n in (cond.get("layers") or [cond["layer"]]))


def _layers_empty(ctx, cond):
    """指定层全部为空(layers 列表或单层 layer);用于右侧空仓门控变体。"""
    names = cond.get("layers") or [cond["layer"]]
    return all(ctx["layers"].shares(n) == 0 for n in names)


def _j_below(ctx, cond):
    j = ctx["series"].get("kdj_j")
    return j is not None and j < float(cond["threshold"])


def _pct_drop(ctx, cond):
    """单日跌幅:close/prev_close - 1 ≤ -threshold%(收盘<前收,非收盘<开盘)。"""
    pc = ctx["series"].get("prev_close")
    if pc is None or pc <= 0:
        return False
    return (ctx["close"] / pc - 1) * 100 <= -float(cond["threshold"])


def _consecutive_down(ctx, cond):
    """连 N 阴:连续 N 个交易日收盘 < 前一交易日收盘(N=2 默认)。"""
    n = int(cond.get("n", 2))
    pc1 = ctx["series"].get("prev_close")
    if pc1 is None or ctx["close"] >= pc1:
        return False
    prev = pc1
    for k in range(2, n + 1):
        pck = ctx["series"].get(f"prev_close{k}")
        if pck is None or prev >= pck:
            return False
        prev = pck
    return True


def _drawdown_above(ctx, cond):
    """回撤:自 ref 峰值(如近120日高点) ≥ threshold%。"""
    peak = ctx["series"].get(cond["ref"])
    if peak is None or peak <= 0:
        return False
    return (peak - ctx["close"]) / peak * 100 >= float(cond["threshold"])


def _close_below_cost(ctx, cond):
    """收盘 ≤ 成本×multiplier(成本=anchor 层加权成本,由 ctx['anchor_cost'] 注入)。"""
    cost = ctx.get("anchor_cost") or ctx["series"].get("anchor_cost") or 0.0
    if cost <= 0:
        return False
    return ctx["close"] <= cost * float(cond.get("multiplier", 0.92))


def _hold_days_ge(ctx, cond):
    return int(ctx["state"].get("anchor_hold", 0)) >= int(cond["days"])


def _ma_spread_below(ctx, cond):
    """C5' 均线粘合:M5/M10/M20 极差 / 现价 < 阈值%(盘中 SPREAD_TH=0.5)。"""
    m5, m10, m20 = (ctx["series"].get(k) for k in ("m_5", "m_10", "m_20"))
    if None in (m5, m10, m20) or m5 <= 0:
        return False
    spread = (max(m5, m10, m20) - min(m5, m10, m20)) / ctx["close"] * 100
    return spread < float(cond.get("threshold", 0.5))


def _panic_drop(ctx, cond):
    """C5' 恐慌跌幅:单日跌幅 ≥ max(1.5×ATR20%, 1.5%)。放量由调用方组合。"""
    pc = ctx["series"].get("prev_close")
    atr_pct = ctx["series"].get("atr20_pct")
    if pc is None or pc <= 0:
        return False
    drop = (pc - ctx["close"]) / pc * 100
    need = max(1.5 * (atr_pct or 0.0), 1.5)
    return drop >= need and ctx["close"] < pc


def _pos_ok(ctx, cond):
    """C5' 位置到位:现价 ≥ 近250日低(阶段低)且距离 ≤ max(1.5%, 1.5×ATR20%)。"""
    low = ctx["series"].get("low250")
    atr_pct = ctx["series"].get("atr20_pct")
    if low is None or low <= 0 or ctx["close"] < low:
        return False
    dist = (ctx["close"] - low) / ctx["close"] * 100
    return dist <= max(1.5 * (atr_pct or 0.0), 1.5)


def _recovery_pct(ctx, cond):
    """回撤修复:收盘 ≥ 入场峰值 − pct%×(峰值−入场收盘)(均值回归止盈,状态记录于入场日)。"""
    if ctx["layers"].anchor <= 0:
        return False
    peak = ctx["state"].get("anchor_peak")
    entry = ctx["state"].get("anchor_entry_close")
    if not peak or not entry or peak <= entry:
        return False
    target = peak - float(cond["pct"]) / 100.0 * (peak - entry)
    return ctx["close"] >= target


def _and(ctx, cond):
    return all(_eval_cond(ctx, c) for c in cond["conds"])


def _or(ctx, cond):
    return any(_eval_cond(ctx, c) for c in cond["conds"])


_CONDS = {
    "cross_above_series": _cross_above_series,
    "close_above_series": _close_above_series,
    "close_below_series": _close_below_series,
    "adx_above": _adx_above,
    "vol_ratio_above": _vol_ratio_above,
    "vol_ratio_below": _vol_ratio_below,
    "close_above_ma": _close_above_ma,
    "close_below_ma": _close_below_ma,
    "close_below_rolling": _close_below_rolling,
    "kdj_golden_cross": _kdj_golden_cross,
    "days_since": _days_since,
    "after_signal": _after_signal,
    "layers_held": _layers_held,
    "layers_any": _layers_any,
    "layers_empty": _layers_empty,
    "j_below": _j_below,
    "pct_drop": _pct_drop,
    "consecutive_down": _consecutive_down,
    "drawdown_above": _drawdown_above,
    "close_below_cost": _close_below_cost,
    "hold_days_ge": _hold_days_ge,
    "recovery_pct": _recovery_pct,
    "ma_spread_below": _ma_spread_below,
    "panic_drop": _panic_drop,
    "pos_ok": _pos_ok,
    "and": _and,
    "or": _or,
}


def _eval_cond(ctx, cond: dict) -> bool:
    t = cond.get("type")
    fn = _CONDS.get(t)
    if fn is None:
        raise StrategyError(f"未知条件类型: {t}")
    return bool(fn(ctx, cond))


# ---------------------------------------------------------------- 信号/动作

def parse_signals(cfg: dict) -> list[dict]:
    sigs = cfg.get("signals") or []
    if not sigs:
        raise StrategyError("策略缺少 signals")
    for s in sigs:
        if "id" not in s or "action" not in s:
            raise StrategyError(f"信号缺 id/action: {s}")
    return sigs


def _signal_conds(sig: dict) -> list[dict]:
    """信号条件:显式 conds(and/or 组合)或单条件(type+参数平铺)。"""
    if "conds" in sig:
        return sig["conds"]
    extras = {k: v for k, v in sig.items()
              if k not in ("id", "action", "conds", "halt_add", "layers",
                           "after_signal", "record_break_level", "type")}
    extras["type"] = sig["type"]
    return [extras]


def evaluate_bar(cfg: dict, ctx: dict) -> list[dict]:
    """按 YAML 顺序求值;返回本 bar 触发的动作列表(触发第一个后停止,
    与"同一收盘信号最多一个层"不变式一致)。halted 时仅放行重置信号
    (right_initial 重新入场)与独立左侧(anchor_entry)。"""
    fired: list[dict] = []
    halted = bool(ctx["state"].get("halted"))
    for sig in cfg.get("signals") or []:
        if halted and "buy" in (sig.get("action") or {}) \
                and sig["id"] not in ("right_initial", "anchor_entry"):
            continue
        conds = _signal_conds(sig)
        if not _eval_cond(ctx, {"type": "and", "conds": conds}):
            continue
        fired.append({"id": sig["id"], "action": sig["action"],
                      "halt_add": sig.get("halt_add", False),
                      "record_break_level": sig.get("record_break_level"),
                      "layers": sig.get("layers", [])})
        break          # 单动作/bar:执行第一个触发的信号
    return fired


def apply_action(broker, ctx, sig: dict, bar: dict, bar_hash: str) -> list | None:
    """执行信号动作 → (broker 记录列表)。buy 需 ctx 内 broker/资金;sell 语义化。"""
    act = sig["action"]
    snap = {k: ctx["series"].get(k) for k in ("prev20_high", "adx14", "vol20", "vol5",
                                              "m_20", "low20_close", "low250_close",
                                              "entry_break_level", "kdj_k", "kdj_d",
                                              "kdj_j", "kdj_k_prev", "kdj_d_prev")}
    snap["close"] = ctx["close"]
    snap["volume"] = ctx["volume"]
    if "buy" in act:
        layer = act["buy"]
        pct = float(act.get("pct_of_full", 0))
        cap = act.get("shared_cap")
        cap_pct = broker.caps.get(cap) if cap else None
        tr = broker.buy_layer(bar["date"], bar["open"], layer, pct, sig["id"],
                              audit_hash=bar_hash, cap_group=cap, cap_pct=cap_pct,
                              cond_snapshot=snap)
        return [tr] if tr else []
    if "sell" in act:
        outs = broker.sell_layer(bar["date"], bar["open"], act["sell"], sig["id"],
                                 audit_hash=bar_hash, cond_snapshot=snap)
        return outs
    raise StrategyError(f"未知动作: {act}")


def load_layered_strategy(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}