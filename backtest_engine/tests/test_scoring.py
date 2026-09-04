# -*- coding: utf-8 -*-
"""v2.2 截面评分单测:分位归一 / 换手带。"""
from backtest_engine.core.scoring import (percentile_ranks, pool_scores,
                                          rotation_trigger, factor_value)


def test_percentile_ranks():
    r = percentile_ranks({"a": 1.0, "b": 2.0, "c": 3.0})
    assert r["a"] == 0.0 and r["c"] == 1.0
    r2 = percentile_ranks({"a": 1.0, "b": 1.0})     # 并列
    assert r2["a"] == r2["b"] == 0.5


def test_pool_scores_weights():
    """0-100 归一:y 最高分 = Σw×100/Σw = 100。"""
    cfg = {"right": [{"factor": "rs20", "weight": 2.0}]}
    sv = lambda rs: {"rs20": rs, "close": 1.0, "prev20_high": 1.0, "volume": 1.0,
                     "vol20": 1.0, "adx14": 1.0}
    scores = pool_scores({"x": sv(0.01), "y": sv(0.15), "z": sv(0.05)}, "right", cfg)
    assert scores["y"] == 100.0 and scores["x"] == 0.0 and scores["z"] == 50.0


def test_pool_scores_needs_full_pool():
    """分位必须在全池上计算:单标的传入 → 恒 50(v2.3 修复 bug 回归锁)。"""
    cfg = {"right": [{"factor": "rs20", "weight": 2.0}]}
    sv = {"rs20": 0.1, "close": 1.0, "prev20_high": 1.0, "volume": 1.0,
          "vol20": 1.0, "adx14": 1.0}
    scores = pool_scores({"only": sv}, "right", cfg)
    assert scores["only"] == 50.0   # n=1 → 唯一标的=中位(50),但池内对比必须全池


def test_rotation_trigger_band():
    band = {"score_gap_pct": 25.0, "rank_gap": 2}
    assert rotation_trigger(1.0, 1.3, 3, 1, band) is True     # 30% 优势 + 领先 2 位
    assert rotation_trigger(1.0, 1.2, 3, 1, band) is False    # 20% 优势不够
    assert rotation_trigger(1.0, 1.3, 3, 2, band) is False    # 只领先 1 位


def test_unknown_factor_raises():
    import pytest
    with pytest.raises(ValueError):
        factor_value("no_such", {"close": 1.0})