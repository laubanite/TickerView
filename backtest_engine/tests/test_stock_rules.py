# -*- coding: utf-8 -*-
"""个股市场规则适配单测:限界计算/涨停拒买/跌停顺延/印花税/ETF零改动/20cm豁免。"""
from backtest_engine.core.portfolio_sim import PortfolioSim
from backtest_engine.core.stock_market_rules import limit_pct, limit_prices

CFG = {"slippage_rate": 0.0, "commission_rate": 0.0005, "lot_size": 100}


def test_limit_pct_prefixes():
    assert limit_pct("300308") == 20.0
    assert limit_pct("301526") == 20.0
    assert limit_pct("688256") == 20.0
    assert limit_pct("600519") == 10.0
    assert limit_pct("000725") == 10.0
    assert limit_pct("002475") == 10.0


def test_limit_prices_bounds():
    # prev=100,10%:buy_max≈109.8 / sell_min≈90.2(0.2% GUARD 内收)
    buy_max, sell_min = limit_prices(100.0, 10.0)
    assert 109.7 < buy_max < 109.9
    assert 90.1 < sell_min < 90.3
    # 无前收(如上市首日)→ 不设限
    assert limit_prices(0.0, 10.0) == (0.0, 0.0)


def test_buy_blocked_at_limit_up_and_passes_within():
    sim = PortfolioSim(1_000_000.0, 1 / 3, ["600519"])
    sim.stocks = {"600519"}
    # prev=100 → buy_max≈109.8;开盘 110(涨停)→ 拒单,信号作废
    tr = sim.buy("600519", "d1", 110.0, "trial", 10.0, "r", 10.0, CFG, prev_close=100.0)
    assert tr is None and sim.blocked_buys == 1
    # 开盘 105(限内)→ 正常成交,计数不再增加
    tr = sim.buy("600519", "d2", 105.0, "trial", 10.0, "r", 10.0, CFG, prev_close=100.0)
    assert tr is not None and sim.blocked_buys == 1


def test_buy_etf_not_blocked():
    # ETF 成员不在 stocks 集合 → 涨停价上方也不拦(线上 ETF 行为零改动)
    # (prev=9.0 → 9.9 已超 ETF 涨停限界,若误入 stocks 会被拦)
    sim = PortfolioSim(100_000.0, 1 / 3, ["512480"])
    tr = sim.buy("512480", "d1", 9.9, "trial", 10.0, "r", 10.0, CFG, prev_close=9.0)
    assert tr is not None and sim.blocked_buys == 0


def test_sell_deferred_at_limit_down_then_executes():
    sim = PortfolioSim(1_000_000.0, 1 / 3, ["600519"])
    sim.stocks = {"600519"}
    sim.buy("600519", "d1", 100.0, "main", 45.0, "r", 45.0, CFG, prev_close=99.0)
    n = sim.layers["600519"].main
    # prev=100 → sell_min≈90.2;开盘 90(跌停)→ 顺延 None,持仓不动
    outs = sim.sell("600519", "d2", 90.0, "clear_all", "x", CFG, prev_close=100.0)
    assert outs is None and sim.deferred_sells == 1
    assert sim.layers["600519"].main == n
    # 次日开盘 95(限内)→ 执行,持仓清零
    outs = sim.sell("600519", "d3", 95.0, "clear_all", "x", CFG, prev_close=100.0)
    assert outs and sim.layers["600519"].total == 0


def test_20cm_not_deferred():
    # 20cm 标的:20% 一字跌停价开盘 → 照常成交(顺延豁免,近似声明)
    sim = PortfolioSim(1_000_000.0, 1 / 3, ["688256"])
    sim.stocks = {"688256"}
    sim.buy("688256", "d1", 100.0, "main", 45.0, "r", 45.0, CFG, prev_close=99.0)
    outs = sim.sell("688256", "d2", 80.0, "clear_all", "x", CFG, prev_close=100.0)
    assert outs is not None and len(outs) == 1 and sim.deferred_sells == 0


def test_no_prev_close_passes_through():
    # 无前收(数据缺 prev)→ 闸门放行,不误杀
    sim = PortfolioSim(1_000_000.0, 1 / 3, ["600519"])
    sim.stocks = {"600519"}
    tr = sim.buy("600519", "d1", 110.0, "trial", 10.0, "r", 10.0, CFG, prev_close=None)
    assert tr is not None


def test_stamp_duty_sell_only_for_stocks():
    # 个股:买入费=佣金;卖出费=佣金+印花税(0.05%);ETF 卖出费减半
    simS = PortfolioSim(1_000_000.0, 1 / 3, ["600519"])
    simS.stocks = {"600519"}
    simE = PortfolioSim(1_000_000.0, 1 / 3, ["512480"])
    simS.buy("600519", "d1", 10.0, "main", 45.0, "r", 45.0, CFG, prev_close=9.9)
    simE.buy("512480", "d1", 10.0, "main", 45.0, "r", 45.0, CFG)
    sa = simS.sell("600519", "d2", 10.0, "clear_all", "x", CFG, prev_close=9.9)[0]
    se = simE.sell("512480", "d2", 10.0, "clear_all", "x", CFG)[0]
    assert abs(sa["fee"] - 2 * se["fee"]) < 0.03   # 佣金万5+印花税万5 vs 佣金万5
    stamp = round(sa["amount"] * 0.0005, 2)
    assert abs((sa["fee"] - se["fee"]) - stamp) < 0.02
