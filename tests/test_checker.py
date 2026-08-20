"""核对引擎回归测试(checker.py,里程碑3)。

纯逻辑(不依赖网络):结论词判定 classify / 量能标签 vol_label / 大盘门控 KDJ。
"""
from __future__ import annotations

from alphaprism.planner.checker import (
    BREAK, BUY_HIT, CALM, NEAR_BUY, NEAR_SELL, WAIT,
    check_instrument, classify, kdj_j, vol_label,
)
from alphaprism.planner.rulemodel import Instrument, Level


def _inst(code="516020", levels=None):
    return Instrument(code=code, name="测试", levels=levels or [])


def test_classify_buy_zone():
    """买区内:缩量企稳+门控开=触发;门控关=等待;平量=接近买点。"""
    # 化工 买区0.855-0.860 突破0.885 红线0.85 生命线0.778
    assert classify(0.857, 0.855, 0.860, 0.885, 0.85, 0.778, 0.7, True)[0] == BUY_HIT
    assert classify(0.857, 0.855, 0.860, 0.885, 0.85, 0.778, 0.7, False)[0] == WAIT
    assert classify(0.857, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == NEAR_BUY


def test_classify_near_buy_zone():
    """买区下沿下方 1.5% 内(未破红线)= 接近买点;门控关 = 等待。"""
    assert classify(0.852, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == NEAR_BUY
    assert classify(0.852, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, False)[0] == WAIT


def test_classify_breakout():
    """突破点对称窗口:放量=触发;平量=接近买点;突破后明显远离=平静。"""
    assert classify(0.880, 0.855, 0.860, 0.885, 0.85, 0.778, 1.8, True)[0] == BUY_HIT
    assert classify(0.880, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == NEAR_BUY
    assert classify(0.90, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == CALM


def test_classify_sell_side():
    """跌破减仓红线=接近卖点;跌破生命线=破位(优先级最高)。"""
    assert classify(0.849, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == NEAR_SELL
    assert classify(0.845, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == NEAR_SELL
    # 生命线 0.778 之下 → 破位(即使贴近买区也不误判)
    assert classify(0.770, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == BREAK


def test_classify_calm():
    """买区与突破点之间、突破后远离、无现价 → 平静。"""
    assert classify(0.870, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == CALM
    assert classify(0.90, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == CALM
    assert classify(None, 0.855, 0.860, 0.885, 0.85, 0.778, 1.0, True)[0] == CALM


def test_vol_label():
    """量比 → 放量/缩量/平量(阈值 1.5 / 0.8)。"""
    assert vol_label(2.0) == "放量"
    assert vol_label(0.5) == "缩量"
    assert vol_label(1.0) == "平量"
    assert vol_label(None) == ""


def test_kdj_j():
    """9 日 KDJ J 值:单调上涨序列 J 高(>60),数据不足返回 None。"""
    uptrend = [100 + i for i in range(20)]
    assert kdj_j(uptrend) is not None and kdj_j(uptrend) > 60
    assert kdj_j([1, 2, 3]) is None  # 不足 9 根


def test_check_instrument_verdict():
    """check_instrument 返回 Verdict:无价位标的不误判(无计划)。"""
    inst = _inst()
    v = check_instrument(inst, 1.0, 1.0, True)
    assert v.conclusion == CALM
    assert any("无关键价位" in r for r in v.reasons)
    d = v.to_dict()
    assert d["code"] == "516020" and "conclusion" in d


def test_check_instrument_with_levels():
    """有买区价位:现价在买区内 + 缩量 + 门控开 → 买点触发。"""
    inst = _inst(levels=[
        Level(name="买区下沿", price=0.855),
        Level(name="买区上沿", price=0.860),
        Level(name="突破点", price=0.885),
        Level(name="减仓红线", price=0.85),
        Level(name="生命线", price=0.778),
    ])
    v = check_instrument(inst, 0.857, 0.7, True)
    assert v.conclusion == BUY_HIT
    assert v.vol_label == "缩量"
