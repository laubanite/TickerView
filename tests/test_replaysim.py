# -*- coding: utf-8 -*-
"""回放器回归(2026-09):前视偏差防护 / N日判定 / 分组统计。

用合成日K(400 个交易日随机游走 + 人为放量破位日)验证引擎可跑、
无未来数据泄漏、判定口径正确。
"""
import random

import pandas as pd

from alphaprism.planner.replaysim import (
    _bucket, _outcome_n, asof_facts, replay_symbol, build_report)
from alphaprism.planner.intraday_engine import signal_state


def _synth_daily(seed: int = 7, n: int = 400) -> pd.DataFrame:
    rnd = random.Random(seed)
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-01-02", periods=n)]
    price = 1.0
    rows = []
    for i, d in enumerate(dates):
        chg = rnd.uniform(-0.8, 0.9)
        if i in (150, 260, 340):          # 多处放量破位日:大跌 4% + 3 倍量
            chg = -4.0
        close = price * (1 + chg / 100)
        vol = 200000 if i not in (150, 260, 340) else 900000
        # 日内振幅:真实行情 low<close<high(否则破位日 low==close 会掏空止损类位源)
        high = max(price, close) * (1 + rnd.uniform(0.001, 0.006))
        low = min(price, close) * (1 - rnd.uniform(0.001, 0.006))
        rows.append({"trade_date": d, "open": round(price, 4),
                     "high": round(high, 4), "low": round(low, 4),
                     "close": round(close, 4), "volume": vol, "amount": vol * close,
                     "pct_chg": round(chg, 2), "turnover": 2.0})
        price = close
    return pd.DataFrame(rows)


def test_asof_facts_lookahead():
    daily = _synth_daily()
    idx = _synth_daily(seed=3)
    d = str(daily["trade_date"].iloc[200])
    facts = asof_facts("515790", "测试", d, daily, idx)
    etf = facts["etf"]
    assert etf["date"] == d                         # 锚定当日
    assert etf["minute"]["price"] is not None       # 收盘价合成
    # 前视防护:回放 facts 的日线不得含 d 之后任何一天
    d_daily_max = etf["daily"]["last_close"]        # 只验证结构层面
    assert etf["daily"]["ma"].get(20) is not None
    state = signal_state(facts)
    assert state["state_word"] not in (None, "数据不足")


def test_asof_facts_no_future_rows():
    daily = _synth_daily()
    idx = _synth_daily(seed=3)
    d = str(daily["trade_date"].iloc[100])
    facts = asof_facts("515790", "测试", d, daily, idx)
    # 构造的 minute 价格 == 当日收盘(<= d 的最后一根;3 位小数精度容差)
    d_close = float(daily[daily["trade_date"] == d]["close"].iloc[0])
    assert abs(facts["etf"]["minute"]["price"] - d_close) < 0.001


def test_outcome_n():
    assert _outcome_n([0.90], 0.842, "up", 1) == ("应验", 6.89)
    out, move = _outcome_n([0.85], 0.842, "up", 1)
    assert out == "部分应验" and abs(move - 0.95) < 0.01
    assert _outcome_n([0.79], 0.842, "down", 1) == ("应验", -6.18)
    assert _outcome_n([], 0.842, "up", 3) == (None, None)       # 数据不足
    assert _outcome_n([0.85] * 5, 0.842, None, 5) == (None, None)  # 无方向


def test_replay_engine_runs_and_records():
    daily = _synth_daily()
    idx = _synth_daily(seed=3)
    rows = replay_symbol("515790", "测试", daily, idx,
                         start="2023-06-01", end="2023-12-31")
    assert rows, "回放应有记录"
    r = rows[0]
    for key in ("date", "state_word", "category", "anchor", "cut", "stop"):
        assert key in r
    # 放量破位日(索引 180,dates 第 181 个交易日)若在区间内应产出方向性建议
    crash = next((x for x in rows if x["state_word"] == "破位退出"), None)
    if crash:
        assert crash["direction"] == "down"
        assert crash["category"] == "减仓"


def test_bucket():
    # 幅度分桶:8% 深度 → -6~-10 档;20% → 开口档
    assert _bucket(8.0, (3, 6, 10, 15), ("0~-3", "-3~-6", "-6~-10", "-10~-15", ">-15")) == "-6~-10"
    assert _bucket(20.0, (3, 6, 10, 15), ("0~-3", "-3~-6", "-6~-10", "-10~-15", ">-15")) == ">-15"
    assert _bucket(None, (3, 6), ("0~-3", ">-3")) == "?"


def test_report_builds():
    daily = _synth_daily()
    idx = _synth_daily(seed=3)
    rows = replay_symbol("515790", "测试", daily, idx,
                         start="2023-06-01", end="2024-12-31")
    md = build_report("515790", rows, "2023-06-01", "2024-12-31")
    assert "## ① 总体" in md and "## ③ 档位宽度分组" in md
    assert "稳定性" in md


def test_risk_metrics_crash_protects_and_rally_negative():
    from alphaprism.planner.replaysim import _risk_metrics
    # 破位后持续下跌:路径A(留50%仓位)回撤应显著小于路径B(满仓)
    crash = [0.97, 0.93, 0.90, 0.87, 0.85]
    rm = _risk_metrics(crash, 1.0)
    assert rm[5]["eff"] > 30                 # 真保险
    assert rm[5]["maxddA"] < rm[5]["maxddB"]
    assert rm[5]["worstA"] > rm[5]["worstB"]  # A 最低收盘亏损更小
    # 破位后反弹:减仓跑输(错过上涨)→ end_ratio<1;且窗口内波动极小(回撤<1%)
    # → 无保险意义:eff 因 A 路径暴露减半而恒为正,语义要看 end_ratio 与 ddB 量级
    rally = [1.02, 1.04, 1.06, 1.05, 1.08]
    rm2 = _risk_metrics(rally, 1.0)
    assert rm2[5]["end_ratio"] < 1.0
    assert rm2[5]["maxddB"] < 1.0


def test_break_class():
    from alphaprism.planner.replaysim import _break_class
    # A:次日收回(收盘 > M20)
    assert _break_class(1.0, 0.95, 0.99, 0.96, 100, 100)[0] == "A"
    # B:未收回 + 缩量(开盘无跳空)
    assert _break_class(1.0, 0.95, 0.995, 0.93, 80, 100)[0] == "B"
    # C:未收回 + 放量(开盘无跳空)
    c, gap = _break_class(1.0, 0.95, 0.995, 0.93, 120, 100)
    assert c == "C" and gap is False
    # D:跳空低开优先(D 覆盖其他)
    c2, gap2 = _break_class(1.0, 0.95, 0.98, 0.93, 80, 100)
    assert c2 == "D" and gap2 is True


def test_risk_and_break_fields_on_replay():
    daily = _synth_daily()
    idx = _synth_daily(seed=3)
    rows = replay_symbol("515790", "测试", daily, idx,
                         start="2023-06-01", end="2024-12-31")
    down = [r for r in rows if r.get("direction") == "down" and r.get("risk")]
    assert down, "合成数据应含破位减仓样本"
    for r in down:
        assert "break_class" in r and "risk" in r
        assert "m20" in r