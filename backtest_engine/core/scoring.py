# -*- coding: utf-8 -*-
"""v2.2 截面评分:YAML 因子声明 → 池内分位归一 → 综合分。

- 每因子"越大越好"(突破幅度/ADX/量比/20日相对强弱/回撤深度/恐慌度/反弹动能);
- 归一:池内升序分位 (rank)/(N-1) ∈ [0,1],缺失因子跳过(该因子不计分量);
- 综合分 = Σ weight×分位;右侧/左侧分侧评分(side 由候选类型决定);
- 纯确定性,无 eval;换手带判据(25% 优势 + 排名带宽 2 位)独立函数,便于单测。
"""
from __future__ import annotations


FACTORS = {
    # ---- 右侧(突破强度) ----
    "breakout_pct": lambda s: (s["close"] / s["prev20_high"] - 1) * 100
    if s.get("prev20_high") else None,                       # 突破幅度%
    "adx": lambda s: s.get("adx14"),                          # 趋势强度
    "vol_ratio": lambda s: s["volume"] / s["vol20"] if s.get("vol20") else None,
    "rs20": lambda s: s.get("rs20"),                          # 20日相对强弱(全池排名内化)
    # ---- 左侧(超跌反弹动能) ----
    "drawdown": lambda s: (s["peak120"] - s["close"]) / s["peak120"] * 100
    if s.get("peak120") else None,                            # 回撤深度%
    "panic": (lambda s: max(0.0, (s["prev_close"] - s["close"]) / s["prev_close"] * 100)
              if s.get("prev_close") else None),              # 恐慌度(单日跌幅%)
    "rebound_energy": lambda s: (s["close"] - s["low20_close"]) / s["low20_close"] * 100
    if s.get("low20_close") else None,                        # 距近20日低点回升%
}


def factor_value(name: str, series_vals: dict) -> float | None:
    fn = FACTORS.get(name)
    if fn is None:
        raise ValueError(f"未知评分因子: {name}")
    v = fn(series_vals)
    return None if v is None else float(v)


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """池内升序分位 (rank)/(N-1);并列取平均位。"""
    items = [(k, v) for k, v in values.items() if v is not None]
    n = len(items)
    if n == 0:
        return {}
    items.sort(key=lambda kv: kv[1])
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        rank = (i + j) / 2.0                      # 并列平均位
        pct = rank / (n - 1) if n > 1 else 0.5
        for k in range(i, j + 1):
            out[items[k][0]] = pct
        i = j + 1
    return out


def pool_scores(series_map: dict[str, dict], side: str,
                scoring_cfg: dict) -> dict[str, float]:
    """对池内各标的按 side 评分,输出 0-100 百分比制
    = Σweight×分位 / Σweight × 100(v2.3 阈值化仓位的输入口径)。"""
    factors = (scoring_cfg or {}).get(side) or []
    if not factors:
        return {sym: 0.0 for sym in series_map}
    total_w = sum(float(f.get("weight", 1.0)) for f in factors)
    contrib = {sym: 0.0 for sym in series_map}
    for f in factors:
        fname, weight = f["factor"], float(f.get("weight", 1.0))
        vals = {sym: factor_value(fname, sv) for sym, sv in series_map.items()}
        ranks = percentile_ranks(vals)
        for sym, r in ranks.items():
            contrib[sym] += weight * r
    return {sym: v / total_w * 100.0 for sym, v in contrib.items()}


def rotation_trigger(holder_score: float, challenger_score: float,
                     holder_rank: int, challenger_rank: int,
                     band: dict) -> bool:
    """换手带:挑战者评分 ≥ 持者×1.25 且排名领先 ≥2 位。"""
    gap_pct = float(band.get("score_gap_pct", 25.0)) / 100.0
    rank_gap = int(band.get("rank_gap", 2))
    return (challenger_score >= holder_score * (1 + gap_pct)
            and challenger_rank <= holder_rank - rank_gap)