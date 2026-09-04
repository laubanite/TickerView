# -*- coding: utf-8 -*-
"""Layer 1:指标数学一致性(与 TA-Lib 逐日 diff)。

- TA-Lib 未安装(本沙箱 pypi 不可达)→ 全部自动 skip,并给出安装提示;
- 已安装:逐日对比 wrapped 输出 vs 直接调用 talib(误差 0);KDJ 与公式
  J=3K-2D 逐日核对;ATR/ADX/MACD 与 talib 原生逐日误差 < 0.001。
"""
import numpy as np
import pandas as pd
import pytest

from backtest_engine.core import indicator_calc as ic

HAS_TALIB = ic._HAS_TALIB

pytestmark = pytest.mark.skipif(
    not HAS_TALIB,
    reason="TA-Lib 未安装(pypi 沙箱不可达)——本机 `pip install ta-lib` 或 "
           "`conda install -c conda-forge ta-lib-python` 后全量运行 Layer 1")


@pytest.fixture
def series():
    rng = np.random.default_rng(7)
    n = 300
    close = pd.Series(10 + np.cumsum(rng.normal(0, 0.3, n)))
    high = close * (1 + rng.uniform(0.001, 0.01, n))
    low = close * (1 - rng.uniform(0.001, 0.01, n))
    return pd.DataFrame({"close": close, "high": high, "low": low})


def test_sma_equals_talib(series):
    import talib
    expect = talib.SMA(series["close"].to_numpy(dtype="float64"), timeperiod=20)
    got = ic.sma(series["close"], 20)
    valid = ~np.isnan(expect)
    assert np.max(np.abs(got.to_numpy()[valid] - expect[valid])) < 1e-9


def test_atr_diff_lt_0001(series):
    import talib
    expect = talib.ATR(series["high"].to_numpy(), series["low"].to_numpy(),
                       series["close"].to_numpy(), timeperiod=14)
    got = ic.atr(series["high"], series["low"], series["close"], 14)
    valid = ~np.isnan(expect)
    assert np.max(np.abs(got.to_numpy()[valid] - expect[valid])) < 0.001


def test_adx_diff_lt_0001(series):
    import talib
    expect = talib.ADX(series["high"].to_numpy(), series["low"].to_numpy(),
                       series["close"].to_numpy(), timeperiod=14)
    got = ic.adx(series["high"], series["low"], series["close"], 14)
    valid = ~np.isnan(expect)
    assert np.max(np.abs(got.to_numpy()[valid] - expect[valid])) < 0.001


def test_macd_diff_lt_0001(series):
    import talib
    d, e, h = talib.MACD(series["close"].to_numpy(dtype="float64"),
                         fastperiod=12, slowperiod=26, signalperiod=9)
    gd, ge, gh = ic.macd(series["close"])
    valid = ~np.isnan(d)
    assert np.max(np.abs(gd.to_numpy()[valid] - d[valid])) < 0.001
    assert np.max(np.abs(ge.to_numpy()[valid] - e[valid])) < 0.001
    assert np.max(np.abs(gh.to_numpy()[valid] - h[valid])) < 0.001


def test_kdj_j_formula(series):
    """KDJ 非 TA-Lib 原生:J=3K-2D 必须逐日精确(公式核对)。"""
    import talib
    k, dd = talib.STOCH(series["high"].to_numpy(), series["low"].to_numpy(),
                        series["close"].to_numpy(), fastk_period=9,
                        slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0)
    expect_j = 3.0 * k - 2.0 * dd
    got_j = ic.stoch_kdj(series["high"], series["low"], series["close"])
    valid = ~np.isnan(expect_j)
    assert np.max(np.abs(got_j.to_numpy()[valid] - expect_j[valid])) < 1e-9


def test_indicator_error_without_talib_is_explicit():
    """talib 缺失时必须显式失败,不静默降级(设计规范)。"""
    if HAS_TALIB:
        pytest.skip("已装 talib,降级路径无需测试")
    with pytest.raises(ic.IndicatorError):
        ic.sma(pd.Series([1.0] * 30), 20)