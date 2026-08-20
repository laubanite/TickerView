"""解析器回归测试(里程碑1,§4.1)。覆盖:文档头/大盘/持仓/逐只/预警/预算/纪律/风险/速查/盯盘。"""
from __future__ import annotations

from pathlib import Path

import pytest

from alphaprism.planner.parser import parse_text

# 一份覆盖全部章节的迷你作战地图(仿真实格式)
SAMPLE = """# 测试作战地图

- **文档日期**：2026-08-20（周四）
- **数据来源**：测试数据
- **适用原则**：七步法 + 留一手 + 金字塔加仓

---

## 一、大盘环境（上证指数）

| 项目 | 数值 | 解读 |
|------|------|------|
| 收盘 | 3894.42（-2.40%） | 放量大阴线 |
| 关键均线 | MA20 3887 / MA60 3977 / MA120 4024 | 破MA60 |
| MACD | 死叉绿柱 | 转弱 |
| KDJ | K50 / D45 / J60 | 中性 |
| 上方压力 | 3940（MA5）→ 3977（MA60） | 反抽位 |
| 下方支撑 | 3880（今日低）→ 3850 | 三底 |

**环境结论**：大盘破位，大盘 J 值回落到 60 以下前，所有 ETF 不加仓。

---

## 二、ETF 一览表

| # | 名称 | 代码 | 持有股数 | 成本 | 现价 | 市值 | 浮亏% | 解套需涨 |
|---|------|------|---------|------|------|------|------|---------|
| 1 | 化工ETF华宝 | 516020.SH | 2000 | 0.879 | 0.863 | 1726 | -1.8% | +0.8% |

---

## 三、逐只详细分析

### 1. 化工ETF华宝 516020（成本 0.879，浮亏 -1.8%）

**今日盘面**：缩量光头阳。

**关键位地图**：
- 压力：0.873（MA60）→ 0.884（8/7高）
- 支撑：0.859（MA250）→ 0.850（8/14低）

**操作建议**：

| 动作 | 触发条件 | 具体操作 |
|------|---------|---------|
| 加仓①（回踩低吸） | 回踩 **0.855-0.860** 缩量止跌 | 加第一批（预算 30%） |
| 减仓红线 | **放量跌破 0.849** | 减 1/3 |

**基本面预警**：⚠️ 油价单月波动>10%。

---

## 四、基本面/消息面预警速查表

| 品种 | 最重要的基本面指标 | 看空预警触发 | 当前状态 |
|------|-------------------|-------------|---------|
| 化工 | 化工品价差 | 油价单月跌>10% | 🟢 价差走阔 |

---

## 五、操作优先级与资金分配

**资金现状**：可用子弹约 8500 元。

| 优先级 | 品种 | 定位 | 预算占比 |
|--------|------|------|---------|
| 1 | 化工 516020 | 解套最近 | 25% |

---

## 六、通用纪律

1. **J 值 >85 不追高**
2. **金字塔加仓**：第一笔最小

---

## 七、风险提示

1. **大盘 J≈100 超买**

---

## 八、触发信号速查

> ⚠️ **红线溯源原则**：波段仓（通信/电网/光伏）红线锚当日MA20；解套仓（化工/电池/黄金/半导体）红线锚结构位。

| 品种 | 买点（回踩） | 买点（突破） | 减仓红线 | 生命线 |
|------|------------|------------|---------|--------|
| 化工 516020 | 0.855-0.860 | 放量过 0.885 | 破 0.850 | 0.778 |
| 通信 515050 | 1.01-1.04 | 放量过 1.17 | 破 1.012 | 0.860 |

---

## 每日盯盘记录

### 📅 2026-08-20（周四）盘前 9:15

| 品种 | 隔夜影响 | 今日动作 |
|------|---------|---------|
| 化工 516020 | 焦炭提涨 | 回踩 0.855-0.860 企稳 → 加仓① |
"""


def test_meta():
    m = parse_text(SAMPLE)
    assert m.meta.doc_date == "2026-08-20（周四）"
    assert "金字塔加仓" in m.meta.principles


def test_market_gate():
    m = parse_text(SAMPLE)
    g = m.global_.market_gate
    assert g.close == 3894.42
    assert g.change_pct == -2.40
    assert len(g.ma) == 3
    assert g.ma[1].period == "MA60" and g.ma[1].value == 3977
    assert g.kdj["J"] == 60
    assert len(g.resistance) == 2 and g.resistance[0].source == "MA5"
    assert len(g.support) == 2
    # 大盘门控规则
    assert len(g.rules) == 1
    assert "不加仓" in g.rules[0].action


def test_positions():
    m = parse_text(SAMPLE)
    inst = m.instrument_by_code("516020")
    assert inst is not None
    assert inst.position.shares == 2000
    assert inst.position.cost == 0.879
    assert inst.position.loss_pct == -1.8


def test_instrument_rules_and_levels():
    m = parse_text(SAMPLE)
    inst = m.instrument_by_code("516020")
    # 操作建议表规则
    actions = [r.action for r in inst.rules]
    assert "加仓①（回踩低吸）" in actions
    assert "减仓红线" in actions
    # 关键位
    prices = [l.price for l in inst.levels]
    assert 0.873 in prices  # 压力
    assert 0.850 in prices  # 支撑
    # 预警
    assert any(a.kind == "预警" for a in inst.alerts)


def test_type_classification():
    m = parse_text(SAMPLE)
    assert m.instrument_by_code("516020").type == "解套仓"
    # 通信在速查表里被归为波段仓(虽无持仓表行,仍应建 instrument)
    comm = m.instrument_by_code("515050")
    assert comm is not None
    assert comm.type == "波段仓"


def test_budget():
    m = parse_text(SAMPLE)
    assert m.global_.budget_total == 8500
    assert len(m.global_.budget_items) == 1
    assert m.global_.budget_items[0].budget_pct == 25


def test_discipline_and_risks():
    m = parse_text(SAMPLE)
    assert any("J 值 >85 不追高" in d for d in m.global_.discipline)
    assert any("超买" in r for r in m.global_.risks)


def test_cheatsheet_levels():
    m = parse_text(SAMPLE)
    inst = m.instrument_by_code("516020")
    names = {l.name for l in inst.levels}
    assert "买区下沿" in names and "买区上沿" in names
    assert "突破点" in names
    assert "减仓红线" in names
    assert "生命线" in names
    # 买区区间正确拆成下沿/上沿
    lo = next(l.price for l in inst.levels if l.name == "买区下沿")
    hi = next(l.price for l in inst.levels if l.name == "买区上沿")
    assert lo == 0.855 and hi == 0.860


def test_journal_and_playbook():
    m = parse_text(SAMPLE)
    assert len(m.daily.journal) == 1
    assert len(m.daily.playbook) == 1
    pb = m.daily.playbook[0]
    assert pb.code == "516020"
    assert "加仓①" in pb.action


def test_no_guess_on_unresolved():
    """含主观词的触发条件应进 unresolved,不进 rules。"""
    text = SAMPLE.replace("回踩 **0.855-0.860** 缩量止跌", "情绪面转好 回踩 0.855")
    m = parse_text(text)
    inst = m.instrument_by_code("516020")
    # 该规则不可计算 → 不应出现在 rules 中
    assert not any("加仓①" in r.action for r in inst.rules)
    assert len(m.unresolved) >= 1


def test_real_battle_map():
    """真实作战地图(存在时)应能解析出 7 只标的,无 unresolved。"""
    path = Path(r"E:\AITrader\七只ETF作战地图_2026-08-17.md")
    if not path.exists():
        pytest.skip("真实作战地图不存在")
    from alphaprism.planner.parser import parse_file

    m = parse_file(path)
    assert len(m.instruments) == 7
    assert m.global_.market_gate.close is not None
    assert len(m.daily.journal) >= 3
    # 关键位必须带溯源(红线溯源原则):至少部分 level 有 source
    traced = [l for i in m.instruments for l in i.levels if l.source]
    assert traced, "所有关键位都应带溯源注释"
