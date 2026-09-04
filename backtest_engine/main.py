# -*- coding: utf-8 -*-
"""引擎对外 API:输入标的+策略配置+区间 → 交易记录 + 资金曲线 + 风险指标。

用法:
  from backtest_engine.main import run_backtest
  result = run_backtest(symbol="159516", strategy="backtest_engine/configs/strat_breakout_stop.yaml",
                        start="2022-01-01", end="2024-12-31")

输出:
  result["trades"]       列表[dict](日期/方向/价格/股数/金额/手续费/现金/理由/审计哈希)
  result["equity"]       每日净值曲线
  result["metrics"]      总收益率/最大回撤/胜率/盈利因子/交易数
"""
from __future__ import annotations

import json
from pathlib import Path

from .core.auditor import bar_fingerprint, trade_fingerprint
from .core.broker_sim import BrokerConfig, BrokerSim
from .core.data_loader import load_daily_csv, slice_window
from .core.strategy_runner import evaluate_signals, load_strategy

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "kline_cache"
ENGINE_YAML = Path(__file__).resolve().parent / "configs" / "engine.yaml"


def _engine_defaults() -> dict:
    """全局配置(手续费/滑点/资金/结算模式)默认来自 engine.yaml。"""
    import yaml
    with open(ENGINE_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return {
        "initial_cash": float(cfg.get("initial_cash", 100000.0)),
        "commission_rate": float(cfg.get("commission_rate", 0.0005)),
        "slippage_rate": float(cfg.get("slippage_rate", 0.0002)),
        "settlement_mode": str(cfg.get("settlement_mode", "T1")),
        "matching": str(cfg.get("matching", "next_open")),
        "lot_size": int(cfg.get("lot_size", 100)),
    }


def run_backtest(symbol: str, strategy: str,
                 start: str = "", end: str = "",
                 engine_cfg: dict | None = None) -> dict:
    """收盘撮合、无未来函数、全输入快照审计。

    默认参数(资金/佣金/滑点/结算模式)读 engine.yaml;engine_cfg 可逐项覆盖
    (如 {'settlement_mode': 'T0', 'slippage_rate': 0.0})。

    窗口边界铁律:信号回看 N 根(如 20 日前峰)必须对**全量数据**计算(含 start
    之前的 bar),交易/净值只发生在 start~end 切片——否则切片头部窗口吞信号
    (2026-09 Layer3 实测:518850 1 月全部信号被 iloc 负索引静默吞掉,JQ 端正常)。
    """
    cfg = load_strategy(strategy)
    defaults = _engine_defaults()
    if engine_cfg:
        defaults.update({k: v for k, v in engine_cfg.items() if v is not None})
    csv_path = CACHE_DIR / f"{symbol}_daily.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"行情缓存缺失: {csv_path}(先跑 scripts/replay_rules.py 拉取)")
    full = load_daily_csv(csv_path).reset_index(drop=True)
    df = slice_window(full, start, end).reset_index(drop=True)
    full_pos = {str(d): i for i, d in enumerate(full["trade_date"])}

    broker = BrokerSim(BrokerConfig(
        initial_cash=defaults["initial_cash"],
        commission_rate=defaults["commission_rate"],
        slippage_rate=defaults["slippage_rate"],
        settlement_mode=defaults["settlement_mode"]))
    matching = str(defaults.get("matching", "next_open"))
    lb = int(cfg["signals"]["entry"].get("lookback", 20))
    pending: list[dict] = []          # next_open 撮合:bar N 收盘信号 → N+1 开盘成交
    # 预热窗口(聚宽日频语义):start 前一交易日的信号一并生效(其"当根开盘成交"相当于
    # 引擎的 12-31 信号→01-04 开盘)。默认开,与聚宽对齐;close 撮合或研究用可关。
    warmup = bool(engine_cfg.get("warmup_signals", True)) if engine_cfg else True
    if warmup and start:
        pre_dates = [d for d in full["trade_date"] if d < start]
        pre = pre_dates[-1] if pre_dates else None
        if pre is not None and full_pos.get(pre) is not None and full_pos[pre] >= lb:
            pf = full_pos[pre]
            pre_peak = float(full["high"].iloc[pf - lb:pf].max())
            pre_row = full.iloc[pf]
            pre_sig = evaluate_signals(cfg, pre_row, 0.0, pre_peak)
            if pre_sig.entry:
                pending.append({"side": "BUY", "reason": "预热(窗前信号,对齐聚宽日频)",
                                "bar": bar_fingerprint(pre, pre_row)})
    for i, row in df.iterrows():
        date = str(row["trade_date"])
        close, open_ = float(row["close"]), float(row["open"])
        bar_hash = bar_fingerprint(date, row)
        # 1) 先清算上一根收盘产生的待执行信号(以本根开盘成交 = 聚宽日频默认)
        for pend in pending:
            if pend["side"] == "BUY" and not broker.in_position:
                broker.buy_all(date, open_, pend["reason"],
                               trade_fingerprint(date, "BUY", open_, 0,
                                                 pend["reason"], pend["bar"]))
            elif pend["side"] == "SELL" and broker.in_position:
                broker.sell_all(date, open_, pend["reason"],
                                trade_fingerprint(date, "SELL", open_, broker.shares,
                                                  pend["reason"], pend["bar"]))
        pending.clear()
        # 2) 无未来函数 + 窗口边界:前峰在全量 full 上取(含 start 之前的历史)
        i_full = full_pos.get(date)
        prev_peak = None
        if i_full is not None and i_full >= lb:
            prev_peak = float(full["high"].iloc[i_full - lb:i_full].max())
        sig = evaluate_signals(cfg, row, broker.entry_price, prev_peak)
        if matching == "close":
            # 当根收盘成交(引擎原始口径)
            if not broker.in_position and sig.entry:
                broker.buy_all(date, close, sig.reason,
                               trade_fingerprint(date, "BUY", close, 0, sig.reason, bar_hash))
            elif broker.in_position and sig.stop:
                broker.sell_all(date, close, sig.reason,
                                trade_fingerprint(date, "SELL", close, broker.shares,
                                                  sig.reason, bar_hash))
        else:   # next_open:收盘判定 → 下一根开盘成交(与聚宽日频默认一致)
            if not broker.in_position and sig.entry:
                pending.append({"side": "BUY", "reason": sig.reason, "bar": bar_hash})
            elif broker.in_position and sig.stop:
                pending.append({"side": "SELL", "reason": sig.reason, "bar": bar_hash})
        broker.mark_equity(date, close)
    trades = [t.__dict__ for t in broker.trades]
    metrics = _metrics(trades, broker.equity)
    return {"symbol": symbol, "strategy": cfg.get("name", ""),
            "start": start, "end": end, "trades": trades,
            "equity": broker.equity, "metrics": metrics}


def _metrics(trades: list[dict], equity: list[dict]) -> dict:
    """风险指标:资金曲线直接计算(总收益/最大回撤)+ 交易统计(胜率/盈利因子)。"""
    eq = [float(e["equity"]) for e in equity]
    total_ret = round((eq[-1] / eq[0] - 1) * 100, 2) if len(eq) >= 2 and eq[0] else None
    peak = eq[0] if eq else 1.0
    maxdd = 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak * 100)
    profit_wins, profit_loss = [], []
    for i in range(0, len(trades) - 1, 2):
        if trades[i]["side"] == "BUY" and trades[i + 1]["side"] == "SELL":
            pnl = (trades[i + 1]["amount"] - trades[i + 1]["fee"]
                   - trades[i]["amount"] - trades[i]["fee"])
            (profit_wins if pnl > 0 else profit_loss).append(pnl)
    n_closed = len(profit_wins) + len(profit_loss)
    return {
        "final_equity": round(eq[-1], 2) if eq else None,
        "total_return_pct": total_ret,
        "max_drawdown_pct": round(maxdd, 2),
        "win_rate_pct": round(len(profit_wins) / n_closed * 100, 1) if n_closed else None,
        "profit_factor": round(sum(profit_wins) / abs(sum(profit_loss)), 2)
        if profit_loss and abs(sum(profit_loss)) > 0 else None,
        "trade_count": len(trades) // 2,
        "audit": {"bar_hashed": True, "engine": "backtest_engine v0.1.0"},
    }


def dump_result(result: dict, out_dir: str = "data/replays/engine") -> Path:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    base = p / f"{result['symbol']}_{result['strategy']}_{result['start'] or 'x'}_{result.get('end') or 'x'}"
    trades_csv = base.with_suffix(".trades.csv")
    with open(trades_csv, "w", encoding="utf-8", newline="") as f:
        import csv
        w = csv.DictWriter(f, fieldnames=["date", "side", "price", "shares", "amount",
                                          "fee", "cash_after", "reason", "audit_hash"])
        w.writeheader()
        w.writerows(result["trades"])
    metrics_json = base.with_suffix(".metrics.json")
    metrics_json.write_text(json.dumps(result["metrics"], ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return p


# ================================================================ 分层策略执行(v2)
# 入口:run_backtest_layered(symbol, strat_alpha_v1.yaml, ...)
# 语义:信号 bar 收盘判定(YAML 顺序,首触发即停)→ 次 bar 开盘成交(next_open,与 Layer3 同)。

def _series_table(full) -> "pd.DataFrame":
    import pandas as pd
    from .core import indicator_calc as ic
    hi, lo, cl, vo = full["high"], full["low"], full["close"], full["volume"]
    k, d = ic.stoch_kd(hi, lo, cl)
    pc1 = cl.shift(1)
    return pd.DataFrame({
        "m_5": ic.sma(cl, 5),
        "m_10": ic.sma(cl, 10),
        "m_20": ic.sma(cl, 20),
        "m_40": ic.sma(cl, 40),
        "adx14": ic.adx(hi, lo, cl, 14),
        "kdj_k": k, "kdj_d": d, "kdj_j": 3.0 * k - 2.0 * d,
        "kdj_k_prev": k.shift(1), "kdj_d_prev": d.shift(1),
        "prev20_high": ic.rolling_max(hi, 20),
        "vol20": vo.shift(1).rolling(20).mean(),
        "vol5": vo.shift(1).rolling(5).mean(),
        "low20_close": cl.shift(1).rolling(20, min_periods=5).min(),
        "low250_close": cl.shift(1).rolling(250, min_periods=60).min(),
        "low250": hi_shift_min(lo, 250, 60),      # 阶段低(近250日最低价,不含当日)
        "atr20_pct": ic.atr(hi, lo, cl, 20) / cl * 100.0,   # ATR20 占价%
        "peak120": cl.shift(1).rolling(120, min_periods=30).max(),
        "prev_close": pc1,
        "prev_close2": cl.shift(2),
        "rs20": cl / cl.shift(20) - 1.0,
    })


def hi_shift_min(s, lookback: int, min_periods: int):
    """前 lookback 根(不含当日)的最低价序列(阶段低,low 口径)。"""
    return s.shift(1).rolling(lookback, min_periods=min_periods).min()


def run_backtest_layered(symbol: str, strategy: str,
                         start: str = "", end: str = "",
                         engine_cfg: dict | None = None) -> dict:
    """分层策略执行(文档:策略规则-执行版.md §A/§B)。返回含 trades(含层/动作/条件快照)。"""
    import pandas as pd
    from .core.broker_v2 import BrokerConfig as B2Cfg, LayeredBroker
    from .core.strategy_v2 import apply_action, evaluate_bar, load_layered_strategy

    s_cfg = load_layered_strategy(strategy)
    defaults = _engine_defaults()
    if engine_cfg:
        defaults.update({k: v for k, v in engine_cfg.items() if v is not None})
    acct = s_cfg.get("accounts") or {}
    full = load_daily_csv(CACHE_DIR / f"{symbol}_daily.csv").reset_index(drop=True)
    df = slice_window(full, start, end).reset_index(drop=True)
    full_pos = {str(d): i for i, d in enumerate(full["trade_date"])}
    table = _series_table(full)

    broker = LayeredBroker(B2Cfg(
        initial_cash=float(acct.get("full_cash", 100000.0)),
        commission_rate=defaults["commission_rate"],
        slippage_rate=defaults["slippage_rate"],
        settlement_mode=defaults["settlement_mode"],
        full_allocation=float(acct.get("full_allocation", 1 / 3))))
    caps = {k: float(v) for k, v in (s_cfg.get("layers") or {}).items()
            if isinstance(v, (int, float))
            and k in ("anchor", "trial", "main_cap", "chase_cap", "third_cap")}
    broker.caps = caps

    state: dict = {"halted": False, "break_level": None}
    pending: list[dict] = []
    for i, row in df.iterrows():
        date = str(row["trade_date"])
        bar_hash = bar_fingerprint(date, row)
        # 1) 上一根收盘信号 → 本根开盘成交
        for pend in pending:
            ctx_snap = pend["snap"]
            bar = {"date": date, "open": float(row["open"])}
            act = pend["sig"].get("action") or {}
            # 资金优先级(v2 并存规则①):右侧买入现金不足 → 先清 anchor 腾仓
            if "buy" in act and act["buy"] in ("trial", "main") and broker.layers.anchor > 0:
                target = broker.full_amount * float(act.get("pct_of_full", 0)) / 100.0
                if broker.cash < target - 1e-9:
                    broker.sell_layer(date, float(row["open"]), "clear_anchor",
                                      "right_priority", pend["bar_hash"])
            apply_action(broker, ctx_snap, pend["sig"], bar, pend["bar_hash"])
        pending.clear()
        # 2) 当前 bar 上下文(全量序列对齐,NaN→None)
        i_full = full_pos.get(date)
        rowvals = {}
        if i_full is not None:
            for name in table.columns:
                v = table.iloc[i_full][name]
                rowvals[name] = None if v is None or pd.isna(v) else float(v)
        rowvals["entry_break_level"] = state.get("break_level")
        rowvals["anchor_cost"] = broker.layers.cost("anchor")
        state["anchor_hold"] = (state.get("anchor_hold", 0) + 1) \
            if broker.layers.anchor > 0 else 0
        for kk in list(state):
            if kk.startswith("since_"):
                state[kk] += 1
        ctx = {"date": date, "open": float(row["open"]), "close": float(row["close"]),
               "volume": float(row["volume"]), "series": rowvals, "state": state,
               "layers": broker.layers}
        fired = evaluate_bar(s_cfg, ctx)
        if fired:
            f = fired[0]
            sid = f["id"]
            state[f"since_{sid}"] = 0
            state[f"fired_{sid}"] = True
            if sid == "right_initial":
                state["halted"] = False
                state["break_level"] = rowvals.get("prev20_high")
            elif sid == "anchor_entry":
                # 记录入场时刻的峰值与信号收盘(回撤修复止盈的目标基准,信号日口径)
                state["anchor_peak"] = rowvals.get("peak120")
                state["anchor_entry_close"] = ctx["close"]
            elif f.get("halt_add"):
                state["halted"] = True
            pending.append({"sig": f, "snap": ctx, "bar_hash": bar_hash})
        broker.mark_equity(date, float(row["close"]))

    trades = [t.__dict__ for t in broker.trades]
    return {"symbol": symbol, "strategy": s_cfg.get("name", ""),
            "start": start, "end": end, "trades": trades,
            "equity": broker.equity, "state": dict(state),
            "metrics": _metrics_v2(trades, broker.equity)}


def _metrics_v2(trades: list[dict], equity: list[dict]) -> dict:
    eq = [float(e["equity"]) for e in equity]
    peak, maxdd = (eq[0] if eq else 1.0), 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak * 100)
    closed = [(t, trades[i + 1]) for i, t in enumerate(trades[:-1])]
    profit_wins, profit_loss = [], []
    for i in range(0, len(trades) - 1, 2):
        if trades[i]["side"] == "BUY" and trades[i + 1]["side"] == "SELL":
            pnl = (trades[i + 1]["amount"] - trades[i + 1]["fee"]
                   - trades[i]["amount"] - trades[i]["fee"])
            (profit_wins if pnl > 0 else profit_loss).append(pnl)
    n_closed = len(profit_wins) + len(profit_loss)
    return {
        "final_equity": round(eq[-1], 2) if eq else None,
        "total_return_pct": round((eq[-1] / eq[0] - 1) * 100, 2) if len(eq) >= 2 and eq[0] else None,
        "max_drawdown_pct": round(maxdd, 2),
        "win_rate_pct": round(len(profit_wins) / n_closed * 100, 1) if n_closed else None,
        "profit_factor": round(sum(profit_wins) / abs(sum(profit_loss)), 2)
        if profit_loss and abs(sum(profit_loss)) > 0 else None,
        "trade_count": len(trades) // 2 if len(trades) % 2 == 0 else (len(trades) // 2 + 1),
        "audit": {"bar_hashed": True, "engine": "backtest_engine v2 (layered)"},
    }


# ================================================================ 资金池组合(v2.1)
# 入口:run_portfolio(pool_yaml, strategy_yaml, start, end)
# 机制:成员各自运行分层信号(alpha_v2 语义),资金池共享;右侧优先清 anchor;
#      事件驱动调仓(无评分阶段);产出池权益/利用率/换手/费用 + 逐标的归因。

def run_portfolio(pool_yaml: str, strategy_yaml: str,
                  start: str = "", end: str = "",
                  engine_cfg: dict | None = None) -> dict:
    import yaml as _yaml
    from .core.portfolio_sim import PortfolioSim
    from .core.strategy_v2 import evaluate_bar, load_layered_strategy

    with open(pool_yaml, encoding="utf-8") as f:
        pool_cfg = _yaml.safe_load(f) or {}
    s_cfg = load_layered_strategy(strategy_yaml)
    defaults = _engine_defaults()
    if engine_cfg:
        defaults.update({k: v for k, v in engine_cfg.items() if v is not None})
    acct = s_cfg.get("accounts") or {}
    full_cash = float(acct.get("full_cash", 100000.0))
    full_alloc = float(acct.get("full_allocation", 1 / 3))

    members = [(m["symbol"], m.get("since", "")) for m in (pool_cfg.get("members") or [])]
    sim = PortfolioSim(full_cash, full_alloc, [m[0] for m in members])

    datas = {}
    tables = {}
    full_pos = {}
    for sym, since in members:
        full = load_daily_csv(CACHE_DIR / f"{sym}_daily.csv").reset_index(drop=True)
        eff_start = max(start or "", since or "", str(full["trade_date"].iloc[0])[:10])
        df = slice_window(full, eff_start or "2021-01-01", end or "").reset_index(drop=True)
        datas[sym] = (df, {str(d): i for i, d in enumerate(full["trade_date"])},
                      _series_table(full))
    dates = sorted({str(d) for sym, (df, _, _) in datas.items() for d in df["trade_date"]})
    # 尾日对齐铁律:只模拟"所有成员都有数据"的日期(成员缓存末日不一 → 尾部单标的
    # 独占日期会把它标的持仓按 0 计价,造成权益单日假崩,2026-09-03 实测 -57k)
    last_common = min(str(df2["trade_date"].iloc[-1])[:10] for df2, _, _ in datas.values())
    dates = [d for d in dates if d <= last_common]
    caps = {k: float(v) for k, v in (s_cfg.get("layers") or {}).items()
            if isinstance(v, (int, float))
            and k in ("anchor", "trial", "main_cap", "chase_cap", "third_cap")}

    for date in dates:
        for sym, (df, fpos, table) in datas.items():
            rows = df[df["trade_date"].astype(str) == date]
            if rows.empty:
                continue
            row = rows.iloc[0]
            L = sim.layers[sym]
            st = sim.state[sym]
            # 1) 执行上一日收盘信号(本日开盘成交)
            for pend in list(sim.pending[sym]):
                act = pend["sig"].get("action") or {}
                if "buy" in act and act["buy"] in ("trial", "main") and L.anchor > 0:
                    need = sim._full_amount() * float(act.get("pct_of_full", 0)) / 100.0
                    if sim.pool_cash < need - 1e-9:
                        sim.sell(sym, date, float(row["open"]), "clear_anchor",
                                 "right_priority", defaults)
                bar = {"date": date, "open": float(row["open"])}
                pend_exec(pend, bar, sim, sym, caps, defaults, date, float(row["open"]))
            sim.pending[sym].clear()
            # 2) 当前 bar 上下文
            i_full = fpos.get(date)
            rowvals = {}
            if i_full is not None:
                for name in table.columns:
                    v = table.iloc[i_full][name]
                    import pandas as pd
                    rowvals[name] = None if v is None or pd.isna(v) else float(v)
            rowvals["entry_break_level"] = st.get("break_level")
            rowvals["anchor_cost"] = L.cost("anchor")
            st["anchor_hold"] = (st.get("anchor_hold", 0) + 1) if L.anchor > 0 else 0
            for kk in list(st):
                if kk.startswith("since_"):
                    st[kk] += 1
            ctx = {"date": date, "open": float(row["open"]), "close": float(row["close"]),
                   "volume": float(row["volume"]), "series": rowvals, "state": st,
                   "layers": L}
            fired = evaluate_bar(s_cfg, ctx)
            if fired:
                f = fired[0]
                sid = f["id"]
                st[f"since_{sid}"] = 0
                st[f"fired_{sid}"] = True
                if sid == "right_initial":
                    st["halted"] = False
                    st["break_level"] = rowvals.get("prev20_high")
                elif sid == "anchor_entry":
                    st["anchor_peak"] = rowvals.get("peak120")
                    st["anchor_entry_close"] = ctx["close"]
                elif f.get("halt_add"):
                    st["halted"] = True
                sim.pending[sym].append({"sig": f, "snap": ctx,
                                         "hash": bar_fingerprint(date, row)})
        closes = {}
        for s2, (df2, _, _) in datas.items():
            m2 = df2[df2["trade_date"].astype(str) == date]
            if not m2.empty:
                closes[s2] = float(m2.iloc[0]["close"])
        sim.mark_equity(date, closes)

    eq = [e["equity"] for e in sim.equity]
    years = max(len(eq) / 244.0, 0.1)
    peak, maxdd = (eq[0] if eq else 1.0), 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak * 100)
    util = sum(e["invested"] for e in sim.equity) / (len(sim.equity) * full_cash) * 100
    return {
        "pool": pool_cfg.get("name", "pool_v21"), "members": [m[0] for m in members],
        "trades_total": sim.stats.buys + sim.stats.sells,
        "metrics": {"final_equity": round(eq[-1], 2) if eq else None,
                    "total_return_pct": round((eq[-1] / eq[0] - 1) * 100, 2) if len(eq) > 1 else None,
                    "max_drawdown_pct": round(maxdd, 2),
                    "utilization_pct": round(util, 1),
                    "turnover_per_year": round(sim.stats.turnover_amount / full_cash / years, 2),
                    "fees_total": round(sim.stats.fees, 2),
                    "fees_pct_initial": round(sim.stats.fees / full_cash * 100, 2),
                    "fees_pct_pool_ret": None},
        "equity": sim.equity, "stats": sim.stats.__dict__,
        "attr": dict(sim.attr), "years": round(years, 2),
    }


def pend_exec(pend, bar, sim, sym, caps, defaults, date, open_, sizing=None,
              prev_close=None):
    """执行一个 pending 信号(资金池版 apply_action)。

    层默认上限与单标的 broker 一致:shared_cap 显式组上限优先,
    否则回落该层自身上限(如 trial 10%、anchor 9%)——否则重复信号会无限加层。
    sizing(v2.3):pend['score'] 0-100 → ≥full 满配 / ≥half 半配(×0.5) / <no_new 不新建。
    prev_close(个股适配):涨停价上方买入拒单(见 portfolio_sim.buy)。
    """
    from .core.strategy_v2 import StrategyError
    act = pend["sig"]["action"]
    if "buy" in act:
        if sizing:
            score = pend.get("score")
            if score is not None:
                if score < float(sizing.get("no_new", 40.0)):
                    return                 # 不新建
                mult = 1.0 if score >= float(sizing.get("full", 80.0)) else 0.5
            else:
                mult = 1.0
        else:
            mult = 1.0
        layer = act["buy"]
        pct = float(act.get("pct_of_full", 0)) * mult
        cap_pct = caps.get(act.get("shared_cap")) if act.get("shared_cap") else caps.get(layer)
        if cap_pct is not None:
            cap_pct *= mult
        sim.buy(sym, date, open_, layer, pct, pend["sig"]["id"], cap_pct, defaults,
                prev_close=prev_close)
    elif "sell" in act:
        sim.sell(sym, date, open_, act["sell"], pend["sig"]["id"], defaults)
    else:
        raise StrategyError(f"未知动作: {act}")


# ================================================================ 截面评分池(v2.2)
# 入口:run_portfolio_scored(pool_yaml, strat_alpha_v22.yaml, ...)
# 每日收盘:①逐标的信号评估(闸门不变) ②全池双侧评分(因子分位归一) ③Top-3 选仓
#          + 换手带(25% 优势 + 排名带宽 2 位) ④次日开盘执行(轮动清仓语义=全层)。
# 口径[v2.2]:轮动=清该标的所有层;买候选=当日有入场信号的标的,按评分取槽。

def _score_sides(series_map: dict[str, dict], scoring_cfg: dict) -> dict[str, dict]:
    """池内分位评分(双侧)。⚠ 必须传全池:分位在池上计算,单标的传入 n=1 → 恒 0.5
    (2026-09-03 实测 bug:v2.2/v2.3 初版评分因此全部恒 50,排序退化为成员顺序)。"""
    from .core.scoring import pool_scores
    r = pool_scores(series_map, "right", scoring_cfg)
    l = pool_scores(series_map, "left", scoring_cfg)
    return {sym: {"right": r.get(sym, 0.0), "left": l.get(sym, 0.0)}
            for sym in series_map}


def run_portfolio_scored(pool_yaml: str, strategy_yaml: str,
                         start: str = "", end: str = "",
                         engine_cfg: dict | None = None) -> dict:
    import yaml as _yaml
    from .core.portfolio_sim import PortfolioSim
    from .core.strategy_v2 import evaluate_bar, load_layered_strategy

    with open(pool_yaml, encoding="utf-8") as f:
        pool_cfg = _yaml.safe_load(f) or {}
    s_cfg = load_layered_strategy(strategy_yaml)
    defaults = _engine_defaults()
    if engine_cfg:
        defaults.update({k: v for k, v in engine_cfg.items() if v is not None})
    acct = s_cfg.get("accounts") or {}
    full_cash = float(acct.get("full_cash", 100000.0))
    full_alloc = float(acct.get("full_allocation", 1 / 3))
    scoring_cfg = s_cfg.get("scoring") or {}
    sel = s_cfg.get("selection") or {}
    sizing = s_cfg.get("sizing") or None
    top_k = int(sel.get("top_k", 3))

    members = [(m["symbol"], m.get("since", "")) for m in (pool_cfg.get("members") or [])]
    sim = PortfolioSim(full_cash, full_alloc, [m[0] for m in members])

    datas, full_pos, tables = {}, {}, {}
    for sym, since in members:
        full = load_daily_csv(CACHE_DIR / f"{sym}_daily.csv").reset_index(drop=True)
        eff_start = max(start or "", since or "", str(full["trade_date"].iloc[0])[:10])
        df = slice_window(full, eff_start or "2021-01-01", end or "").reset_index(drop=True)
        datas[sym] = df
        full_pos[sym] = {str(d): i for i, d in enumerate(full["trade_date"])}
        tables[sym] = _series_table(full)
    dates = sorted({str(d) for df in datas.values() for d in df["trade_date"]})
    last_common = min(str(df["trade_date"].iloc[-1])[:10] for df in datas.values())
    dates = [d for d in dates if d <= last_common]
    caps = {k: float(v) for k, v in (s_cfg.get("layers") or {}).items()
            if isinstance(v, (int, float))
            and k in ("anchor", "trial", "main_cap", "chase_cap", "third_cap")}

    buy_next: set[str] = set()      # 次日允许执行的买入标的(截面选中)
    score_hist = {m0: {"right": [], "left": []} for m0, _ in members}   # 评分 3 日均值化
    # 个股适配(2026-09-04):池内含非 ETF 代码 → 启用涨跌停闸门/印花税/停牌估值。
    # ETF(5/1 开头)与个股(6/0/3 开头)按代码前缀区分,ETF 成员零改动。
    stock_syms = {s for s, _ in members if not str(s).startswith(("5", "1"))}
    sim.stocks = stock_syms
    stock_mode = bool(stock_syms)
    last_close: dict[str, float] = {}   # 最近收盘(涨跌停定界 + 停牌估值),仅个股需要

    for date in dates:
        # 1) 执行前一日信号(卖出全执行,跌停顺延的保留到次日;买入仅截面选中者)
        for sym, df in datas.items():
            rows = df[df["trade_date"].astype(str) == date]
            if rows.empty:
                continue                     # 停牌:无 bar → 不执行,估值段兜底
            row = rows.iloc[0]
            L = sim.layers[sym]
            keep: list = []                  # 跌停顺延的卖出保留到次日
            for pend in list(sim.pending[sym]):
                act = pend["sig"].get("action") or {}
                if "sell" in act:
                    outs = sim.sell(sym, date, float(row["open"]), act["sell"],
                                    pend["sig"]["id"], defaults,
                                    prev_close=last_close.get(sym))
                    if outs is None:
                        keep.append(pend)    # 跌停顺延(None) → 持仓不动,明日再试
                        continue
                elif "buy" in act:
                    if sym not in buy_next:
                        continue                   # 未选中 → 不买(信号作废)
                    if act["buy"] in ("trial", "main") and L.anchor > 0:
                        need = sim._full_amount() * float(act.get("pct_of_full", 0)) / 100.0
                        if sim.pool_cash < need - 1e-9:
                            sim.sell(sym, date, float(row["open"]), "clear_anchor",
                                     "right_priority", defaults,
                                     prev_close=last_close.get(sym))
                    pend_exec(pend, {"date": date, "open": float(row["open"])}, sim, sym,
                              caps, defaults, date, float(row["open"]), sizing,
                              prev_close=last_close.get(sym))
            sim.pending[sym] = keep
        buy_next.clear()
        # 2) 收盘:逐标的信号评估 + 上下文收集
        ctx_map, fired_map = {}, {}
        for sym, df in datas.items():
            rows = df[df["trade_date"].astype(str) == date]
            if rows.empty:
                # 停牌标记(个股):状态冻结,评分上下文缺席(不参与选中/换手/低分清仓)
                if sym in stock_syms:
                    st_m = sim.state[sym]
                    st_m["suspended"] = True
                    st_m["suspend_days"] = st_m.get("suspend_days", 0) + 1
                continue
            row = rows.iloc[0]
            if sym in stock_syms:
                last_close[sym] = float(row["close"])
                st_m = sim.state[sym]
                st_m["suspended"] = False
                st_m["suspend_days"] = 0
            L, st = sim.layers[sym], sim.state[sym]
            i_full = full_pos[sym].get(date)
            import pandas as pd
            rowvals = {}
            if i_full is not None:
                for name in tables[sym].columns:
                    v = tables[sym].iloc[i_full][name]
                    rowvals[name] = None if v is None or pd.isna(v) else float(v)
            rowvals["entry_break_level"] = st.get("break_level")
            rowvals["anchor_cost"] = L.cost("anchor")
            rowvals["close"] = float(row["close"])
            rowvals["volume"] = float(row["volume"])
            st["anchor_hold"] = (st.get("anchor_hold", 0) + 1) if L.anchor > 0 else 0
            for kk in list(st):
                if kk.startswith("since_"):
                    st[kk] += 1
            ctx = {"date": date, "open": float(row["open"]), "close": float(row["close"]),
                   "volume": float(row["volume"]), "series": rowvals, "state": st,
                   "layers": L}
            fired = evaluate_bar(s_cfg, ctx)
            if fired:
                f = fired[0]
                sid = f["id"]
                st[f"since_{sid}"] = 0
                st[f"fired_{sid}"] = True
                if sid == "right_initial":
                    st["halted"] = False
                    st["break_level"] = rowvals.get("prev20_high")
                elif sid == "anchor_entry":
                    st["anchor_peak"] = rowvals.get("peak120")
                    st["anchor_entry_close"] = ctx["close"]
                elif f.get("halt_add"):
                    st["halted"] = True
                sim.pending[sym].append({"sig": f, "snap": ctx,
                                         "hash": bar_fingerprint(date, row)})
                fired_map[sym] = f
            ctx_map[sym] = rowvals
        # 3) 截面评分 + 选仓(收盘决策)
        if ctx_map:
            scores = _score_sides(ctx_map, scoring_cfg)
            # 评分 3 日均值化(最终裁决件 B):日频分位波动 → 换手/回撤共同来源;
            # 平滑后分数参与 阈值化仓位/排名/换手带/<40 清仓 全部决策
            smoothed: dict[str, dict] = {}
            for sym in ctx_map:
                for side in ("right", "left"):
                    h = score_hist[sym][side]
                    h.append(scores[sym][side])
                    if len(h) > 3:
                        h.pop(0)
                smoothed[sym] = {side: sum(score_hist[sym][side]) / len(score_hist[sym][side])
                                 for side in ("right", "left")}
            held = {s for s in ctx_map if sim.layers[s].total > 0}
            cand = {s: f for s, f in fired_map.items() if "buy" in (f.get("action") or {})}
            # 候选评分:左侧信号用 left,其余(右侧)用 right;持仓标的分侧取持仓侧(均用 3 日均值)
            def side_score(sym):
                f = cand.get(sym) or {}
                if f.get("action", {}).get("buy") == "anchor":
                    return smoothed[sym]["left"]
                if sim.layers[sym].anchor > 0 and sim.layers[sym].right_total == 0:
                    return smoothed[sym]["left"]
                return smoothed[sym]["right"]
            # v2.3:评分挂到买入 pending(阈值化仓位在 pend_exec 按 score 执行)
            for sym, f in fired_map.items():
                for p in sim.pending[sym]:
                    if "buy" in (p["sig"].get("action") or {}):
                        p["score"] = side_score(sym)
            # v2.3 最终裁决:低分持仓清仓带 2 日滞回(score < clear_below 连续 2 日)
            if sizing:
                clear_below = float(sizing.get("clear_below", 40.0))
                for sym in list(held):
                    st = sim.state[sym]
                    st["low_score_days"] = (st.get("low_score_days", 0) + 1) \
                        if side_score(sym) < clear_below else 0
                    if st["low_score_days"] >= 2 and \
                            not any("sell" in (p["sig"].get("action") or {})
                                    for p in sim.pending[sym]):
                        sim.pending[sym].append({
                            "sig": {"id": "score_clear", "action": {"sell": "clear_all"}},
                            "snap": ctx_map[sym], "hash": ""})
                        held.discard(sym)
            ranked = sorted(ctx_map, key=lambda s: -side_score(s))
            free = max(top_k - len(held), 0)
            picked = [s for s in cand if s in ranked[:top_k]][:free]
            buy_next.update(picked)
            # 换手带:槽满且有候选 → 最强挑战者 vs 最弱持者
            if not free and cand and len(held) >= top_k:
                challengers = [s for s in cand if s not in held]
                if challengers:
                    c = max(challengers, key=lambda s: side_score(s))
                    weakest = min(held, key=lambda s: side_score(s))
                    from .core.scoring import rotation_trigger
                    band = sel.get("band") or {}
                    if rotation_trigger(side_score(weakest), side_score(c),
                                        ranked.index(weakest), ranked.index(c), band):
                        sim.pending[weakest].append({
                            "sig": {"id": "rotation", "action": {"sell": "clear_all"}},
                            "snap": ctx_map[weakest], "hash": ""})
                        buy_next.add(c)
        # 4) 净值(个股停牌:沿最近收盘价估值,而非按 0 计价;从未有过价格 → 0)
        closes = {}
        for s2, df2 in datas.items():
            mm = df2[df2["trade_date"].astype(str) == date]
            if not mm.empty:
                closes[s2] = float(mm.iloc[0]["close"])
            elif s2 in stock_syms and s2 in last_close:
                closes[s2] = last_close[s2]
        sim.mark_equity(date, closes)

    eq = [e["equity"] for e in sim.equity]
    years = max(len(eq) / 244.0, 0.1)
    peak, maxdd = (eq[0] if eq else 1.0), 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak * 100)
    util = sum(e["invested"] for e in sim.equity) / (len(sim.equity) * full_cash) * 100
    turnover_yr = sim.stats.turnover_amount / full_cash / years
    out = {
        "pool": pool_cfg.get("name", "pool_v21"), "members": [m[0] for m in members],
        "trades_total": sim.stats.buys + sim.stats.sells,
        "metrics": {"final_equity": round(eq[-1], 2) if eq else None,
                    "total_return_pct": round((eq[-1] / eq[0] - 1) * 100, 2) if len(eq) > 1 else None,
                    "max_drawdown_pct": round(maxdd, 2),
                    "utilization_pct": round(util, 1),
                    "turnover_per_year": round(turnover_yr, 2),
                    "fees_total": round(sim.stats.fees, 2),
                    "fees_pct_initial": round(sim.stats.fees / full_cash * 100, 2)},
        "equity": sim.equity, "stats": sim.stats.__dict__,
        "attr": dict(sim.attr), "years": round(years, 2),
        "trades": sim.trades,
        "accept": {"maxdd_lt_15": maxdd < 15.0, "turnover_lt_3": turnover_yr < 3.0,
                   "fees_lt_1pct": sim.stats.fees / full_cash * 100 < 1.0},
        "stock_mode": {"enabled": stock_mode, "n_stocks": len(stock_syms),
                       "blocked_buys": sim.blocked_buys,
                       "deferred_sells": sim.deferred_sells},
    }
    return out