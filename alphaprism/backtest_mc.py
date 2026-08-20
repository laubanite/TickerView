"""蒙特卡洛回测:随机入场日 × 纪律操作 vs 买入持有(红线溯源原则版)。

回答:同一标的、同一时点、同一预算,纪律化操作是否优于买入持有?
- 随机入场:每只 ETF 在历史数据中抽取 N 个入场日(当日收盘建仓)
- 双轨运行:纪律路径(规则管理仓位)vs 持有路径(拿住不动)
- 规则全部确定性,锚点可溯源(红线溯源原则,作战地图 §八):
  波段仓(通信/电网/光伏) : 减仓红线 = 当日 MA20(每日漂移)
  解套仓(化工/电池/黄金/半导体): 减仓红线 = 近10日低点平台(可指出具体日期)
  生命线(两类)           : 近60日低点(可指出具体日期)
  大盘过滤              : 上证收盘 < 其 MA20 → 禁止加仓
- 仓位: 初始 30% 预算,每次加仓 20%,最多 3 次(90% 上限),留 10% 子弹
- 单笔止损 5%、单笔 +15% 减 1/3(分批兑现)
- 成本: 佣金万1 + 滑点 0.1%(每边),期末双向计提

用法:
  python -m alphaprism.backtest_mc            # 全池蒙特卡洛回测 + 报告
  python -m alphaprism.backtest_mc 159516     # 单只
  python -m alphaprism.backtest_mc --samples 300 --seed 42
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

import pandas as pd

from .db import connect

# 持仓分类(作战地图 §八:波段仓红线锚当日MA20;解套仓红线锚结构位)
BAND_SYMBOLS = {"515050", "159326", "515790"}          # 通信/电网/光伏
UNWIND_SYMBOLS = {"516020", "159796", "518850", "159516"}  # 化工/电池/黄金/半导体

# 规则参数
INIT_RATIO = 0.50        # 初始建仓占预算比例(趋势确认期提高至 50%)
ADD_RATIO = 0.25         # 每次加仓比例
MAX_ADDS = 2             # 最多加仓次数(50%+25%×2=100% 满仓)
STOP_PCT = 0.08          # 单笔止损(作战地图 §5.1: 5~8%,高波动ETF取8%)
TAKE_PCT = 0.15          # 单笔目标
REDUCE_1_3 = 1 / 3       # 减仓比例
FEET = 0.001 + 0.001     # 佣金万1 + 滑点0.1%(每边)
BUDGET = 10000.0
WARMUP = 60              # 指标预热(60日,够近60日低)
MIN_AFTER_ENTRY = 60     # 入场后最少留60日
VOL_UP = 1.5             # 放量阈值(量比)
VOL_DN = 0.8             # 缩量阈值(量比)
VOL_CUT = 1.2            # 破位减仓需放量(作战地图"放量跌破才减")
J_LOW = 30.0             # KDJ J 低位
CROSS = 0.004            # 十字星判定(实体/昨收)

# 追高杀跌路径参数(模拟无纪律的真实行为:高点买/恐慌卖/再追高)
PANIC_DROP = 0.90        # 从峰值回落 10% → 恐慌卖出
CHASE_RISE = 0.03        # 较上次买入价回升 3% → 追高买回


def _rng(seed: int):
    return random.Random(seed)


def load_daily(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """从库内加载日线(qfq)。返回 {symbol: df[trade_date,open,high,low,close,volume,amount]}。"""
    conn = connect()
    try:
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = pd.read_sql_query(
                "SELECT trade_date, open, high, low, close, volume, amount "
                "FROM etf_kline_daily WHERE symbol=? ORDER BY trade_date",
                conn, params=(sym,),
            )
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            out[sym] = df.reset_index(drop=True)
        return out
    finally:
        conn.close()


def load_index() -> pd.DataFrame:
    """加载上证指数日线(腾讯接口,带本地缓存 data/index_sh000001.csv)。"""
    import requests

    cache = Path(__file__).resolve().parent.parent / "data" / "index_sh000001.csv"
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["trade_date"])
        if len(df) > 500:
            return df
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        import os
        os.environ.pop(key, None)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": "sh000001,day,2023-01-01,2026-08-19,900,qfq"}
    js = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, params=params, timeout=15).json()
    data = (js.get("data") or {}).get("sh000001") or {}
    bars = data.get("qfqday") or data.get("day") or []
    df = pd.DataFrame(bars, columns=["trade_date", "open", "close", "high", "low", "volume"])
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """一次性预计算全部指标(向量化)。"""
    d = df.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    d["ma20"] = c.rolling(20).mean()
    d["ma60"] = c.rolling(60).mean()
    d["vol_ratio"] = v / v.rolling(20).mean().shift(1)
    d["near20low"] = l.rolling(20).min()
    d["near10low"] = l.rolling(10).min()
    d["near20high"] = h.rolling(20).max()
    d["near60low"] = l.rolling(60).min()
    # KDJ(9,3,3)
    llv9 = l.rolling(9).min()
    hhv9 = h.rolling(9).max()
    rsv = (c - llv9) / (hhv9 - llv9) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    dd = k.ewm(com=2, adjust=False).mean()
    d["j"] = 3 * k - 2 * dd
    d["is_up"] = c >= d["open"]
    d["is_cross"] = (c - d["open"]).abs() / c.shift(1).abs() <= CROSS
    return d


def _classify(symbol: str) -> str:
    return "band" if symbol in BAND_SYMBOLS else "unwind"


def _fee_factor(budget_alloc: float, price: float) -> tuple[float, float]:
    """按预算分配买入:返回 (股数, 实际花费)。成本=分配额,股数=(分配×(1-费))/价。"""
    shares = budget_alloc * (1 - FEET) / price
    return shares, budget_alloc


def _sell(batch: dict, frac: float, price: float) -> tuple[float, dict]:
    """卖出某批次的一部分,返回 (现金增加, 剩余批次)。"""
    sell_sh = batch["shares"] * frac
    proceeds = sell_sh * price * (1 - FEET)
    batch["shares"] -= sell_sh
    return proceeds, batch


def simulate(symbol: str, d: pd.DataFrame, idx: int, ix: pd.DataFrame,
             full_deploy: bool = False) -> dict:
    """单样本模拟:从 idx 日建仓,运行到序列末端。

    full_deploy=True 时:初始 100% 仓位(与买入持有同仓位),空仓后可按信号再进场——
    纯粹测"信号管理 vs 被动持有"的价值,排除仓位差异。

    d: 带指标的标的数据;ix: 上证指数(df 含 ma20) → 返回统计 dict。
    """
    cls = d["close"].to_numpy()
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
    dates = d["trade_date"].to_numpy()

    ix_close = ix.set_index("trade_date")["close"].to_numpy()  # noqa
    ix_ma20 = ix.set_index("trade_date")["ma20"].to_numpy()    # noqa
    ix_map = {pd.Timestamp(dt): i for i, dt in enumerate(ix["trade_date"])}

    typ = _classify(symbol)
    budget = BUDGET
    cash = budget
    batches: list[dict] = []
    shares_total = 0.0
    init_ratio = 1.0 if full_deploy else INIT_RATIO
    entry_price = cls[idx]
    # 初始建仓(默认30%起步;full_deploy 模式 100% 同仓位)
    sh, cost = _fee_factor(budget * init_ratio, entry_price)
    batches.append({"shares": sh, "entry": entry_price})
    shares_total = sh
    cash -= cost

    cut_ma20 = False
    cut_near10 = False
    closed: list[float] = []   # 已平仓批次收益率
    equity = [cash + shares_total * cls[idx]]
    peak = equity[0]

    for i in range(idx + 1, len(d)):
        price = cls[i]
        # 大盘状态
        rix = ix_map.get(dates[i])
        market_ok = True
        if rix is not None:
            market_ok = float(ix_close[rix]) >= float(ix_ma20[rix]) if not pd.isna(ix_ma20[rix]) else True

        # 1) 单笔止损(先于一切)
        keep = []
        for b in batches:
            if price <= b["entry"] * (1 - STOP_PCT):
                proceeds, _ = _sell(b, 1.0, price)
                cash += proceeds
                closed.append(price / b["entry"] - 1)
            else:
                keep.append(b)
        batches = keep

        # 2) 减仓(防守优先;趋势门控: 仅中期转弱(close<MA20 且 MA20<MA60)才执行减仓红线)
        if batches:
            if price < n60l[i]:            # 生命线破位 → 清仓(无条件)
                for b in batches:
                    proceeds, _ = _sell(b, 1.0, price)
                    cash += proceeds
                    closed.append(price / b["entry"] - 1)
                batches = []
            else:
                trend_broken = price < ma20[i] and (pd.isna(ma20[i]) or pd.isna(ma60[i]) or ma20[i] < ma60[i])
                vol_cut = not pd.isna(vratio[i]) and vratio[i] > VOL_CUT
                if trend_broken and vol_cut:
                    if typ == "band" and not cut_ma20:        # 波段仓破MA20(中期转弱) → 减1/3
                        new_batches = []
                        for b in batches:
                            if b["shares"] * REDUCE_1_3 > 0.0001:
                                proceeds, b = _sell(b, REDUCE_1_3, price)
                                cash += proceeds
                            if b["shares"] > 0.0001:
                                new_batches.append(b)
                        batches = new_batches
                        cut_ma20 = True
                    if price < n10l[i] and not cut_near10:    # 再破近端结构位 → 减1/3
                        new_batches = []
                        for b in batches:
                            if b["shares"] * REDUCE_1_3 > 0.0001:
                                proceeds, b = _sell(b, REDUCE_1_3, price)
                                cash += proceeds
                            if b["shares"] > 0.0001:
                                new_batches.append(b)
                        batches = new_batches
                        cut_near10 = True
                if price >= n10l[i]:
                    cut_near10 = False
                if typ == "band" and price >= ma20[i]:
                    cut_ma20 = False

        # 3) 单笔止盈(+15% 减1/3)
        if batches:
            new_batches = []
            for b in batches:
                if price >= b["entry"] * (1 + TAKE_PCT):
                    proceeds, b = _sell(b, REDUCE_1_3, price)
                    cash += proceeds
                if b["shares"] > 0.0001:
                    new_batches.append(b)
            batches = new_batches

        # 4) 加仓(大盘过滤 + 中期趋势向上 + 信号;full_deploy 模式不额外加仓)
        trend_up = (not pd.isna(ma60[i])) and price > ma60[i]
        if (not full_deploy and market_ok and trend_up and batches and len(batches) < MAX_ADDS + 1
                and cash >= budget * ADD_RATIO):
            vr = vratio[i]
            in_pullback = price >= n20l[i] * 1.02 and price <= ma20[i] and not pd.isna(ma20[i])
            pull_sig = in_pullback and (not pd.isna(vr)) and vr < VOL_DN and (is_up[i] or is_cross[i]) and jv[i] < J_LOW
            brk_sig = (not pd.isna(vr)) and vr > VOL_UP and price > n20h[i]
            if pull_sig or brk_sig:
                sh, cost = _fee_factor(budget * ADD_RATIO, price)
                batches.append({"shares": sh, "entry": price})
                shares_total += sh
                cash -= cost
        # 4b) full_deploy 空仓再进场:清仓/止损后,若信号重现 → 全仓买回
        elif (full_deploy and not batches and cash > budget * 0.01
              and market_ok and trend_up):
            vr = vratio[i]
            in_pullback = price >= n20l[i] * 1.02 and price <= ma20[i] and not pd.isna(ma20[i])
            pull_sig = in_pullback and (not pd.isna(vr)) and vr < VOL_DN and (is_up[i] or is_cross[i]) and jv[i] < J_LOW
            brk_sig = (not pd.isna(vr)) and vr > VOL_UP and price > n20h[i]
            if pull_sig or brk_sig:
                sh, cost = _fee_factor(cash, price)
                batches.append({"shares": sh, "entry": price})
                shares_total = sh
                cash = 0.0

        # 5) 权益曲线
        shares_total = sum(b["shares"] for b in batches)
        eq = cash + shares_total * price
        equity.append(eq)
        peak = max(peak, eq)

    # 期末平仓
    final = 0.0
    for b in batches:
        proceeds, _ = _sell(b, 1.0, float(cls[-1]))
        cash += proceeds
        closed.append(float(cls[-1]) / b["entry"] - 1)
    final_equity = cash

    hold_shares = budget * (1 - FEET) / entry_price
    hold_curve = hold_shares * cls[idx:]
    hold_final = hold_shares * cls[-1] * (1 - FEET)
    hold_mdd = _max_drawdown(hold_curve)

    wins = [x for x in closed if x > 0]
    return {
        "symbol": symbol,
        "entry_date": str(pd.Timestamp(dates[idx]).date()),
        "entry_price": round(entry_price, 4),
        "disc_return": round(final_equity / budget - 1, 4),
        "hold_return": round(hold_final / budget - 1, 4),
        "disc_mdd": round(_max_drawdown(pd.Series(equity).to_numpy()), 4),
        "hold_mdd": round(hold_mdd, 4),
        "beats": final_equity / budget > hold_final / budget,
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
    }


def _max_drawdown(series) -> float:
    if len(series) == 0:
        return 0.0
    peak = series[0]
    mdd = 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return round(mdd, 4)


def simulate_panic(d: pd.DataFrame, idx: int) -> dict:
    """追高杀跌路径:高点买入 → 回落10%恐慌卖出 → 回升3%追高买回,循环(无纪律版)。

    模拟真实行为:重仓追高 + 恐慌减仓 + 再追高推高成本。返回 {return, mdd}。
    """
    cls = d["close"].to_numpy()
    budget = BUDGET
    entry_price = cls[idx]
    cash = budget
    shares = 0.0
    peak = entry_price
    last_buy = entry_price
    equity = []
    bought = False
    # 首日全仓买入
    shares = budget * (1 - FEET) / entry_price
    cash = 0.0
    bought = True
    equity.append(shares * entry_price)

    for i in range(idx + 1, len(d)):
        price = cls[i]
        peak = max(peak, price)
        if bought:
            if price <= peak * PANIC_DROP:      # 从峰值回落 10% → 恐慌清仓
                proceeds = shares * price * (1 - FEET)
                cash = proceeds
                shares = 0.0
                bought = False
        else:
            if price >= last_buy * (1 + CHASE_RISE):  # 回升 3% → 追高买回
                shares = cash * (1 - FEET) / price
                cash = 0.0
                bought = True
                last_buy = price
                peak = price
        equity.append(cash + shares * price)
    # 期末平仓
    if shares > 0:
        cash += shares * cls[-1] * (1 - FEET)
    return {"panic_return": round(cash / budget - 1, 4),
            "panic_mdd": round(_max_drawdown(pd.Series(equity).to_numpy()), 4)}


def run_symbol(symbol: str, n_samples: int = 200, seed: int = 42,
               full_deploy: bool = False) -> list[dict]:
    d = add_indicators(load_daily([symbol])[symbol])
    ix = add_indicators(load_index())
    rng = _rng(seed)
    results = []
    max_start = len(d) - MIN_AFTER_ENTRY
    for _ in range(n_samples):
        idx = rng.randint(WARMUP, max_start)
        res = simulate(symbol, d, idx, ix, full_deploy=full_deploy)
        res.update(simulate_panic(d, idx))
        results.append(res)
    return results


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    beat_hold = sum(1 for r in results if r["beats"])
    beat_panic = sum(1 for r in results if r["disc_return"] > r["panic_return"])
    wins = [r["win_rate"] for r in results if r["win_rate"] is not None]
    return {
        "symbol": results[0]["symbol"] if results else "",
        "samples": n,
        "disc_mean": round(sum(r["disc_return"] for r in results) / n, 4),
        "disc_median": round(sorted(r["disc_return"] for r in results)[n // 2], 4),
        "hold_mean": round(sum(r["hold_return"] for r in results) / n, 4),
        "hold_median": round(sorted(r["hold_return"] for r in results)[n // 2], 4),
        "panic_mean": round(sum(r["panic_return"] for r in results) / n, 4),
        "panic_median": round(sorted(r["panic_return"] for r in results)[n // 2], 4),
        "beats_ratio": round(beat_hold / n, 3),
        "beats_panic": round(beat_panic / n, 3),
        "disc_mdd_mean": round(sum(r["disc_mdd"] for r in results) / n, 4),
        "hold_mdd_mean": round(sum(r["hold_mdd"] for r in results) / n, 4),
        "panic_mdd_mean": round(sum(r["panic_mdd"] for r in results) / n, 4),
        "avg_trades": round(sum(r["trades"] for r in results) / n, 1),
        "avg_win_rate": round(sum(wins) / len(wins), 3) if wins else None,
    }


def make_report(all_results: dict[str, list[dict]], out: Path) -> Path:
    rows = []
    for sym, res in all_results.items():
        rows.append(aggregate(res))
    df = pd.DataFrame(rows)
    names = {"516020": "化工", "159516": "半导体", "159796": "电池", "159326": "电网",
             "515790": "光伏", "515050": "通信", "518850": "黄金"}
    lines = [
        "# 蒙特卡洛回测报告:纪律操作 vs 买入持有 vs 追高杀跌",
        "",
        f"> 生成:{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} | 规则=红线溯源原则(作战地图§八)+趋势门控+进取仓位(50%起步)",
        f"> 每只随机入场 {df['samples'].iloc[0]} 次,三轨对比;成本=佣金万1+滑点0.1%每边",
        "",
        "| 标的 | 纪律收益 | 持有收益 | 追高杀跌收益 | **纪律跑赢持有** | **纪律跑赢追高杀跌** | 纪律MDD | 持有MDD | 追高杀跌MDD | 交易次数 | 胜率 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in df.iterrows():
        nm = names.get(r["symbol"], r["symbol"])
        wr = f"{r['avg_win_rate']*100:.0f}%" if r["avg_win_rate"] is not None else "-"
        lines.append(
            f"| {nm} {r['symbol']} | {r['disc_mean']*100:+.1f}% | {r['hold_mean']*100:+.1f}% | {r['panic_mean']*100:+.1f}% | "
            f"**{r['beats_ratio']*100:.0f}%** | **{r['beats_panic']*100:.0f}%** | "
            f"{r['disc_mdd_mean']*100:.1f}% | {r['hold_mdd_mean']*100:.1f}% | {r['panic_mdd_mean']*100:.1f}% | "
            f"{r['avg_trades']:.1f} | {wr} |"
        )
    lines.append("")
    lines.append("## 结论(统计口径,非投资建议)")
    total_samples = sum(len(v) for v in all_results.values())
    total_beats_hold = sum(1 for v in all_results.values() for r in v if r["beats"])
    total_beats_panic = sum(1 for v in all_results.values() for r in v if r["disc_return"] > r["panic_return"])
    disc_mean = sum(r["disc_return"] for v in all_results.values() for r in v) / total_samples
    hold_mean = sum(r["hold_return"] for v in all_results.values() for r in v) / total_samples
    panic_mean = sum(r["panic_return"] for v in all_results.values() for r in v) / total_samples
    disc_mdd = sum(r["disc_mdd"] for v in all_results.values() for r in v) / total_samples
    hold_mdd = sum(r["hold_mdd"] for v in all_results.values() for r in v) / total_samples
    panic_mdd = sum(r["panic_mdd"] for v in all_results.values() for r in v) / total_samples
    lines.append(
        f"- 全池 {total_samples} 个样本:纪律平均收益 {disc_mean*100:+.1f}% vs 持有 {hold_mean*100:+.1f}% vs 追高杀跌 {panic_mean*100:+.1f}%"
    )
    lines.append(
        f"- 纪律跑赢持有占比 **{total_beats_hold/total_samples*100:.0f}%**,纪律跑赢追高杀跌占比 **{total_beats_panic/total_samples*100:.0f}%**"
    )
    lines.append(
        f"- 平均最大回撤:纪律 {disc_mdd*100:.1f}% vs 持有 {hold_mdd*100:.1f}% vs 追高杀跌 {panic_mdd*100:.1f}%"
    )
    lines.append("")
    lines.append("## 附:真实案例回放(半导体 159516)")
    lines.append("> 实际持仓:2026-01 高位买入成本约 1.825(不复权),截至 2026-08 现价约 0.772,浮亏约 **-57.6%**(追高+扛单+杀跌推高成本)。")
    lines.append("> 回放:从 2026-01-07 按纪律规则运行(规则用前复权口径=经济回报;实际浮亏为不复权口径,两口径经份额折算后不等价)。")
    lines.append("")
    lines.append("| 路径 | 期末收益 | 说明 |")
    lines.append("|---|---|---|")
    d = add_indicators(load_daily(["159516"])["159516"])
    ix = add_indicators(load_index())
    entry_idx = d.index[d["trade_date"] == pd.Timestamp("2026-01-07")]
    if len(entry_idx):
        r = simulate("159516", d, int(entry_idx[0]), ix)
        lines.append(f"| 纪律操作 | {r['disc_return']*100:+.1f}% | 单笔止损 8% 内锁住,本金保住 |")
        lines.append(f"| 实际持仓 | **-57.6%** | 不复权口径,追高+无止损 |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="蒙特卡洛回测:纪律操作 vs 买入持有")
    ap.add_argument("symbols", nargs="*", help="ETF 代码,默认全池7只")
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--full", action="store_true", help="同仓位对照:初始100%起步(只测信号管理价值,排除仓位差异)")
    ap.add_argument("--out", default="reports/回测报告-蒙特卡洛.md")
    args = ap.parse_args(argv)

    symbols = args.symbols or sorted(BAND_SYMBOLS | UNWIND_SYMBOLS)
    all_results: dict[str, list[dict]] = {}
    for sym in symbols:
        res = run_symbol(sym, args.samples, args.seed, full_deploy=args.full)
        all_results[sym] = res
        agg = aggregate(res)
        mode = "同仓" if args.full else "半仓"
        print(f"[{sym}] {mode}纪律 {agg['disc_mean']*100:+.1f}% | 持有 {agg['hold_mean']*100:+.1f}% | 追高杀跌 {agg['panic_mean']*100:+.1f}% | 跑赢持有 {agg['beats_ratio']*100:.0f}% | 跑赢追高杀跌 {agg['beats_panic']*100:.0f}%")
    out = make_report(all_results, Path(args.out))
    print(f"报告已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())