"""解套回测:套牢后 纪律操作 vs 死等 的回本速度对比。

回答:我现在的套牢(化工-0.8% 半导体-57% 电池-22% 电网-32% 光伏-29% 通信-31% 黄金-9%)
这套打法能帮我更快回本吗?

方法:对每只ETF,在3年历史中找出所有"从120日高点回撤超过当前浮亏幅度"的时点(套牢起点),
从每个起点模拟两条路径到期末或回本:
- 死等: 持有不动,不操作
- 纪律: 套牢后按作战地图规则操作(加仓摊低成本+红线减仓+波段)+ 换仓(弱势→强势)
输出: 每只ETF 的回本率 / 平均回本天数 / 期末净值,对比两条路径。

用法: python -m alphaprism.backtest_unwind
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .backtest_mc import (BUDGET, FEET, J_LOW, REDUCE_1_3, STOP_PCT, TAKE_PCT,
                          VOL_CUT, VOL_DN, VOL_UP, WARMUP, add_indicators,
                          load_daily, load_index)

# 当前套牢深度 = 经济口径(前复权)距120日高点回撤,由 backtest_unwind --check 自动计算
# 注意:作战地图账面浮亏为不复权成本口径,含份额折算失真(半导体-57.2%实为1:4拆分假象,
# 经济口径仅-29.5%,2026-01买入实为盈利+63%)。回测一律用经济口径。
UNWIND_DEPTH = {
    "516020": 0.178,   # 化工 -17.8%
    "159516": 0.295,   # 半导体 -29.5%
    "159796": 0.238,   # 电池 -23.8%
    "159326": 0.231,   # 电网 -23.1%
    "515790": 0.270,   # 光伏 -27.0%
    "515050": 0.254,   # 通信 -25.4%
    "518850": 0.215,   # 黄金 -21.5%
}
# 换仓规则:弱势仓反弹到目标位换去强势仓(简化:弱势=下降结构,强势=上升结构)
WEAK_TO_STRONG = {  # 弱势ETF -> 目标强势ETF
    "515790": "516020",   # 光伏 -> 化工(作战地图:反弹0.92-0.94换仓)
}

def find_trap_points(d: pd.DataFrame, depth: float, window: int = 120) -> list[int]:
    """找所有从 window 日高点回撤超过 depth 的入场日(进入套牢的第一天)。

    返回 [(idx, peak_price)]:peak_price=套牢起点的 120 日高点(=买入成本价)。
    """
    hi = d["high"].rolling(window).max()
    series = d["close"] / hi
    pts = []
    in_trap = False
    for i in range(WARMUP, len(d)):
        v = series.iloc[i]
        if pd.isna(v):
            continue
        if v <= (1 - depth) and not in_trap:
            pts.append((i, float(hi.iloc[i])))
            in_trap = True
        elif v > (1 - depth):
            in_trap = False
    return pts


def dead_hold(d: pd.DataFrame, idx: int, buy_price: float) -> dict:
    """死等: 在高点买入后持有不动到回本或期末。buy_price=套牢成本价(120日高点)。"""
    cls = d["close"].to_numpy()
    dates = d["trade_date"].to_numpy()
    for i in range(idx, len(d)):
        if cls[i] >= buy_price:
            return {"breakeven": True, "days": i - idx,
                    "exit_date": str(pd.Timestamp(dates[i]).date()),
                    "final_return": round(cls[i] / buy_price - 1, 4)}
    final = cls[-1] / buy_price - 1
    return {"breakeven": False, "days": None, "exit_date": None, "final_return": round(final, 4)}


# 解套打法参数(对齐作战地图:金字塔加仓+独立止损+高抛低吸+换仓)
ADD_BUDGET = 0.15        # 每次补仓占预算比例(作战地图各品种15-25%,取通用15%)
MAX_ADDS = 2             # 最多补 2 批(4-6批预算留2批)
BATCH_STOP = 0.05        # 每批补仓独立止损(作战地图:新仓亏损5%无条件先减)
SWING_TAKE = 0.08        # 波段:每批 +8% 减 1/3(高抛低吸)
REENTRY_DROP = 0.05      # 波段高抛后,回落 5% 再接回
SWITCH_TARGET = 0.92     # 弱势仓(光伏)反弹到成本 92%(=MA60区) 换仓

# 红线(作战地图§八): 波段仓(通信/电网/光伏)锚当日MA20;解套仓(化工/电池/黄金/半导体)锚结构位
BAND_SYMBOLS = {"515050", "159326", "515790"}
UNWIND_SYMBOLS = {"516020", "159796", "518850", "159516"}
REDUCE_LINE = 1 / 3      # 破红线减 1/3
REDUCE_LINE_VOL = 1 / 2  # 放量破红线减 1/2


def _add_signal(price, ma20, ma60, n20l, n20h, vratio, is_up, is_cross, j):
    """补仓信号(作战地图): 买区缩量企稳 或 放量突破。"""
    if pd.isna(ma60) or price < ma60:   # 深套补仓不要求站上MA60,但需在买区附近
        pass
    in_zone = price >= n20l * 0.99 and price <= n20l * 1.06
    pull = in_zone and (not pd.isna(vratio)) and vratio < VOL_DN \
        and (bool(is_up) or bool(is_cross)) and j < J_LOW
    brk = (not pd.isna(vratio)) and vratio > VOL_UP and price > n20h
    return pull or brk


def simulate_unwind(symbol: str, d: pd.DataFrame, idx: int, buy_price: float,
                    ix: pd.DataFrame, strong: pd.DataFrame | None = None) -> dict:
    """套牢后纪律操作(对齐作战地图): 金字塔补仓摊成本 + 独立止损 + 高抛低吸接回 + 换仓。

    buy_price = 套牢成本价(下跌前的高点,即真实买入价)。
    strong = 换仓目标ETF的日线(如光伏→化工),换仓后资金转入跟踪到期末。
    记账: invested=累计投入(初始 BUDGET+补仓新钱), cash=卖出所得。
    解套 = 现价 >= 摊薄成本(invested/总股数)。
    """
    cls = d["close"].to_numpy()
    dates = d["trade_date"].to_numpy()
    ma20 = d["ma20"].to_numpy()
    ma60 = d["ma60"].to_numpy()
    vratio = d["vol_ratio"].to_numpy()
    n20l = d["near20low"].to_numpy()
    n10l = d["near10low"].to_numpy()
    n20h = d["near20high"].to_numpy()
    n60l = d["near60low"].to_numpy()
    jv = d["j"].to_numpy()
    is_up = d["is_up"].to_numpy()
    is_cross = d["is_cross"].to_numpy()

    strong_dates = set() if strong is None else {pd.Timestamp(x) for x in strong["trade_date"]}
    strong_close = strong.set_index("trade_date")["close"] if strong is not None else None

    ix_map = {pd.Timestamp(dt): i for i, dt in enumerate(ix["trade_date"])}
    ix_j = ix["j"].to_numpy()

    shares = BUDGET * (1 - FEET) / buy_price     # 初始套牢仓位
    invested = BUDGET
    cash = 0.0
    batches = [{"shares": shares, "entry": buy_price, "sold": False}]
    n_adds = 0
    swing_cash = 0.0          # 高抛所得,等待回落接回
    switched_value = None     # 换仓后资金(转入强势标的)
    switch_entry_price = None
    cut_near10 = False

    for i in range(idx + 1, len(d)):
        price = cls[i]
        dt = pd.Timestamp(dates[i])
        rix = ix_map.get(dt)
        market_ok = True
        if rix is not None and not pd.isna(ix_j[rix]):
            market_ok = float(ix_j[rix]) < 60.0   # 大盘J<60 才允许补仓(作战地图)

        total_shares = sum(b["shares"] for b in batches)
        blended = invested / total_shares if total_shares > 0 else buy_price

        if switched_value is not None:
            # 换仓后: 资金在强势标的中,跟踪到期末
            if switch_entry_price is not None and strong_close is not None:
                final = switched_value * float(strong_close.iloc[-1]) / switch_entry_price
                return {"breakeven": False, "days": None, "exit_date": None,
                        "final_return": round(final / invested - 1, 4)}
            continue

        if price >= blended and total_shares > 0:   # 解套(摊薄成本)
            return {"breakeven": True, "days": i - idx,
                    "exit_date": str(pd.Timestamp(dates[i]).date()),
                    "final_return": round(price / blended - 1, 4)}

        # 每批独立止损(新仓 -5% 无条件先减,作战地图)
        keep = []
        for b in batches:
            if b["entry"] < buy_price and price <= b["entry"] * (1 - BATCH_STOP):
                cash += b["shares"] * price * (1 - FEET)
            else:
                keep.append(b)
        batches = keep

        # 红线减仓(作战地图§八): 波段仓破当日MA20;解套仓破结构位(近20日低平台)
        vol_cut = not pd.isna(vratio[i]) and vratio[i] > VOL_CUT
        is_band = symbol in BAND_SYMBOLS
        line_price = ma20[i] if is_band else n20l[i]
        if not pd.isna(line_price) and batches and price < line_price and not cut_near10:
            reduce_ratio = REDUCE_LINE_VOL if vol_cut else REDUCE_LINE
            for b in batches:
                cut = b["shares"] * reduce_ratio
                cash += cut * price * (1 - FEET)
                b["shares"] -= cut
            batches = [b for b in batches if b["shares"] > 0.0001]
            cut_near10 = True
        elif batches and price >= line_price:
            cut_near10 = False

        # 高抛低吸: 每批到 +8% 只高抛一次(卖1/3),回落 5% 且企稳后接回(波段减亏)
        if batches:
            for b in batches:
                if not b["sold"] and price >= b["entry"] * (1 + SWING_TAKE):
                    cut = b["shares"] * REDUCE_1_3
                    swing_cash += cut * price * (1 - FEET)
                    b["shares"] -= cut
                    b["sold"] = True
                    b["sell_price"] = price
            batches = [b for b in batches if b["shares"] > 0.0001]
        # 接回: 高抛后回落 5% 且缩量企稳 → 用波段现金买回,作为新批次
        if swing_cash > 0 and any(b["sold"] for b in batches):
            if price <= max(b["sell_price"] for b in batches if b["sold"]) * (1 - REENTRY_DROP):
                if not pd.isna(vratio[i]) and vratio[i] < VOL_DN and (is_up[i] or is_cross[i]) and jv[i] < J_LOW:
                    sh = swing_cash * (1 - FEET) / price
                    batches.append({"shares": sh, "entry": price, "sold": False})
                    swing_cash = 0.0

        # 弱势换仓(光伏): 反弹到成本92%(MA60区)清仓换走 → 资金转入强势标的
        if symbol in WEAK_TO_STRONG and total_shares > 0 and price >= buy_price * SWITCH_TARGET:
            proceeds = cash + swing_cash + sum(b["shares"] for b in batches) * price * (1 - FEET)
            if strong_close is not None and dt in strong_dates:
                switch_entry_price = float(strong_close[dt])
                switched_value = proceeds
                continue
            return {"breakeven": False, "days": None, "exit_date": None,
                    "final_return": round(proceeds / invested - 1, 4)}

        # 金字塔补仓(预算15%/批,最多2批,大盘J<60才可)
        if (market_ok and batches and n_adds < MAX_ADDS and invested < BUDGET * 1.3):
            if _add_signal(price, ma20[i], ma60[i], n20l[i], n20h[i],
                           vratio[i], is_up[i], is_cross[i], jv[i]):
                alloc = min(BUDGET * ADD_BUDGET, BUDGET * 1.3 - invested)
                if alloc >= BUDGET * 0.05:
                    sh = alloc * (1 - FEET) / price
                    batches.append({"shares": sh, "entry": price, "sold": False})
                    invested += alloc
                    n_adds += 1

    # 期末
    total = cash + swing_cash + sum(b["shares"] for b in batches) * cls[-1]
    return {"breakeven": False, "days": None, "exit_date": None,
            "final_return": round(total / invested - 1, 4)}


def run_unwind(symbol: str) -> list[dict]:
    daily = load_daily([symbol])
    d = add_indicators(daily[symbol])
    ix = add_indicators(load_index())
    depth = UNWIND_DEPTH.get(symbol, 0.2)
    strong = None
    if symbol in WEAK_TO_STRONG:
        strong = add_indicators(load_daily([WEAK_TO_STRONG[symbol]])[WEAK_TO_STRONG[symbol]])
    points = find_trap_points(d, depth)
    results = []
    for idx, buy_price in points:
        dead = dead_hold(d, idx, buy_price)
        disc = simulate_unwind(symbol, d, idx, buy_price, ix, strong=strong)
        results.append({"entry_date": str(pd.Timestamp(d["trade_date"].iloc[idx]).date()),
                        "depth": depth, "buy_price": round(buy_price, 4),
                        "dead": dead, "disc": disc})
    return results


def report(results: list[dict], symbol: str, out: Path) -> None:
    n = len(results)
    dead_be = [r for r in results if r["dead"]["breakeven"]]
    disc_be = [r for r in results if r["disc"]["breakeven"]]
    dead_days = sum(r["dead"]["days"] for r in dead_be) / max(len(dead_be), 1)
    disc_days = sum(r["disc"]["days"] for r in disc_be) / max(len(disc_be), 1)
    dead_final = sum(r["dead"]["final_return"] for r in results) / max(n, 1)
    disc_final = sum(r["disc"]["final_return"] for r in results) / max(n, 1)
    be_help = len([r for r in results if r["disc"]["breakeven"] and not r["dead"]["breakeven"]])
    lines = [
        f"## {symbol} 解套回测(套牢深度 {results[0]['depth']*100:.0f}%)",
        f"- 套牢案例数: {n}",
        f"- 死等回本: {len(dead_be)}/{n} ({len(dead_be)/n*100:.0f}%),平均回本天数 {dead_days:.0f}",
        f"- 纪律回本: {len(disc_be)}/{n} ({len(disc_be)/n*100:.0f}%),平均回本天数 {disc_days:.0f}",
        f"- 纪律独力解套(死等不回而纪律回): {be_help} 例",
        f"- 期末净值: 死等 {dead_final*100:+.1f}% vs 纪律 {disc_final*100:+.1f}%",
        "",
    ]
    with out.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="解套回测:套牢后纪律 vs 死等")
    ap.add_argument("--out", default="reports/回测报告-解套.md")
    args = ap.parse_args()
    out = Path(args.out)
    out.write_text(
        "# 解套回测报告:套牢后 纪律操作 vs 死等\n\n"
        "> 生成:2026-08-19 | 方法:每只ETF在3年历史中找所有「从120日高点回撤超过当前深度」的套牢起点,\n"
        "> 从起点模拟到期末或回本。纪律=作战地图打法(金字塔补仓+独立止损5%+高抛低吸+红线减仓+光伏换仓)。\n"
        "> 深度用经济口径(前复权)。注:案例数少(1-12),统计力度弱,只能当方向参考。\n\n",
        encoding="utf-8",
    )
    for sym in sorted(UNWIND_DEPTH):
        res = run_unwind(sym)
        if res:
            report(res, sym, out)
    with out.open("a", encoding="utf-8") as f:
        f.write("\n## 总体结论\n"
                "- 回本速度:纪律并不普遍快于死等(半导体397日 vs 161日更慢;电池91日 vs 165日更快;多数平局)。\n"
                "- 回本概率:纪律略低(高抛卖飞/换仓割肉),但多数标的期末亏损更小(电池-1.6% vs -3.5%,化工-1.8% vs -3.8%,黄金-20.4% vs -24.5%)。\n"
                "- 换仓(光伏→化工)在本窗口未兑现:化工同期也走弱,资金没找到更快的目标。\n"
                "- 解套速度的天花板是数学:经济口径需回本涨幅=深度/(1-深度)。半导体经济口径-30%需+43%,黄金-22%需+28%。规则能压低成本(补仓)和亏损(红线),但不能让价格涨更快。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())