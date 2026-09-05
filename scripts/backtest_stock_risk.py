# -*- coding: utf-8 -*-
"""个股风险状态机 → 日线回测翻译(strat_stock_risk_v1,方案 §14 预注册)。

翻译纪律同 strat_c5_daily.yaml(盘中状态机→日线回测的先例):逐条原文翻译。
映射(预注册,回测与放开后建议同源):
  满仓→半仓: R3(close<low20_base) | R2(close<M20 且 vr≥1.5) | R10(计数≥3)
  任意→空仓: R4(close<low250_base),即时(确认臂亦不延迟)
  半仓→满仓: 风险全解除闸门 close≥low20_base 且 close≥M20 且 计数<3
  空仓→半仓: 同一闸门(状态标签回正常≠可回补,必须过闸门)
确认臂 v1b: 减仓需连续2日处于风险态;回补/再进场需连续2日过闸门;清仓即时。

运行: python scripts/backtest_stock_risk.py [--confirm]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest_engine.core.data_loader import load_daily_csv  # noqa: E402

CACHE = ROOT / "data" / "kline_cache"
COMMISSION = 0.0005          # 双边,engine.yaml 同口径
SLIPPAGE = 0.0002            # 单边
LOT = 100                    # A股整手
CAPITAL = 1000000.0          # 初始资金 100 万(镜像 strat_alpha_v23_stock full_cash=1M;
                             # 10 万本金在茅台级高价股上买不起一手,整手闸门会吞掉建仓)
SYMBOLS = {                  # 方案 §14 五体质(预注册)
    "300308": "高波动科技", "600519": "消费白马", "601899": "周期资源",
    "600795": "公用事业", "601179": "大盘基建央企",
}
START, END = "2021-06-01", "2099-12-31"   # 起点前有250日暖机;终点=缓存末日


# ---------------------------------------------------------------- 指标层(与实盘 stock_risk 同口径)
def _indicators(df: pd.DataFrame) -> pd.DataFrame:
    """切位基准(rolling)截至**前收盘**(.shift(1)),与实盘 build_stock_context
    base=iloc[:-1] 口径一致;均线含当日全窗(实盘同口径);min_periods 与回测引擎
    low20_close/low250_close 相同。weak_cnt=近10日20日通道新破位计数(封顶4)。"""
    out = df.copy()
    c = out["close"].astype(float)
    out["m5"] = c.rolling(5).mean()
    out["m20"] = c.rolling(20).mean()
    out["m60"] = c.rolling(60).mean()
    out["vol20"] = out["volume"].astype(float).rolling(20).mean().shift(1)
    out["low20_base"] = c.rolling(20, min_periods=5).min().shift(1)
    out["low250_base"] = c.rolling(250, min_periods=60).min().shift(1)
    out["peak250"] = c.rolling(250, min_periods=60).max().shift(1)
    roll20 = c.rolling(20).min().shift(1)
    breach = (c < roll20) & (c.shift(1) >= roll20.shift(1))
    out["weak_cnt"] = breach.rolling(10).sum().clip(upper=4).fillna(0)
    return out


# ---------------------------------------------------------------- 状态机(纯函数,合成矩阵可复用)
def risk_state(row) -> tuple[str, list[str]]:
    """(风险态, 命中规则)。风险态∈正常/风险/严重。回测子集=R2/R3/R4/R10
    (R5/R8/R9/R11/R12 不产生动作,不进映射)。"""
    price = row["close"]
    hits: list[str] = []
    if pd.notna(row["low250_base"]) and price < row["low250_base"]:
        hits.append("R4")
    if pd.notna(row["low20_base"]) and price < row["low20_base"]:
        hits.append("R3")
    if pd.notna(row["m20"]) and pd.notna(row["vol20"]) and row["vol20"] > 0 \
            and price < row["m20"] and row["volume"] / row["vol20"] >= 1.5:
        hits.append("R2")
    if row["weak_cnt"] >= 3:
        hits.append("R10")
    if "R4" in hits:
        return "严重", hits
    return ("风险", hits) if hits else ("正常", hits)


def risk_cleared(row) -> bool:
    """风险全解除闸门(回补/再进场共用,预注册语义)。"""
    price = row["close"]
    return bool(pd.notna(row["low20_base"]) and price >= row["low20_base"]
                and pd.notna(row["m20"]) and price >= row["m20"]
                and row["weak_cnt"] < 3)


# ---------------------------------------------------------------- 单标的回测
def _sell_ok(open_: float, prev_close: float | None) -> bool:
    """跌停开盘闸门:开盘≤昨收×0.905 视为跌停,卖出顺延至首个非跌停开盘。"""
    return not (prev_close is not None and pd.notna(prev_close)
                and open_ <= prev_close * 0.905)


def backtest_one(df: pd.DataFrame, confirm: bool = False, code: str = "SYN",
                 reentry: bool = True) -> dict:
    """单标的回测。目标仓位:1.0 满仓 / 0.5 半仓 / 0.0 空仓。
    收盘判定 → 次日开盘成交(next_open);T1;佣金+滑点;整手。
    降档调度(风险态连续性/闸门连续性)在收盘判定层;清仓即时。
    reentry=False 为 v5.2 对照臂"只减不补"(§14.6):减仓/清仓后仓位锁定不回补,
    用于回答"回补在回测里贡献了什么"。"""
    df = _indicators(df.reset_index(drop=True))
    dates = df["trade_date"].astype(str).tolist()
    try:
        i0 = next(i for i, d in enumerate(dates) if d >= START)
    except StopIteration:
        return {"symbol": code, "error": "窗口无数据"}
    if i0 < 260:
        return {"symbol": code, "error": f"暖机不足({i0}根)"}
    cash, shares = CAPITAL, 0
    target = 1.0
    pend: float | None = None       # 次日开盘执行的目标仓位(跌停顺延时保留重试)
    risk_streak = 0                 # 连续风险态天数(确认臂:减仓)
    clear_streak = 0                # 连续过闸门天数(确认臂:回补/再进场)
    trades: list[dict] = []
    equity: list[float] = []
    bh_equity: list[float] = []
    bh_shares = 0
    bh_cash = CAPITAL
    blocked = 0
    peak = mdd = peak_bh = mdd_bh = 0.0

    # ---- 窗口首日建满仓(与买入持有同起点;建仓非信号交易,不计入 trades) ----
    o0 = float(df.iloc[i0]["open"])
    lots0 = int(CAPITAL // (o0 * (1 + SLIPPAGE) * LOT))
    if lots0 > 0:
        cash -= lots0 * LOT * o0 * (1 + SLIPPAGE) * (1 + COMMISSION)
        shares = lots0 * LOT

    for i in range(i0, len(df)):
        row = df.iloc[i]
        o, c = float(row["open"]), float(row["close"])
        date = str(row["trade_date"])
        prev_close = float(df.iloc[i - 1]["close"]) if i > 0 else None
        # ---- 1) 开盘执行昨日调度(next_open;卖出受跌停闸门约束,顺延重试) ----
        if pend is not None and abs(pend - target) > 1e-9:
            total = cash + shares * o
            want = total * pend
            cur = shares * o
            if want < cur - 1e-6:                     # 卖出
                if _sell_ok(o, prev_close):
                    px = o * (1 - SLIPPAGE)
                    lots = min(shares // LOT, int(round((cur - want) / (px * LOT))))
                    if lots > 0:
                        cash += lots * LOT * px * (1 - COMMISSION)
                        shares -= lots * LOT
                        trades.append({"date": date, "side": "SELL",
                                       "px": round(px, 3), "lots": lots})
                    target = pend
                    pend = None
                else:
                    blocked += 1                      # 跌停,顺延至下一交易日
            else:                                     # 买入
                px = o * (1 + SLIPPAGE)
                lots = min(int(cash // (px * LOT)),
                           int(round((want - cur) / (px * LOT))))
                if lots > 0:
                    cost = lots * LOT * px
                    fee = cost * COMMISSION
                    if cost + fee <= cash:
                        cash -= cost + fee
                        shares += lots * LOT
                        trades.append({"date": date, "side": "BUY",
                                       "px": round(px, 3), "lots": lots})
                target = pend
                pend = None
        # ---- 2) 收盘判定(降档调度/闸门升级) ----
        state, _hits = risk_state(row)
        if state == "严重":
            if target > 0.0:
                pend = 0.0                            # 清仓即时,确认臂不延迟
            risk_streak = clear_streak = 0
        elif state == "风险":
            risk_streak += 1
            clear_streak = 0
            if target == 1.0 and (risk_streak >= 2 if confirm else True):
                pend = 0.5
        else:                                         # 正常
            risk_streak = 0
            if target < 1.0 and reentry and risk_cleared(row):
                clear_streak += 1
                if clear_streak >= 2 if confirm else True:
                    pend = 1.0 if target == 0.5 else 0.5
            else:
                clear_streak = 0
        # ---- 3) 估值(策略与买入持有基准同起点) ----
        eq = cash + shares * c
        equity.append(eq)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak) if peak else 0.0
        if bh_shares == 0 and o > 0:
            # BH 一次性建仓:股数 = 手数×100(注意单位,勿把手数当股数)
            bh_lots = int(CAPITAL // (o * LOT))
            bh_shares = bh_lots * LOT
            bh_cash = CAPITAL - bh_shares * o
        bhv = bh_cash + bh_shares * c
        bh_equity.append(bhv)
        peak_bh = max(peak_bh, bhv)
        mdd_bh = max(mdd_bh, 1 - bhv / peak_bh) if peak_bh else 0.0

    final = equity[-1] if equity else CAPITAL
    bh_final = bh_equity[-1] if bh_equity else CAPITAL
    return {"symbol": code, "trades": trades,
            "n_trades": len(trades), "sells": sum(1 for t in trades if t["side"] == "SELL"),
            "ret_pct": (final / CAPITAL - 1) * 100,
            "bh_ret_pct": (bh_final / CAPITAL - 1) * 100,
            "mdd_pct": mdd * 100, "bh_mdd_pct": mdd_bh * 100,
            "equity": equity, "bh_equity": bh_equity,
            "blocked_sells": blocked}


# ---------------------------------------------------------------- 合成校验(断路器:先于实盘)
def _synthetic_df(path: list[tuple[str, float, float]]) -> pd.DataFrame:
    """(date, close, volume) → 最小 OHLC 框(open=前收,高低=收±0.5%)。"""
    rows = []
    prev = path[0][1]
    for d, c, v in path:
        rows.append({"trade_date": d, "symbol": "SYN", "open": prev,
                     "high": max(prev, c) * 1.005, "low": min(prev, c) * 0.995,
                     "close": c, "volume": v})
        prev = c
    return pd.DataFrame(rows)


def run_synthetic_checks() -> list[str]:
    """方案§14 断路器:已知走势 → 断言信号/成交时点/仓位路径与手推一致;
    扰动未来K线不改变历史信号(无未来函数)。返回违规清单。"""
    bad: list[str] = []
    warm = pd.bdate_range(end="2021-05-31", periods=260)
    scen = pd.bdate_range("2021-06-01", periods=8)
    # 场景A:缓涨260日 → 破20日低(风险) → 续破250日低(严重) → 反弹过闸门
    path = [(str(d), 10 + i * 0.02, 1000) for i, d in enumerate(warm)]
    path += [
        (str(scen[0]), 15.0, 1000),              # 末根缓涨(高位)
        (str(scen[1]), 13.0, 1000),              # 破20日低 → 风险(次日开盘减半)
        (str(scen[2]), 8.0, 1000),               # 破250日低 → 严重(次日开盘清仓)
        (str(scen[3]), 8.0, 1000),
        (str(scen[4]), 14.5, 1000),              # 反弹(m20 仍压着,闸门未过)
        (str(scen[5]), 14.6, 1000),              # 闸门过(≥low20/m20,计数2<3)→ 次日买回半仓
        (str(scen[6]), 14.7, 1000),
        (str(scen[7]), 14.8, 1000),
    ]
    df = _synthetic_df(path)
    df["trade_date"] = df["trade_date"].astype(str)
    r = backtest_one(df, confirm=False)
    if "error" in r:
        return [f"场景A构建失败: {r['error']}"]
    sells = [t for t in r["trades"] if t["side"] == "SELL"]
    buys = [t for t in r["trades"] if t["side"] == "BUY"]
    if len(sells) != 2:
        bad.append(f"场景A:预期减仓+清仓两笔卖出,实得 {len(sells)}: {sells}")
    if sells and sells[0]["date"][:10] != str(scen[2].date()):
        bad.append(f"场景A:首笔卖出(减半)应在风险日次一交易日 {scen[2].date()},"
                   f"实得 {sells[0]['date'][:10]}")
    if len(buys) < 1:
        bad.append("场景A:闸门解除后未回补(再进场缺失)")
    # 无未来函数:截断未来K线,截断点前的成交必须逐笔一致
    cut_date = str(scen[3])
    r_short = backtest_one(_synthetic_df([p for p in path if p[0] <= cut_date]),
                           confirm=False)
    s_full = [(t["date"], t["side"], t["lots"]) for t in r["trades"] if t["date"] <= cut_date]
    s_short = [(t["date"], t["side"], t["lots"]) for t in r_short.get("trades", [])]
    if s_full != s_short:
        bad.append(f"无未来函数违例:全量 {s_full} vs 截断 {s_short}")
    # 确认臂:单日风险不触发减仓(次日即恢复,连续性不成立)
    path_b = [(str(warm[i]), 10 + i * 0.02, 1000) for i in range(260)]
    path_b += [(str(scen[0]), 15.0, 1000), (str(scen[1]), 13.0, 1000),
               (str(scen[2]), 13.6, 1000), (str(scen[3]), 13.8, 1000),
               (str(scen[4]), 14.0, 1000), (str(scen[5]), 14.1, 1000),
               (str(scen[6]), 14.2, 1000), (str(scen[7]), 14.3, 1000)]
    rb = backtest_one(_synthetic_df(path_b), confirm=True)
    if any(t["side"] == "SELL" for t in rb.get("trades", [])):
        bad.append("确认臂:单日风险态即触发减仓(应需连续2日)")
    return bad


def _portfolio_mdd(equities: list[list[float]]) -> float:
    """真等权组合 MDD:各标的净值归一到 1 后逐日平均(等权、不做再平衡假设的
    最保守口径),对组合净值算最大回撤。预注册 c1 用这个,不是个体 MDD 平均。"""
    n = len(equities)
    if not n:
        return 0.0
    norm = []
    for eq in equities:
        base = eq[0] if eq and eq[0] else 1.0
        norm.append([v / base for v in eq])
    m = min(len(x) for x in norm)
    port = [sum(x[i] for x in norm) / n for i in range(m)]
    peak = 0.0
    mdd = 0.0
    for v in port:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak) if peak else 0.0
    return mdd * 100


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    only_synth = "--synth" in sys.argv
    bad = run_synthetic_checks()
    if bad:
        print("### 合成校验失败(断路器,阻止实盘回测):")
        for b in bad:
            print(" -", b)
        sys.exit(1)
    print("合成校验通过(信号时点/回补/无未来函数/确认臂)")
    if only_synth:
        sys.exit(0)
    print(f"=== 个股状态机回测 v1{'b(确认臂)' if confirm else ''} 方案§14 ===")
    rows = []
    for code, label in SYMBOLS.items():
        path = CACHE / f"{code}_daily.csv"
        if not path.exists():
            print(f"{code} {label}: 缓存缺失")
            continue
        r = backtest_one(load_daily_csv(path), confirm=confirm, code=code)
        if "error" in r:
            print(f"{code} {label}: {r['error']}")
            continue
        rows.append(r)
        print(f"{code} {label}: 策略 {r['ret_pct']:+.1f}% (MDD {r['mdd_pct']:.1f}%) "
              f"vs 持有 {r['bh_ret_pct']:+.1f}% (MDD {r['bh_mdd_pct']:.1f}%) "
              f"卖出{r['sells']}次 顺延{r['blocked_sells']}")
    if rows:
        n = len(rows)
        sret = sum(r["ret_pct"] for r in rows) / n
        bret = sum(r["bh_ret_pct"] for r in rows) / n
        pmdd = _portfolio_mdd([r["equity"] for r in rows])
        bpmdd = _portfolio_mdd([r["bh_equity"] for r in rows])
        sells = sum(r["sells"] for r in rows)
        imp = (bpmdd - pmdd) / bpmdd * 100 if bpmdd else 0
        print(f"--- 组合等权({n}标的,真组合净值): 策略 {sret:+.1f}% (组合MDD {pmdd:.1f}%) "
              f"vs 持有 {bret:+.1f}% (组合MDD {bpmdd:.1f}%) | 回撤改善 {imp:.0f}% "
              f"| 卖出 {sells} 次")
        c1, c2, c3 = imp >= 15, sells >= 30, (sret - bret) >= -2
        print(f"判据(预注册§14): c1回撤改善≥15% [{'过' if c1 else '不过'}] "
              f"c2卖出≥30 [{'过' if c2 else '不过'}] "
              f"c3收益差≥-2pp [{'过' if c3 else '不过'}] "
              f"→ {'放行' if (c1 + c2 + c3) >= 2 else '不放行'}")
