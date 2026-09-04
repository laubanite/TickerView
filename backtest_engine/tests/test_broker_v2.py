# -*- coding: utf-8 -*-
"""broker_v2 分层单测(小样本 E2 硬标准:注入式动作覆盖)。"""
import pytest

from backtest_engine.core.broker_v2 import BrokerConfig, LayeredBroker


def _broker():
    b = LayeredBroker(BrokerConfig(initial_cash=10000.0, commission_rate=0.0005,
                                   slippage_rate=0.0, full_allocation=1 / 3))
    b.caps = {"trial": 10.0, "main_cap": 45.0, "chase_cap": 10.0, "third_cap": 33.0}
    return b


def test_buy_layer_caps_trial_and_main_shared():
    b = _broker()
    # trial 10%: 满配 3333.33 → 上限 333.33 → 约 0.10 元/股 → 3300 股整手
    b.buy_layer("2023-01-01", 0.10, "trial", 10.0, "right_initial")
    assert 3200 <= b.layers.trial <= 3400
    # 再买 trial 10% → 已达上限,不动作
    assert b.buy_layer("2023-01-02", 0.10, "trial", 10.0, "again") is None
    # main:②-B 22% → ②-A 45% 扣已占 → 合计 ≤45%
    b.buy_layer("2023-01-03", 0.10, "main", 22.0, "time_confirm", cap_group="main_cap")
    m1 = b.layers.main
    b.buy_layer("2023-01-04", 0.10, "main", 45.0, "pullback_confirm", cap_group="main_cap")
    assert 0 < b.layers.main - m1
    inv_main = b.layers.main * b.layers.main_cost
    assert inv_main <= b.full_amount * 0.45 + 1e-6
    # 买入第三笔 main → 已到 45% 上限
    assert b.buy_layer("2023-01-05", 0.10, "main", 22.0, "x", cap_group="main_cap") is None


def test_clear_right_main_keeps_trial():
    b = _broker()
    b.buy_layer("d1", 1.0, "trial", 10.0, "r1")
    b.buy_layer("d2", 1.0, "main", 45.0, "r2", cap_group="main_cap")
    b.sell_layer("d3", 1.1, "clear_right_main", "cut")
    assert b.layers.main == 0 and b.layers.trial > 0
    assert b.layers.trial_cost > 0


def test_clear_all_and_clear_trial():
    b = _broker()
    b.buy_layer("d1", 1.0, "trial", 10.0, "r1")
    b.buy_layer("d2", 1.0, "main", 45.0, "r2", cap_group="main_cap")
    outs = b.sell_layer("d3", 0.9, "clear_all", "stop")
    layers = {o.layer for o in outs}
    assert layers == {"main", "trial"} and b.layers.total == 0
    # clear_trial
    b.buy_layer("d4", 1.0, "trial", 10.0, "r3")
    b.sell_layer("d5", 0.8, "clear_trial", "invalid")
    assert b.layers.trial == 0


def test_clear_right_same_as_clear_to_anchor_keeps_anchor():
    for act in ("clear_right", "clear_to_anchor"):
        b = _broker()
        # 低价标的避开满配件 3%≈99.9999 元的浮点整手边缘
        b.buy_layer("d1", 0.1, "anchor", 3.0, "left")
        b.buy_layer("d2", 0.1, "trial", 10.0, "r1")
        b.buy_layer("d3", 0.1, "main", 45.0, "r2", cap_group="main_cap")
        assert b.layers.anchor > 0 and b.layers.trial > 0 and b.layers.main > 0
        b.sell_layer("d4", 0.11, act, "x")
        assert b.layers.anchor > 0 and b.layers.right_total == 0


def test_hygiene_raises_on_negative_cash():
    """现金为负(应被买入预算+整手约束规避;此处验证断言兜底)。"""
    b = _broker()
    b.buy_layer("d1", 0.01, "main", 45.0, "r", cap_group="main_cap")  # 低价满配内
    # 人为破坏现金 → 断言应炸
    b.cash = -1.0
    with pytest.raises(AssertionError):
        b._assert_health()