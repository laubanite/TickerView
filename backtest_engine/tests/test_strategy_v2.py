# -*- coding: utf-8 -*-
"""strategy_v2 信号引擎单测(门控/单动作/条件组件)。"""
import pytest

from backtest_engine.core.broker_v2 import BrokerConfig, LayeredBroker
from backtest_engine.core.strategy_v2 import (StrategyError, _eval_cond,
                                              evaluate_bar, load_layered_strategy)

YAML = r"E:\AgentProjects\AlphaPrism\backtest_engine\configs\strat_alpha_v1.yaml"
CFG = load_layered_strategy(YAML)


def _ctx(close=1.0, open_=1.0, volume=100.0, series=None, state=None, layers=None):
    if series is None:
        series = {"prev20_high": 0.99, "adx14": 25.0, "vol20": 60.0, "vol5": 90.0,
                  "m_20": 1.01, "low20_close": 0.95, "low250_close": 0.80,
                  "entry_break_level": 0.99, "kdj_k": 55.0, "kdj_d": 50.0,
                  "kdj_j": 65.0, "kdj_k_prev": 51.0, "kdj_d_prev": 52.0}
    return {"date": "d", "open": open_, "close": close, "volume": volume,
            "series": series, "state": state or {},
            "layers": layers or LayeredBroker(BrokerConfig()).layers}


def test_days_since_requires_fired():
    assert _eval_cond(_ctx(state={}), {"type": "days_since", "since": "right_initial",
                                       "ge": 1}) is False
    st = {"fired_right_initial": True, "since_right_initial": 2}
    assert _eval_cond(_ctx(state=st), {"type": "days_since", "since": "right_initial",
                                       "ge": 1, "le": 1}) is False
    st2 = {"fired_right_initial": True, "since_right_initial": 1}
    assert _eval_cond(_ctx(state=st2), {"type": "days_since", "since": "right_initial",
                                        "ge": 1, "le": 1}) is True


def test_halt_gates_entries_except_right_initial():
    st = {"halted": True}
    # 构造无退出触发的上下文(close 高于所有档位),仅考察入场门控
    s = {"m_20": 0.95, "low20_close": 0.90, "low250_close": 0.85,
         "prev20_high": 0.99, "adx14": 25.0, "vol20": 60.0, "vol5": 90.0,
         "entry_break_level": 0.99, "kdj_k": 55.0, "kdj_d": 50.0, "kdj_j": 65.0,
         "kdj_k_prev": 51.0, "kdj_d_prev": 52.0}
    ctx = _ctx(state=st, series=s)
    fired = evaluate_bar(CFG, ctx)
    assert len(fired) == 1 and fired[0]["id"] == "right_initial"   # 只放行重置信号


def test_single_action_per_bar():
    # 一个 bar 同时满足多个信号 → 只触发第一个(退出优先)
    st = {"fired_right_initial": True, "since_right_initial": 5, "halted": False}
    ctx = _ctx(close=0.85, volume=200.0, state=st)   # 破位+减仓档同时
    fired = evaluate_bar(CFG, ctx)
    assert len(fired) == 1
    assert fired[0]["id"] in ("stop_level", "break_m20", "cut_level")


def test_unknown_condition_raises():
    with pytest.raises(StrategyError):
        _eval_cond(_ctx(), {"type": "no_such_cond"})


def test_pullback_requires_right_initial_fired():
    ctx = _ctx(close=1.05, state={})   # 企稳+缩量但 right_initial 从未触发
    fired = evaluate_bar(CFG, ctx)
    assert all(f["id"] != "pullback_confirm" for f in fired)


def test_kdj_golden_cross_component():
    s = {"kdj_k": 40.0, "kdj_d": 35.0, "kdj_j": 25.0, "kdj_k_prev": 33.0,
         "kdj_d_prev": 34.0, "m_20": 0.99}
    assert _eval_cond(_ctx(close=1.0, series=s),
                      {"type": "kdj_golden_cross", "j_max": 30}) is True
    s["kdj_j"] = 40.0
    assert _eval_cond(_ctx(close=1.0, series=s),
                      {"type": "kdj_golden_cross", "j_max": 30}) is False