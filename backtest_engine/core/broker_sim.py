# -*- coding: utf-8 -*-
"""撮合模拟(与聚宽日频对齐):默认「收盘撮合」。

规则:
- 信号在 bar N 收盘产生 → 成交价 = bar N 收盘(与聚宽日频 order 近似);
- 佣金 = 成交金额 × commission_rate(双向);滑点 = 0(对标期固定,engine.yaml 可配);
- T+1:当日买入不可当日卖出(日频下天然满足,显式校验兜底);
- 全仓买卖:金额按可用现金(买入)/持仓市值(卖出),股数按 100 股取整(ETF 场内)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class BrokerConfig:
    initial_cash: float = 100000.0
    commission_rate: float = 0.0005        # ETF 万5,双向
    slippage_rate: float = 0.0002          # 单边固定滑点 0.02%(保守;买贵卖贱)
    matching: str = "close"                # close | open(未来扩展 VWAP)
    settlement_mode: str = "T1"            # T1:当日不可回转;T0:跨境/债券/商品/货币 ETF
    lot_size: int = 100


@dataclass
class Trade:
    date: str
    side: str                            # BUY | SELL
    price: float
    shares: int
    amount: float
    fee: float
    cash_after: float
    reason: str
    audit_hash: str = ""


@dataclass
class BrokerSim:
    cfg: BrokerConfig
    cash: float = field(init=False)
    shares: int = field(init=False)
    entry_price: float = 0.0             # 持仓成本(avg cost)
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.cfg.initial_cash
        self.shares = 0

    @property
    def in_position(self) -> bool:
        return self.shares > 0

    def mark_equity(self, date: str, close: float) -> None:
        value = self.cash + self.shares * close
        self.equity.append({"date": date, "close": close, "equity": round(value, 2)})

    def _fill_price(self, price: float, side: str) -> float:
        """固定滑点:买入抬价、卖出压价(单边 slippage_rate)。"""
        mult = 1 + self.cfg.slippage_rate if side == "BUY" else 1 - self.cfg.slippage_rate
        return round(price * mult, 4)

    def _fee(self, amount: float) -> float:
        return round(amount * self.cfg.commission_rate, 2)

    def buy_all(self, date: str, price: float, reason: str, audit_hash: str = "") -> Trade | None:
        if self.in_position:
            return None
        fill = self._fill_price(price, "BUY")
        # 预算 = 全部现金,不预留佣金(与聚宽 order_target_value(available_cash) 同公式,
        # 消除整手差;佣金成交时扣,与聚宽行为一致)
        shares = int(math.floor(self.cash / fill / self.cfg.lot_size)) * self.cfg.lot_size
        if shares <= 0:
            return None
        amount = round(shares * fill, 2)
        fee = self._fee(amount)
        self.cash = round(self.cash - amount - fee, 2)
        self.shares = shares
        self.entry_price = (amount + fee) / shares          # 含费成本(聚宽 avg_cost 口径)
        tr = Trade(date, "BUY", fill, shares, amount, fee, self.cash, reason, audit_hash)
        self.trades.append(tr)
        return tr

    def sell_all(self, date: str, price: float, reason: str, audit_hash: str = "") -> Trade | None:
        if not self.in_position:
            return None
        if self.cfg.settlement_mode == "T1" and self.trades \
                and self.trades[-1].date == date and self.trades[-1].side == "BUY":
            return None                                  # T+1 当日买入不可卖
        fill = self._fill_price(price, "SELL")
        amount = round(self.shares * fill, 2)
        fee = self._fee(amount)
        self.cash = round(self.cash + amount - fee, 2)
        tr = Trade(date, "SELL", fill, self.shares, amount, fee, self.cash, reason, audit_hash)
        self.shares = 0
        self.entry_price = 0.0
        self.trades.append(tr)
        return tr