"""作战地图回测引擎回归测试(backtest_map.py)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from alphaprism.planner.parser import parse_text

# 迷你作战地图(含可回测的 levels + 规则)
SAMPLE = """# 测试作战地图

- **文档日期**：2026-08-20（周四）
- **数据来源**：测试数据

---

## 一、大盘环境

| 项目 | 数值 | 解读 |
|------|------|------|
| 收盘 | 3894.42 | 放量阴线 |

**环境结论**：大盘破位，大盘 J 值回落到 60 以下前，所有 ETF 不加仓。

---

## 二、ETF 一览表

| # | 名称 | 代码 | 持有股数 | 成本 | 现价 |
|---|------|------|---------|------|------|
| 1 | 化工ETF华宝 | 516020.SH | 2000 | 0.879 | 0.863 |

---

## 三、逐只详细分析

### 1. 化工ETF华宝 516020

**关键位地图**：
- 压力：0.873 → 0.884
- 支撑：0.859 → 0.850

**操作建议**：

| 动作 | 触发条件 | 具体操作 |
|------|---------|---------|
| 加仓①（回踩低吸） | 回踩 0.855-0.860 缩量企稳 | 加第一批 |
| 减仓红线 | 放量跌破 0.849 | 减 1/3 |

---

## 五、操作优先级与资金分配

**资金现状**：可用子弹约 10000 元。

| 优先级 | 品种 | 预算占比 |
|--------|------|---------|
| 1 | 化工 516020 | 100% |

---

## 八、触发信号速查

| 品种 | 买点（回踩） | 买点（突破） | 减仓红线 | 生命线 |
|------|------------|------------|---------|--------|
| 化工 516020 | 0.855-0.860 | 0.885 | 0.849 | 0.778 |
"""


@pytest.fixture
def model():
    return parse_text(SAMPLE)


def test_select_instruments(model):
    from alphaprism.backtest_map import MapBacktester

    bt = MapBacktester(model)
    insts = bt._select_instruments()
    assert len(insts) == 1
    assert insts[0].code == "516020"


def test_budget_allocation(model):
    from alphaprism.backtest_map import MapBacktester

    bt = MapBacktester(model)
    alloc = bt._budget_allocation(bt._select_instruments(), 10000)
    assert abs(alloc["516020"] - 10000) < 1  # 100% 预算


def test_levels_extracted(model):
    inst = model.instrument_by_code("516020")
    by_name = {l.name: l.price for l in inst.levels}
    assert by_name.get("买区下沿") == 0.855
    assert by_name.get("买区上沿") == 0.860
    assert by_name.get("突破点") == 0.885
    assert by_name.get("减仓红线") == 0.849
    assert by_name.get("生命线") == 0.778


def test_run_returns_metrics(model):
    from alphaprism.backtest_map import MapBacktester

    bt = MapBacktester(model)
    r = bt.run("2025-01-01", "2026-01-01", 10000)
    if "error" in r:
        pytest.skip(f"回测无数据: {r['error']}")
    assert "total_return_pct" in r
    assert "max_drawdown_pct" in r
    assert "benchmark_return_pct" in r
    assert "alpha_pct" in r
    assert "nav_curve" in r
    assert r["nav_curve"], "应有净值曲线"


def test_no_anomalous_prices(model):
    """交易价格不应出现 MA 周期之类的异常值(如 20.0)。"""
    from alphaprism.backtest_map import MapBacktester

    bt = MapBacktester(model)
    r = bt.run("2025-01-01", "2026-01-01", 10000)
    if "error" in r:
        pytest.skip(f"回测无数据: {r['error']}")
    for t in r["trades"]:
        assert t["price"] < 15, f"异常价位: {t}"


def test_real_battle_map_backtest():
    """真实作战地图应能跑通回测(区间内数据足够时)。"""
    path = Path(r"E:\AITrader\七只ETF作战地图_2026-08-17.md")
    if not path.exists():
        pytest.skip("真实作战地图不存在")
    from alphaprism.backtest_map import run_map_backtest
    from alphaprism.planner.parser import parse_file

    m = parse_file(path)
    r = run_map_backtest(m, "2025-08-01", "2026-08-01", 17300)
    if "error" in r:
        pytest.skip(f"回测无数据: {r['error']}")
    assert "total_return_pct" in r
    assert len(r["trades"]) >= 0
    # 无异常价位
    for t in r["trades"]:
        assert t["price"] < 15
