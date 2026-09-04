# -*- coding: utf-8 -*-
"""v2.1 资金池组合模拟器(多标的共享资金池,事件驱动,暂无截面评分)。

语义(与 v2 单标的完全一致,资金改为池化):
- 每标的独立运行分层信号(YAML 全同:alpha_v2 右侧+左侧并存规则);
- 动作执行作用于共享资金池:买入扣池内现金、卖出回笼;
- 资金不足:右侧优先(先清该标的 anchor 腾仓);多标的同日争抢 → 按成员顺序分配(无评分
  阶段的占位规则,记 [v2.1 口径]);
- 满配 = pool_initial × full_allocation;单标的投资 ≤ 满配;
- 声明式分批起点:每成员 since 日才入池参与(数据切片从 max(available, since));
- 产出:池权益曲线/收益/回撤/资金利用率/年化换手/费用拖累 + 逐标的归因。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .broker_v2 import PositionLayers
from .strategy_v2 import StrategyError
from .stock_market_rules import STAMP_DUTY, limit_pct, limit_prices


@dataclass
class PoolMember:
    symbol: str
    since: str = ""            # 声明式分批起点(空=数据可得即入)


@dataclass
class PoolStats:
    buys: int = 0
    sells: int = 0
    fees: float = 0.0
    turnover_amount: float = 0.0


class PortfolioSim:
    def __init__(self, pool_cash: float, full_allocation: float = 1 / 3,
                 member_order: list[str] | None = None):
        self.pool_cash = pool_cash
        self.initial = pool_cash
        self.full_allocation = full_allocation
        self.member_order = member_order or []
        # 每标的持仓/状态/信号
        self.layers: dict[str, PositionLayers] = defaultdict(PositionLayers)
        self.state: dict[str, dict] = defaultdict(lambda: {
            "halted": False, "break_level": None,
            "anchor_peak": None, "anchor_entry_close": None, "anchor_hold": 0})
        self.pending: dict[str, list] = defaultdict(list)
        self.equity: list[dict] = []
        self.stats = PoolStats()
        self.attr: dict[str, float] = defaultdict(float)   # 逐标的已实现 P&L
        self.stocks: set[str] = set()                      # 个股成员(涨跌停/印花税适配)
        self.blocked_buys = 0                              # 涨停拒买次数
        self.deferred_sells = 0                            # 跌停顺延卖出次数
        self.trades: list[dict] = []                       # 逐笔成交(含费/层/理由)

    # ---------------------------------------------------------------- 资金/额度
    def _full_amount(self) -> float:
        return round(self.initial * self.full_allocation, 2)

    def _invested(self, sym: str) -> float:
        L = self.layers[sym]
        return sum(L.shares(l) * L.cost(l) for l in ("anchor", "trial", "main"))

    def _cap_left(self, sym: str) -> float:
        return max(self._full_amount() - self._invested(sym), 0.0)

    # ---------------------------------------------------------------- 动作执行
    def _fill(self, price: float, side: str, cfg) -> float:
        m = 1 + cfg["slippage_rate"] if side == "BUY" else 1 - cfg["slippage_rate"]
        return round(price * m, 4)

    def buy(self, sym: str, date: str, price: float, layer: str, pct_of_full: float,
            reason: str, cap_pct: float | None, cfg: dict,
            prev_close: float | None = None) -> dict | None:
        # 个股涨跌停闸门:开盘 ≥ 涨停价(容差内)→ 追买拒单(信号作废,不重试)
        if sym in self.stocks and prev_close:
            buy_max, _ = limit_prices(float(prev_close), limit_pct(sym))
            if buy_max and price >= buy_max:
                self.blocked_buys += 1
                return None
        L = self.layers[sym]
        full = self._full_amount()
        target = full * pct_of_full / 100.0
        if cap_pct is not None:
            grp_inv = L.shares(layer) * L.cost(layer)
            cap_amt = full * cap_pct / 100.0
            if grp_inv >= cap_amt - 1e-6:
                return None
            target = min(target, cap_amt - grp_inv)
        limit = min(self.pool_cash, self._cap_left(sym))
        target = min(target, limit)
        fill = self._fill(price, "BUY", cfg)
        shares = int(math.floor((target + 1e-9) / fill / cfg["lot_size"])) * cfg["lot_size"]
        if shares <= 0:
            return None
        amount = round(shares * fill, 2)
        fee = round(amount * cfg["commission_rate"], 2)
        self.pool_cash = round(self.pool_cash - amount - fee, 2)
        cost = (amount + fee) / shares
        L.add(layer, shares, cost)
        self.stats.buys += 1
        self.stats.fees += fee
        self.stats.turnover_amount += amount
        tr = {"date": date, "sym": sym, "side": "BUY", "layer": layer, "price": fill,
              "shares": shares, "amount": amount, "fee": fee,
              "cash_after": self.pool_cash, "reason": reason}
        self.trades.append(tr)
        return tr

    def sell(self, sym: str, date: str, price: float, action: str,
             reason: str, cfg: dict, prev_close: float | None = None) -> list[dict] | None:
        """卖出执行。返回 executed 交易列表(可为空 = 无可卖层);
        跌停顺延返回 None(持仓不动,调用方应保留 pending 次日重试)——与"无持仓"
        的空列表严格区分。"""
        L = self.layers[sym]
        # 个股跌停闸门:开盘 ≤ 跌停价(容差内)且跌停幅 <20% → 整笔顺延次日
        # (20cm 标的 20% 一字跌停罕见,顺延排队意义有限,照常成交 [声明:近似]);
        # 顺延 = 持仓不动,当前 pending 丢弃,由调用方按次日 pending 重新评估执行。
        if (sym in self.stocks and prev_close and L.total > 0
                and limit_pct(sym) < 20.0):
            _, sell_min = limit_prices(float(prev_close), limit_pct(sym))
            if sell_min and price <= sell_min:
                self.deferred_sells += 1
                return None
        fill = self._fill(price, "SELL", cfg)
        outs = []
        if action in ("clear_right", "clear_to_anchor", "clear_all"):
            layers = ("main", "trial", "anchor") if action == "clear_all" else ("main", "trial")
        elif action == "clear_right_main":
            layers = ("main",)
        elif action in ("clear_trial", "clear_anchor"):
            layers = (action.removeprefix("clear_"),)
        else:
            raise StrategyError(f"未知动作: {action}")
        for l in layers:
            shares = L.shares(l)
            if shares <= 0:
                continue
            amount = round(shares * fill, 2)
            stamp = round(amount * STAMP_DUTY, 2) if sym in self.stocks else 0.0
            fee = round(amount * cfg["commission_rate"], 2) + stamp
            self.pool_cash = round(self.pool_cash + amount - fee, 2)
            basis = shares * L.cost(l)
            self.attr[sym] += (amount - fee) - basis      # 逐标的已实现归因
            L.remove(l, shares)
            self.stats.sells += 1
            self.stats.fees += fee
            self.stats.turnover_amount += amount
            self.trades.append({"date": date, "sym": sym, "side": "SELL", "layer": l,
                                "price": fill, "shares": shares, "amount": amount,
                                "fee": fee, "cash_after": self.pool_cash,
                                "reason": reason})
            outs.append({"date": date, "side": "SELL", "layer": l, "price": fill,
                         "shares": shares, "amount": amount, "fee": fee,
                         "cash_after": self.pool_cash, "reason": reason})
        # 归因:卖出已实现盈亏 = 卖出金额-费 - 对应买入(近似:层成本×股数)
        return outs

    def mark_equity(self, date: str, closes: dict[str, float]) -> None:
        val = self.pool_cash
        invested = 0.0
        for sym, L in self.layers.items():
            mv = sum(L.shares(l) * closes.get(sym, 0.0) for l in ("anchor", "trial", "main"))
            val += mv
            invested += mv
        self.equity.append({"date": date, "equity": round(val, 2),
                            "cash": round(self.pool_cash, 2),
                            "invested": round(invested, 2)})