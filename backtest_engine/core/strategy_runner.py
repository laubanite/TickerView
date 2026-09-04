# -*- coding: utf-8 -*-
"""策略执行器:读取 YAML,不内置策略。

schema(声明式,无 eval/无任意代码):
```yaml
name: breakout_stop
indicators:                       # 需要引擎预计算的指标(引擎注册表,缺失即报错)
  - {id: m20, type: sma, params: {period: 20}}
signals:
  entry:
    type: cross_above_prev_peak   # 收盘 > 前 lookback 根最高价(不含当日)
    lookback: 20
  stop:
    type: below_entry_pct         # 收盘 <= 成本价 × (1 - pct/100)
    pct: 8.0
```
- 引擎 bar 循环中按 lookback 无未来:cross_above_prev_peak 使用 shift(1) 滚动最高;
- 未来扩展:新信号类型在本文件注册表注册(仍是 YAML 声明,不写策略逻辑)。
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml

from .indicator_calc import IndicatorError


class StrategyError(RuntimeError):
    pass


@dataclass
class BarSignals:
    entry: bool
    stop: bool
    reason: str = ""


REGISTRY = {"cross_above_prev_peak", "below_entry_pct"}


def load_strategy(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if "signals" not in cfg or "entry" not in cfg["signals"]:
        raise StrategyError("策略缺少 signals.entry 定义")
    for sk, sv in cfg["signals"].items():
        if sv.get("type") not in REGISTRY:
            raise StrategyError(f"未知信号类型: {sv.get('type')} ({sk})")
    return cfg


def evaluate_signals(cfg: dict, bar: dict, entry_price: float,
                     prev_peak: float | None) -> BarSignals:
    """bar: {date, open, high, low, close, volume};entry_price=0 表示空仓。"""
    entry = False
    stop = False
    en = cfg["signals"]["entry"]
    if en["type"] == "cross_above_prev_peak":
        close = float(bar["close"])
        lb = int(en.get("lookback", 20))
        entry = prev_peak is not None and close > prev_peak
    else:
        raise StrategyError(f"信号类型未实现: {en['type']}")
    st = cfg["signals"].get("stop")
    if st and st.get("type") == "below_entry_pct" and entry_price > 0:
        pct = float(st.get("pct", 8.0))
        stop = float(bar["close"]) <= entry_price * (1 - pct / 100)
    reason = ""
    if entry:
        reason = f"突破近{int(en.get('lookback', 20))}日高点"
    if stop:
        reason = f"跌破成本 {pct:.0f}%(入场价 {entry_price:.3f})"
    return BarSignals(entry, stop, reason)