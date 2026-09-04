# -*- coding: utf-8 -*-
"""PortfolioSim 单元测试:资金守恒/上限/尾日对齐(回归锁)。"""
from backtest_engine.core.portfolio_sim import PortfolioSim


def test_cash_conservation_and_caps():
    sim = PortfolioSim(100000.0, 1 / 3, ["A", "B"])
    cfg = {"slippage_rate": 0.0, "commission_rate": 0.0005, "lot_size": 100}
    cash0 = sim.pool_cash
    tr = sim.buy("A", "d1", 1.0, "trial", 10.0, "r", 10.0, cfg)
    assert tr is not None and sim.pool_cash < cash0
    assert sim.pool_cash >= 0
    # 层上限:重复 trial 买入到 10% 满配后拒绝
    for _ in range(30):
        sim.buy("A", "d2", 1.0, "trial", 10.0, "r", 10.0, cfg)
    inv = sim.layers["A"].trial * sim.layers["A"].cost("trial")
    assert inv <= sim._full_amount() * 0.10 + 1e-6
    # 卖出回笼
    cash_before = sim.pool_cash
    outs = sim.sell("A", "d3", 1.1, "clear_trial", "x", cfg)
    assert sim.pool_cash > cash_before and sim.layers["A"].trial == 0


def test_pool_cash_never_negative_on_sell():
    sim = PortfolioSim(10000.0, 1 / 3, ["A"])
    cfg = {"slippage_rate": 0.0, "commission_rate": 0.0005, "lot_size": 100}
    sim.buy("A", "d1", 0.5, "main", 45.0, "r", 45.0, cfg)
    assert sim.pool_cash >= 0
    sim.sell("A", "d2", 0.4, "clear_right_main", "cut", cfg)
    assert sim.pool_cash >= 0