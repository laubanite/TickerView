# -*- coding: utf-8 -*-
"""broker_v2:分层持仓(anchor/trial/main)+ 语义化卖出动作。

分层模型(docs/策略规则-执行版.md §A):
  anchor: 左侧锚点仓(金额 ≤ 总资金 3%,v2 起用;v1 空)
  trial : 右侧 ①A 试探仓/右侧底仓(≤ 满配 10%)
  main  : 右侧 ②-A/②-B/③/追单 主仓批次(②-A+②-B ≤ 满配 45% 共享)
动作动词:clear_right / clear_right_main / clear_to_anchor / clear_all / clear_trial
不变式:anchor 只由左侧试多路径建仓;trial/main 只由右侧入场路径建仓。
撮合口径与 Layer3 一致:next_open 次 bar 开盘成交、佣金成交时扣、整手取整、滑点可选。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .broker_sim import BrokerConfig as BaseBrokerConfig


@dataclass
class BrokerConfig(BaseBrokerConfig):
    """继承基础配置(佣金/滑点/撮合/结算/整手),新增满配比例。"""
    full_allocation: float = 1 / 3


@dataclass
class PositionLayers:
    anchor: int = 0
    trial: int = 0
    main: int = 0
    anchor_cost: float = 0.0
    trial_cost: float = 0.0
    main_cost: float = 0.0

    @property
    def total(self) -> int:
        return self.anchor + self.trial + self.main

    @property
    def right_total(self) -> int:
        return self.trial + self.main

    def shares(self, layer: str) -> int:
        return getattr(self, layer, 0)

    def cost(self, layer: str) -> float:
        return getattr(self, f"{layer}_cost", 0.0)

    def right_weight_cost(self) -> float:
        """右侧独立加权成本(已拍板 T2:不混入 anchor)。"""
        if self.right_total <= 0:
            return 0.0
        tc = self.trial * self.trial_cost
        mc = self.main * self.main_cost
        return (tc + mc) / self.right_total

    def add(self, layer: str, shares: int, cost: float) -> None:
        """合并同时调高成本(加权平均)。"""
        cur = getattr(self, layer, 0)
        cur_cost = getattr(self, f"{layer}_cost", 0.0)
        new_total = cur + shares
        blended = ((cur * cur_cost + shares * cost) / new_total) if new_total else 0.0
        setattr(self, layer, new_total)
        setattr(self, f"{layer}_cost", blended)

    def remove(self, layer: str, shares: int) -> tuple[int, float]:
        """按层移除,返回 (实际移除股数, 成交金额不含费)。"""
        cur = getattr(self, layer, 0)
        gone = min(cur, shares)
        cost = getattr(self, f"{layer}_cost", 0.0)
        setattr(self, layer, cur - gone)
        if getattr(self, layer) == 0:
            setattr(self, f"{layer}_cost", 0.0)
        return gone, gone * cost


@dataclass
class TradeV2:
    date: str
    side: str                      # BUY | SELL
    layer: str                     # anchor | trial | main
    action: str                    # 触发规则动作(如 buy_10 / clear_right_main)
    price: float
    shares: int
    amount: float
    fee: float
    cash_after: float
    reason: str
    audit_hash: str = ""
    cond_snapshot: dict = field(default_factory=dict)   # E1:触发条件值快照


class LayeredBroker:
    """分层撮合器:满配百分比预算 / 共享上限 / 剩余额度 / 逐层成本 / 卫生断言钩子。"""

    def __init__(self, cfg: BrokerConfig):
        self.cfg = cfg
        self.cash: float = cfg.initial_cash
        self.layers = PositionLayers()
        self.trades: list[TradeV2] = []
        self.equity: list[dict] = []
        self.caps: dict = {}          # 注入: {"trial": 10.0, "main": 45.0, "chase": 10.0, "third": 33.0}
        self._hooks: list = []

    # ---------------------------------------------------------------- 基础工具
    def _fill(self, price: float, side: str) -> float:
        m = 1 + self.cfg.slippage_rate if side == "BUY" else 1 - self.cfg.slippage_rate
        return round(price * m, 4)

    def _fee(self, amount: float) -> float:
        return round(amount * self.cfg.commission_rate, 2)

    @property
    def full_amount(self) -> float:
        return round(self.cfg.initial_cash * self.cfg.full_allocation, 2)

    def invested(self) -> float:
        """各层已占金额(按层成本计)。"""
        return sum(self.layers.shares(l) * self.layers.cost(l)
                   for l in ("anchor", "trial", "main"))

    def remaining(self) -> float:
        return max(self.full_amount - self.invested(), 0.0)

    # ---------------------------------------------------------------- 买入
    def buy_layer(self, date: str, price: float, layer: str, pct_of_full: float,
                  reason: str, audit_hash: str = "", cap_group: str = "",
                  cap_pct: float | None = None,
                  cond_snapshot: dict | None = None) -> TradeV2 | None:
        """按「满配×pct%」预算买入指定层。cap_group 覆盖共享上限(如 main 45)。"""
        if layer not in ("anchor", "trial", "main"):
            raise ValueError(f"未知层: {layer}")
        target = self.full_amount * pct_of_full / 100.0
        # 共享上限校验:该层/组已占 + 本次 ≤ 上限金额
        group_cap = cap_pct if cap_pct is not None else self.caps.get(cap_group or layer)
        if group_cap is not None:
            invested_grp = self.layers.shares(layer) * self.layers.cost(layer)
            cap_amount = self.full_amount * group_cap / 100.0
            if invested_grp >= cap_amount - 1e-6:
                return None                       # 已达共享上限
            target = min(target, cap_amount - invested_grp)
        target = min(target, self.cash, self.remaining())
        fill = self._fill(price, "BUY")
        # 1e-9 容忍浮点边缘(如 3%×满配=99.9999 元,floor 归零)
        shares = int(math.floor((target + 1e-9) / fill / self.cfg.lot_size)) * self.cfg.lot_size
        # 硬夹:组上限按"金额+费"执行(取整边界会超 1-2 元,512890 实测 +1.23 触发断言)
        if group_cap is not None and shares > 0:
            room = cap_amount - invested_grp
            unit = fill * (1 + self.cfg.commission_rate)
            max_sh = int(math.floor(room / unit / self.cfg.lot_size)) * self.cfg.lot_size
            if max_sh < shares:
                shares = max_sh
        if shares <= 0:
            return None
        amount = round(shares * fill, 2)
        fee = self._fee(amount)
        self.cash = round(self.cash - amount - fee, 2)
        cost = (amount + fee) / shares
        self.layers.add(layer, shares, cost)
        tr = TradeV2(date, "BUY", layer, f"buy_{pct_of_full}", fill, shares, amount,
                     fee, self.cash, reason, audit_hash, cond_snapshot or {})
        self.trades.append(tr)
        self._assert_health()
        return tr

    # ---------------------------------------------------------------- 卖出
    def sell_layer(self, date: str, price: float, action: str,
                   reason: str, audit_hash: str = "",
                   cond_snapshot: dict | None = None) -> list[TradeV2]:
        """语义化卖出(action ∈ clear_right/clear_right_main/clear_to_anchor/
        clear_all/clear_trial)。返回成交记录列表(可能多笔,逐层)。"""
        fill = self._fill(price, "SELL")
        outs: list[TradeV2] = []
        if action in ("clear_right", "clear_to_anchor", "clear_all"):
            layers = ("main", "trial")
        elif action == "clear_right_main":
            layers = ("main",)
        elif action == "clear_trial":
            layers = ("trial",)
        elif action == "clear_anchor":
            layers = ("anchor",)
        else:
            raise ValueError(f"未知动作: {action}")
        for l in layers:
            shares = self.layers.shares(l)
            if shares <= 0:
                continue
            amount = round(shares * fill, 2)
            fee = self._fee(amount)
            self.cash = round(self.cash + amount - fee, 2)
            self.layers.remove(l, shares)
            outs.append(TradeV2(date, "SELL", l, action, fill, shares, amount, fee,
                                self.cash, reason, audit_hash, cond_snapshot or {}))
        self.trades.extend(outs)
        self._assert_health()
        return outs

    # ---------------------------------------------------------------- 净值与卫生
    def mark_equity(self, date: str, close: float) -> None:
        value = self.cash + sum(self.layers.shares(l) * close
                                for l in ("anchor", "trial", "main"))
        self.equity.append({"date": date, "close": close, "equity": round(value, 2),
                            "cash": round(self.cash, 2),
                            "anchor": self.layers.anchor, "trial": self.layers.trial,
                            "main": self.layers.main})

    def _assert_health(self) -> None:
        """E3 卫生断言:层非负 / 现金非负 / 单只累计 ≤ 满配(容差=金额级 0.05)。"""
        tol = 0.05
        for l in ("anchor", "trial", "main"):
            assert getattr(self.layers, l) >= 0, f"{l} 层负持仓"
        assert self.cash >= -1e-6, f"现金为负: {self.cash}"
        assert self.invested() <= self.full_amount + tol, \
            f"单只累计超满配: {self.invested():.2f} > {self.full_amount:.2f}"
        if self.caps.get("trial"):
            t_inv = self.layers.shares("trial") * self.layers.cost("trial")
            assert t_inv <= self.full_amount * self.caps["trial"] / 100 + tol, "trial 超上限"
        if self.caps.get("main"):
            m_inv = self.layers.shares("main") * self.layers.cost("main")
            assert m_inv <= self.full_amount * self.caps["main"] / 100 + tol, "main 超 45% 上限"