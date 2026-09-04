# -*- coding: utf-8 -*-
"""Layer 2:无未来函数检测(合成 K 线已知场景)。

场景构造:前 20 根 high 全部 = 10.0;第 21 根(索引 20)收盘 10.5 突破 → 当日
必须买入;第 22 根收盘 9.2 ≤ 成本×0.92 → 当日必须卖出。信号日期与预期 100% 一致。
"""
import pandas as pd

from backtest_engine.core.broker_sim import BrokerConfig, BrokerSim
from backtest_engine.core.data_loader import from_records
from backtest_engine.main import run_backtest


def _mk_bars(closes: list[float], highs: list[float] | None = None,
             base: str = "2022-01-03") -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        d = pd.bdate_range(base, periods=len(closes))[i].strftime("%Y-%m-%d")
        h = highs[i] if highs else max(c, 10.0)
        rows.append({"trade_date": d, "open": h, "high": h,
                     "low": min(c, 9.0), "close": c, "volume": 10000,
                     "amount": 10000 * c, "pct_chg": 0.0, "turnover": 1.0})
    return pd.DataFrame(rows)


def _ws_cache(monkeypatch, symbol: str, df: pd.DataFrame) -> None:
    """沙箱内 pytest tmp_path 不可写(用户 Temp 拒绝访问)→ 用工作区 data/.bt_tmp。"""
    from pathlib import Path
    from backtest_engine import main as m
    base = Path(__file__).resolve().parent.parent.parent / "data" / ".bt_tmp"
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{symbol}_daily.csv"
    df.to_csv(p, index=False)
    monkeypatch.setattr(m, "CACHE_DIR", base)


def test_entry_exact_day(monkeypatch):
    from backtest_engine import main as m
    closes = [9.5] * 20 + [10.5, 9.2, 9.0, 9.5]
    df = _mk_bars(closes)
    _ws_cache(monkeypatch, "T0001", df)
    # close 撮合口径(引擎早期口径):信号当日收盘成交
    r = m.run_backtest("T0001", "backtest_engine/configs/strat_breakout_stop.yaml",
                       engine_cfg={"matching": "close"})
    dates = [t["date"] for t in r["trades"]]
    # 前 20 根 high=10.0;索引 20 收盘 10.5 > 10.0 → 当日买入;次日 9.2 ≤ 10.5*0.92 → 卖出
    assert dates[0] == str(df["trade_date"].iloc[20]), dates
    assert r["trades"][0]["side"] == "BUY"
    assert r["trades"][0]["reason"].startswith("突破近20日高点")
    assert dates[1] == str(df["trade_date"].iloc[21])
    assert r["trades"][1]["side"] == "SELL"
    assert len(r["trades"]) == 2   # 无提前/延迟触发


def test_next_open_fills_next_bar(monkeypatch):
    """next_open 撮合(聚宽日频默认,Layer3 对齐):信号 bar N 收盘判定 → N+1 开盘成交。"""
    from backtest_engine import main as m
    closes = [9.5] * 20 + [10.5, 9.2, 9.0, 9.5]
    df = _mk_bars(closes)
    _ws_cache(monkeypatch, "T0004", df)
    r = m.run_backtest("T0004", "backtest_engine/configs/strat_breakout_stop.yaml",
                       engine_cfg={"matching": "next_open", "slippage_rate": 0.0})
    dates = [t["date"] for t in r["trades"]]
    assert dates[0] == str(df["trade_date"].iloc[21])          # 信号索引20 → 成交索引21
    assert abs(r["trades"][0]["price"] - float(df["open"].iloc[21])) < 1e-6
    assert dates[1] == str(df["trade_date"].iloc[22])          # 止损信号21 → 成交22(开盘)
    assert abs(r["trades"][1]["price"] - float(df["open"].iloc[22])) < 1e-6


def test_no_early_entry_before_window(monkeypatch):
    """窗口未满 20 根前不得触发任何信号。"""
    from backtest_engine import main as m
    closes = [9.5] * 15 + [15.0, 9.0]
    df = _mk_bars(closes, highs=[11.0] * 17)
    _ws_cache(monkeypatch, "T0002", df)
    r = m.run_backtest("T0002", "backtest_engine/configs/strat_breakout_stop.yaml")
    assert r["trades"] == []   # 索引 15 前无前峰;索引 15 收盘 15>11 但前峰在第 19 根后才有效


def test_window_edge_uses_full_history(monkeypatch):
    """窗口边界铁律:信号回看 N 根必须用 start 之前的全量历史(2026-09 实测 bug:
    切片头部 i-lb<0 → iloc 负索引吞信号 → 518850 1 月全部信号丢失)。"""
    from backtest_engine import main as m
    closes = [9.5] * 30 + [10.5, 9.2]
    highs = [10.0] * 30 + [10.5] * 2
    df = _mk_bars(closes, highs=highs)
    _ws_cache(monkeypatch, "T0005", df)
    # 交易窗口从索引 25 开始(信号在索引 30,距离窗口头仅 5 根 < lookback 20)
    start_date = str(df["trade_date"].iloc[25])
    r = m.run_backtest("T0005", "backtest_engine/configs/strat_breakout_stop.yaml",
                       start=start_date, end="",
                       engine_cfg={"matching": "next_open", "slippage_rate": 0.0})
    # 索引 30 的突破信号必须被识别(前峰来自窗口外的 20 根历史) → 31 开盘成交
    assert [(t["date"], t["side"]) for t in r["trades"]] == \
           [(str(df["trade_date"].iloc[31]), "BUY")], r["trades"]


def test_stop_price_uses_entry_cost(monkeypatch):
    """止损基准 = 含费成本价(avg_cost 口径)。"""
    from backtest_engine import main as m
    closes = [10.0] * 20 + [11.0, 10.3, 9.9, 9.5]
    df = _mk_bars(closes, highs=[10.0] * 20 + [11.0] * 4)
    _ws_cache(monkeypatch, "T0003", df)
    r = m.run_backtest("T0003", "backtest_engine/configs/strat_breakout_stop.yaml",
                       engine_cfg={"matching": "close"})
    # 成本 ≈ 11.0*(1+0.0005);10.3 高于 0.92×成本(不触发);9.9 ≤ 0.92×成本 → 触发
    assert [t["side"] for t in r["trades"]] == ["BUY", "SELL"]
    assert r["trades"][1]["date"] == str(df["trade_date"].iloc[22])


def test_broker_cash_math_and_t1():
    cfg = BrokerConfig(initial_cash=10000.0, commission_rate=0.0005,
                       slippage_rate=0.0002, settlement_mode="T1")
    b = BrokerSim(cfg)
    b.buy_all("2022-01-01", 10.0, "buy")
    assert b.in_position and b.shares > 0
    # 滑点生效:成交价抬价 0.02%
    assert abs(b.trades[0].price - 10.002) < 1e-6
    cash_after_buy = b.cash
    # T+1:当日不可卖
    assert b.sell_all("2022-01-01", 10.5, "sell") is None
    b.sell_all("2022-01-02", 10.5, "sell")
    assert not b.in_position
    # 卖出成交价压价 0.02%:10.5 × 0.9998 = 10.4979
    assert abs(b.trades[1].price - 10.4979) < 1e-6
    # 现金守恒:买时现金 = 初始 - 金额 - 费;卖后 = 买后 + 金额 - 费
    buy_tr = b.trades[0]
    sell_tr = b.trades[1]
    assert sell_tr.cash_after == round(cash_after_buy + sell_tr.amount - sell_tr.fee, 2)


def test_broker_t0_same_day_settlement():
    """T0 品种(跨境/债券/商品 ETF):当日买入可当日卖出。"""
    cfg = BrokerConfig(initial_cash=10000.0, settlement_mode="T0",
                       slippage_rate=0.0)
    b = BrokerSim(cfg)
    b.buy_all("2022-01-01", 10.0, "buy")
    tr = b.sell_all("2022-01-01", 10.5, "sell")
    assert tr is not None and tr.side == "SELL"
    assert not b.in_position