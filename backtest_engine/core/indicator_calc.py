# -*- coding: utf-8 -*-
"""指标计算:唯一出口 = TA-Lib(talib),禁止手搓滚动窗口。

- 全部函数签名为 (df 或 series) → 同长 pd.Series(前 lookback 根为 NaN,与聚宽一致);
- talib 未安装 → IndicatorError(引擎在此明确失败,不静默降级);
- 测试层(tests/test_indicator_accuracy.py)在无 talib 时自动 skip,本机装
  `pip install ta-lib` 后全量跑 Layer1。

数据操作(rolling_max 等)不属于 TA 指标,由 strategy_runner 的数据层提供。
KDJ:TA-Lib 无 KDJ,用 STOCH 衍生(K=fastk, D=slowd, J=3K-2D)——写入本模块并在
Layer1 测试中与公式逐日核对(非手搓指标,是 TA-Lib 组合)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import talib  # noqa: F401
    _HAS_TALIB = True
except Exception:  # noqa: BLE001
    # 沙箱/无 pip 环境:探测工作区 vendor(.bt-vendor,ta-lib wheel 手工解包)
    import sys
    from pathlib import Path

    _VENDOR = Path(__file__).resolve().parent.parent.parent / ".bt-vendor"
    if _VENDOR.exists():
        sys.path.insert(0, str(_VENDOR))
        try:
            import talib  # noqa: F401
            _HAS_TALIB = True
        except Exception:  # noqa: BLE001
            _HAS_TALIB = False
    else:
        _HAS_TALIB = False


class IndicatorError(RuntimeError):
    pass


def _arr(s: pd.Series) -> np.ndarray:
    return s.to_numpy(dtype="float64")


def _series(x: np.ndarray, idx) -> pd.Series:
    return pd.Series(x, index=idx)


def require_talib() -> None:
    """引擎对指标的唯一硬依赖:talib 缺失必须显式失败。"""
    if not _HAS_TALIB:
        raise IndicatorError(
            "TA-Lib 未安装:请 `pip install ta-lib`(引擎禁止手搓指标)。"
            "仅有滚动最高价/收盘价的数据操作策略可在无 talib 下运行。")


def sma(close: pd.Series, period: int = 20) -> pd.Series:
    require_talib()
    return _series(talib.SMA(_arr(close), timeperiod=period), close.index)


def ema(close: pd.Series, period: int = 12) -> pd.Series:
    require_talib()
    return _series(talib.EMA(_arr(close), timeperiod=period), close.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    require_talib()
    return _series(talib.ATR(_arr(high), _arr(low), _arr(close), timeperiod=period),
                   close.index)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    require_talib()
    return _series(talib.ADX(_arr(high), _arr(low), _arr(close), timeperiod=period),
                   close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist)(与聚宽 MACD 同口径)。"""
    require_talib()
    dif, dea, hist = talib.MACD(_arr(close), fastperiod=fast, slowperiod=slow,
                                signalperiod=signal)
    return (_series(dif, close.index), _series(dea, close.index),
            _series(hist, close.index))


def stoch_kdj(high: pd.Series, low: pd.Series, close: pd.Series,
              kp: int = 9, dp: int = 3) -> pd.Series:
    """J = 3K - 2D(TA-Lib STOCH 衍生,聚宽 KDJ 同口径)。"""
    require_talib()
    k, d = talib.STOCH(_arr(high), _arr(low), _arr(close), fastk_period=kp,
                       slowk_period=3, slowk_matype=0, slowd_period=dp, slowd_matype=0)
    j = 3.0 * k - 2.0 * d
    return _series(j, close.index)


def stoch_kd(high: pd.Series, low: pd.Series, close: pd.Series,
             kp: int = 9, dp: int = 3) -> tuple[pd.Series, pd.Series]:
    """K/D 原始对(strategy_v2 金叉判定用)。"""
    require_talib()
    k, d = talib.STOCH(_arr(high), _arr(low), _arr(close), fastk_period=kp,
                       slowk_period=3, slowk_matype=0, slowd_period=dp, slowd_matype=0)
    return _series(k, close.index), _series(d, close.index)


def rolling_max(s: pd.Series, lookback: int) -> pd.Series:
    """数据操作(非 TA 指标):前 lookback 根(不含当日)的最高价序列。"""
    return s.shift(1).rolling(lookback).max()