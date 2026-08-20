"""作战地图回测:按 RuleModel 整套可计算规则跑历史区间,输出收益率/最大回撤/交易明细。

定位(产品方案 §4.1 + 方案讨论):测"规则骨架"整体有效性,不含消息/情绪信息层。
- 只执行可计算规则(回踩/突破/止损/红线/生命线)
- 关键位动态更新(MA 每日漂移,红线按当日 MA)
- 仓位/预算从作战地图 §五 读
- 结果可复核、可追溯

条件映射(作战地图标准模板):
  加仓(回踩):  收盘 ∈ [买区下沿, 买区上沿] 且 缩量企稳(量比<0.8 且未创新低)
  加仓(突破):  收盘 > 突破点 且 放量(量比>1.5)
  减仓红线:    收盘 < 减仓红线 且 放量
  生命线/清仓: 收盘 < 生命线
  新仓止损:    收盘 < 该笔买入的止损价
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .db import connect
from .planner.rulemodel import Instrument, RuleModel

logger = logging.getLogger(__name__)

VOL_UP = 1.5      # 放量阈值(量比)
VOL_DOWN = 0.8    # 缩量阈值


@dataclass
class Position:
    """单只标的的模拟仓位。"""
    code: str
    name: str = ""
    shares: int = 0
    avg_cost: float = 0.0
    last_buy_price: float = 0.0   # 最近一笔买入价(新仓止损锚定)
    last_buy_stop: float = 0.0    # 最近一笔买入的止损价
    budget_alloc: float = 0.0     # 分配预算金额(元)
    realized_pnl: float = 0.0     # 已实现盈亏(元)
    trades: list[dict] = field(default_factory=list)

    def buy(self, price: float, shares: int, date: str, reason: str, stop: float = 0.0) -> None:
        new_cost = self.avg_cost * self.shares + price * shares
        new_shares = self.shares + shares
        self.avg_cost = new_cost / new_shares if new_shares else 0.0
        self.shares = new_shares
        self.last_buy_price = price
        self.last_buy_stop = stop
        self.trades.append({"date": date, "action": "加仓", "price": round(price, 4),
                            "shares": shares, "reason": reason})

    def sell(self, price: float, shares: int, date: str, reason: str) -> None:
        shares = min(shares, self.shares)
        if shares <= 0:
            return
        self.realized_pnl += (price - self.avg_cost) * shares
        self.shares -= shares
        self.trades.append({"date": date, "action": "减仓", "price": round(price, 4),
                            "shares": -shares, "reason": reason})


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为日线 df 追加技术指标列(MA20/KDJ/量比/是否新低)。"""
    df = df.copy()
    df["ma20"] = df["close"].rolling(20).mean()
    # 量比:当日量 / 前 20 日均量(不含当日)
    df["vol_ma20"] = df["volume"].rolling(20).mean().shift(1)
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    # KDJ(9,3,3)
    lo9 = df["low"].rolling(9).min()
    hi9 = df["high"].rolling(9).max()
    rsv = (df["close"] - lo9) / (hi9 - lo9) * 100
    rsv = rsv.fillna(50.0)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    df["kdj_k"] = k
    df["kdj_d"] = d
    df["kdj_j"] = 3 * k - 2 * d
    # 是否创近 20 日新低(企稳判据)
    df["new_low20"] = df["close"] < df["low"].rolling(20).min().shift(1)
    return df


def _load_daily(symbol: str) -> pd.DataFrame:
    conn = connect()
    try:
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, volume, turnover "
            "FROM etf_kline_daily WHERE symbol=? ORDER BY trade_date",
            conn, params=(symbol,))
    finally:
        conn.close()
    return df


def _find_level(inst: Instrument, name: str) -> float | None:
    """取标的关键位(买区下沿/上沿/突破点/减仓红线/生命线/止损价)。"""
    vals = [l.price for l in inst.levels if l.name == name]
    return vals[0] if vals else None


class MapBacktester:
    """按作战地图整套可计算规则,在历史区间跑模拟。

    输入:RuleModel + 标的日线 + 区间 + 本金。
    输出:组合净值曲线、收益率/最大回撤、与买入持有对比、交易明细。
    """

    def __init__(self, model: RuleModel) -> None:
        self.model = model

    def run(self, start_date: str, end_date: str, capital: float) -> dict:
        instruments = self._select_instruments()
        if not instruments:
            return {"error": "作战地图无可回测标的(需含 levels 且可计算规则)"}

        # 加载每只标的日线 + 指标,截取 [start, end]
        bars: dict[str, pd.DataFrame] = {}
        for inst in instruments:
            df = _add_indicators(_load_daily(inst.code))
            df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)]
            if len(df) < 30:
                logger.warning("[%s] 区间内样本不足(<30根),跳过", inst.code)
                continue
            bars[inst.code] = df.reset_index(drop=True)
        if not bars:
            return {"error": "区间内无足够数据的标的"}

        # 预算分配(从作战地图 §五;缺失等分)
        alloc = self._budget_allocation(instruments, capital)

        positions = {i.code: Position(code=i.code, name=i.name, budget_alloc=alloc.get(i.code, 0))
                     for i in instruments if i.code in bars}
        nav_curve = []
        benchmark_curve = []
        cash = capital

        all_dates = sorted({d for df in bars.values() for d in df["trade_date"]})
        for date in all_dates:
            for inst in instruments:
                if inst.code not in bars:
                    continue
                df = bars[inst.code]
                row = df[df["trade_date"] == date]
                if row.empty:
                    continue
                i = row.index[0]
                pos = positions[inst.code]
                self._evaluate_bar(inst, df, i, pos, date)

            # 当日组合净值
            nav = cash
            for inst in instruments:
                if inst.code not in bars:
                    continue
                sub = bars[inst.code][bars[inst.code]["trade_date"] == date]
                if sub.empty:
                    continue
                close = float(sub["close"].iloc[0])
                nav += positions[inst.code].shares * close
            nav_curve.append((date, nav))

            # 买入持有基准(等权满仓,归一化到本金)
            bval = 0.0
            for inst in instruments:
                if inst.code not in bars:
                    continue
                sub = bars[inst.code][bars[inst.code]["trade_date"] <= date]
                if sub.empty:
                    continue
                first = float(bars[inst.code]["close"].iloc[0])
                last = float(sub["close"].iloc[-1])
                share = capital / len(instruments)
                bval += share * last / first if first else 0
            benchmark_curve.append((date, bval))

        trades_flat = [t for p in positions.values() for t in p.trades]
        return self._metrics(nav_curve, benchmark_curve, trades_flat, capital, instruments)

    def _select_instruments(self) -> list[Instrument]:
        """只取有 levels 且至少一条可计算规则的标的。"""
        out = []
        for inst in self.model.instruments:
            if inst.levels and any(r.computable for r in inst.rules):
                out.append(inst)
        return out

    def _budget_allocation(self, instruments: list[Instrument], capital: float) -> dict[str, float]:
        """预算分配(元):优先读作战地图 §五 budget_items;缺失则等分。"""
        alloc: dict[str, float] = {}
        items = self.model.global_.budget_items
        if items:
            for it in items:
                if it.code and it.budget_pct is not None:
                    alloc[it.code] = capital * it.budget_pct / 100
        covered = set(alloc)
        missing = [i.code for i in instruments if i.code not in covered]
        if missing:
            remaining = capital - sum(alloc.values())
            per = remaining / len(missing) if missing else 0
            for code in missing:
                alloc[code] = per
        return alloc

    def _evaluate_bar(self, inst: Instrument, df: pd.DataFrame, i: int,
                      pos: Position, date: str) -> None:
        close = float(df["close"].iloc[i])
        vr = float(df["vol_ratio"].iloc[i]) if not pd.isna(df["vol_ratio"].iloc[i]) else 1.0
        new_low = bool(df["new_low20"].iloc[i])
        buy_low = _find_level(inst, "买区下沿") or _find_level(inst, "买区")
        buy_high = _find_level(inst, "买区上沿")
        brk = _find_level(inst, "突破点")
        cut = _find_level(inst, "减仓红线")
        life = _find_level(inst, "生命线")
        stop = _find_level(inst, "止损价")

        # ---- 卖出优先(红线/生命线/止损) ----
        if pos.shares > 0:
            if life and close < life:
                pos.sell(close, pos.shares, date, f"跌破生命线 {life:.3f}")
                return
            if cut and close < cut and vr >= VOL_UP:
                pos.sell(close, pos.shares, date, f"放量跌破减仓红线 {cut:.3f}")
                return
            if pos.last_buy_stop and close < pos.last_buy_stop:
                pos.sell(close, pos.shares, date, f"新仓止损 {pos.last_buy_stop:.3f}")
                return

        # ---- 买入(仅当空仓且预算>0) ----
        if pos.shares == 0 and pos.budget_alloc > 0:
            if buy_low and close >= buy_low and (buy_high is None or close <= buy_high):
                if vr <= VOL_DOWN and not new_low:   # 缩量企稳
                    shares = int(pos.budget_alloc / close / 100) * 100
                    if shares > 0:
                        pos.buy(close, shares, date,
                                f"回踩买区 {buy_low:.3f}-{buy_high or buy_low:.3f} 缩量企稳",
                                stop=stop or 0)
                        return
            if brk and close > brk and vr >= VOL_UP:
                shares = int(pos.budget_alloc / close / 100) * 100
                if shares > 0:
                    pos.buy(close, shares, date, f"放量突破 {brk:.3f}", stop=stop or 0)

    @staticmethod
    def _metrics(nav_curve, benchmark_curve, trades, capital, instruments) -> dict:
        if not nav_curve:
            return {"error": "无净值曲线"}
        dates = [d for d, _ in nav_curve]
        navs = [n for _, n in nav_curve]
        bench = [b for _, b in benchmark_curve]

        total_ret = (navs[-1] / capital - 1) * 100 if capital else 0
        peak = navs[0]
        mdd = 0.0
        for n in navs:
            peak = max(peak, n)
            mdd = min(mdd, (n - peak) / peak)
        bh_ret = (bench[-1] / capital - 1) * 100 if capital and bench else 0
        alpha = total_ret - bh_ret
        days = max(len(navs), 1)
        years = days / 250
        ann = ((navs[-1] / capital) ** (1 / years) - 1) * 100 if years > 0 and capital > 0 else 0

        return {
            "period": f"{dates[0]} ~ {dates[-1]}",
            "trading_days": len(navs),
            "capital": capital,
            "nav_curve": nav_curve,
            "benchmark_curve": benchmark_curve,
            "trades": trades,
            "trade_count": len(trades),
            "total_return_pct": round(total_ret, 2),
            "annualized_pct": round(ann, 2),
            "max_drawdown_pct": round(mdd * 100, 2),
            "benchmark_return_pct": round(bh_ret, 2),
            "alpha_pct": round(alpha, 2),
            "instruments": [i.code for i in instruments],
            "note": "仅执行可计算规则(不含消息/情绪信息层);关键位动态更新",
        }


def run_map_backtest(model: RuleModel, start_date: str, end_date: str,
                     capital: float) -> dict:
    """便捷入口:作战地图 RuleModel → 回测结果。"""
    return MapBacktester(model).run(start_date, end_date, capital)
