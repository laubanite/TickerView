"""弹药-回本曲线回测:套牢后投入 P 倍弹药补仓摊成本,回本要多快?

回答:我现在的套牢(半导体-57% 化工-18%...)需要补多少弹药,才能把摊薄成本压到
历史上有把握回本的水平?回本要等多久?

方法:每只ETF,在3年历史中找所有套牢起点(与当前深度相同)。
从起点起,假设在底部区间(trap后120日内最低价≈补仓均价)投入 P 倍预算补仓,
摊薄成本 = (1×成本价 + P×补仓均价)/(1+P)。
回本 = 收盘价 >= 摊薄成本;统计 回本率 / 平均回本天数 / 期末净值。

P 从 0(死等)到 5 倍。输出曲线:投入弹药 vs 摊薄成本 vs 回本时间。

用法: python -m alphaprism.backtest_ammo
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .backtest_mc import FEET, load_daily
from .backtest_unwind import UNWIND_DEPTH, find_trap_points

AMMO_FRACTIONS = [0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0]   # P: 弹药=初始仓位的倍数
TROUGH_LOOKAHEAD = 120    # 补仓发生在底部区间:起点后120日内最低价
RECOVERY_LOOKAHEAD = 500  # 回本观测窗口(约2年)

# 作战地图实际弹药映射:P = 弹药金额 / 当前持仓市值(按 2026-08-19 现价×股数)
ACTUAL_AMMO = {   # symbol: (弹药金额, 当前持仓市值, P=弹药/市值)
    "516020": (2125, 1726, 1.23),   # 化工 2000股×0.863  + 25%预算
    "159516": (1275, 368, 3.46),    # 半导体 500股×0.736 + 15%预算 → 弹药=3.5倍仓位!
    "159796": (850, 1609, 0.53),    # 电池 1800股×0.894
    "159326": (2125, 1566, 1.36),   # 电网 900股×1.740
    "515790": (0, 1311, 0.0),       # 光伏 1500股×0.874 不加仓
    "515050": (1275, 1181, 1.08),   # 通信 1100股×1.074
    "518850": (680, 903, 0.75),     # 黄金 100股×9.033
}


def compute_case(d: pd.DataFrame, idx: int, buy_price: float, p: float) -> dict:
    """单个套牢案例 + 弹药比例 p:返回 摊薄成本/回本率/回本天数/期末净值。"""
    cls = d["close"].to_numpy()
    dates = d["trade_date"].to_numpy()
    end = min(len(d), idx + RECOVERY_LOOKAHEAD)
    trough = float(cls[idx + 1:end].min()) if idx + 1 < end else cls[idx]
    # 补仓均价 ≈ 底部区间均价(简化为最低价上方一点,即真能在底部接到的近似)
    add_price = trough
    if p <= 0:
        blended = buy_price
        add_price = 0.0
    else:
        blended = (buy_price + p * add_price) / (1 + p)
    # 回本:收盘 >= 摊薄成本
    days = None
    for i in range(idx + 1, end):
        if cls[i] >= blended:
            days = i - idx
            break
    breakeven = days is not None
    # 期末净值(全部资金按期末价 vs 摊薄成本)
    final_price = cls[-1]
    return {
        "p": p, "trough": round(add_price, 4), "blended": round(blended, 4),
        "breakeven": breakeven, "days": days,
        "final_return": round(final_price / blended * (1 - FEET) - 1, 4),
    }


def run_ammo(symbol: str) -> list[dict]:
    d = load_daily([symbol])[symbol]
    depth = UNWIND_DEPTH.get(symbol, 0.2)
    points = find_trap_points(d, depth)
    results = []
    for idx, buy_price in points:
        for p in AMMO_FRACTIONS:
            results.append(compute_case(d, idx, buy_price, p))
    return results


def report(results: list[dict], symbol: str, out: Path) -> None:
    n_cases = len(results) // len(AMMO_FRACTIONS)
    lines = [f"\n## {symbol} 弹药-回本曲线(套牢深度见 UNWIND_DEPTH)"]
    # 每个P汇总
    per = {}
    for r in results:
        per.setdefault(r["p"], []).append(r)
    lines.append("| 弹药倍数P | 摊薄成本降幅 | 回本率 | 平均回本天数 | 期末净值 |")
    lines.append("|---|---|---|---|---|")
    base_cost = None
    for p in AMMO_FRACTIONS:
        grp = per[p]
        orig = [g for g in grp]
        # 原始成本(buy_price)用第一个案例
        be = [g for g in grp if g["breakeven"]]
        days = sum(g["days"] for g in be) / max(len(be), 1)
        finals = sum(g["final_return"] for g in grp) / len(grp)
        if p == 0:
            base_cost = grp[0]["blended"]
            lines.append(f"| 0(死等) | - | {len(be)}/{len(grp)} ({len(be)/len(grp)*100:.0f}%) | {days:.0f} | {finals*100:+.1f}% |")
        else:
            cut = (1 - grp[0]["blended"] / base_cost) * 100
            lines.append(f"| {p:.2f} | -{cut:.0f}% | {len(be)}/{len(grp)} ({len(be)/len(grp)*100:.0f}%) | {days:.0f} | {finals*100:+.1f}% |")
    lines.append("")
    # 作战地图实际弹药定位(P = 弹药/当前持仓市值)
    amt, pos_val, p_approx = ACTUAL_AMMO.get(symbol, (0, 0, 0.0))
    nearest = min(AMMO_FRACTIONS, key=lambda x: abs(x - p_approx))
    grp = per[nearest]
    be = [g for g in grp if g["breakeven"]]
    be_under = [g for g in per[0.0] if g["breakeven"]]
    under_days = sum(g["days"] for g in be_under) / max(len(be_under), 1)
    lines.append(
        f"> 实际弹药 {amt}元 / 持仓 {pos_val:.0f}元 = **P={p_approx:.2f}**(≈曲线P={nearest}):"
        f"摊薄成本 -{(1 - grp[0]['blended']/per[0.0][0]['blended'])*100:.0f}%,"
        f"回本 {len(be)}/{len(grp)} ({len(be)/len(grp)*100:.0f}%),平均 {sum(g['days'] for g in be)/max(len(be),1):.0f} 天"
        f"(vs 死等 {len(be_under)}/{len(per[0.0])} ({len(be_under)/len(per[0.0])*100:.0f}%),{under_days:.0f} 天)"
    )
    with out.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="弹药-回本曲线:补仓摊成本要多久回本")
    ap.add_argument("--out", default="reports/回测报告-弹药回本.md")
    args = ap.parse_args()
    out = Path(args.out)
    out.write_text(
        "# 弹药-回本曲线回测:补仓摊成本 vs 回本速度\n\n"
        "> 生成:2026-08-19 | 方法:每只ETF找所有与当前深度相同的套牢起点,在底部区间(trap后120日最低价≈补仓均价)\n"
        "> 投入 P 倍弹药补仓,摊薄成本=(成本价+P×补仓均价)/(1+P),统计回本率/天数。P=0 即死等。\n"
        "> 注:补仓均价用历史最低价近似(最乐观情形),真实回本天数会更长;案例数少,当方向参考。\n\n",
        encoding="utf-8",
    )
    for sym in sorted(UNWIND_DEPTH):
        res = run_ammo(sym)
        if res:
            report(res, sym, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())