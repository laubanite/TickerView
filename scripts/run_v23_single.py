# -*- coding: utf-8 -*-
"""v2.3 信号 × 单标的分层口径补跑(2026-09-04,诊断性对照,零调参)。

目的:补齐对比矩阵缺失的一行——
    C5'原版(单标的) +1.94% / v1修订(单标的) +0.76% / v2.3信号(单标的)=本行 / v2.3(评分池) +2.72%
把"信号差异"与"评分池架构差异"分解开。

口径(与 c5_validation 报告头严格对齐):
- 同 10 只 ETF 池(pool_v21 成员),每只独立 10 万全现金,分层单标的入口 run_backtest_layered;
- start=2021-01-01 统一切片(序列在全量历史上算,warmup 不受影响);end=缓存末日;
- engine.yaml 默认:next_open / 佣金万5 / 滑点0.02%(与 C5' 报告一致);
- 策略=configs/strat_alpha_v23.yaml 原样:run_backtest_layered 只消费
  accounts/layers/signals/series/indicators,scoring/sizing/selection 段天然不生效
  → 即"v2.3 信号、无截面评分、无 Top-3",零改动零调参。
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"E:\AgentProjects\AlphaPrism")
from backtest_engine.main import run_backtest_layered

ROOT = Path(r"E:\AgentProjects\AlphaPrism")
OUT = ROOT / "data" / "replays" / "v23_single"
OUT.mkdir(parents=True, exist_ok=True)
STRAT = str(ROOT / "backtest_engine" / "configs" / "strat_alpha_v23.yaml")

SYMS = ["510300", "510500", "159915", "512480", "512400",
        "512880", "512010", "512660", "518850", "512890"]

rows, trades_all = [], []
for sym in SYMS:
    r = run_backtest_layered(sym, STRAT, start="2021-01-01")
    m = r["metrics"]
    buys = [t for t in r["trades"] if t.get("side") == "BUY"]
    sells = [t for t in r["trades"] if t.get("side") == "SELL"]
    buy_reasons = Counter(t.get("reason", "?") for t in buys)
    sell_reasons = Counter(t.get("reason", "?") for t in sells)
    rows.append({"symbol": sym, **m,
                 "buys": len(buys), "sells": len(sells),
                 "buy_reasons": dict(buy_reasons), "sell_reasons": dict(sell_reasons)})
    trades_all.extend(r["trades"])
    print(f"{sym}  ret={m['total_return_pct']:+6.2f}%  dd={m['max_drawdown_pct']:5.2f}%  "
          f"trades={m['trade_count']:3d}  win={m['win_rate_pct']}", flush=True)

rets = [r["total_return_pct"] for r in rows]
dds = [r["max_drawdown_pct"] for r in rows]
tcs = [r["trade_count"] for r in rows]
wins = [r["win_rate_pct"] for r in rows if r["win_rate_pct"] is not None]
agg = {
    "equal_weight_return_pct": round(sum(rets) / len(rets), 2),
    "n_positive": sum(1 for x in rets if x > 0),
    "dd_range": [min(dds), max(dds)],
    "trades_total": sum(tcs),
    "win_rate_range": [min(wins), max(wins)],
    "buy_reasons_total": dict(sum((Counter(r["buy_reasons"]) for r in rows), Counter())),
    "sell_reasons_total": dict(sum((Counter(r["sell_reasons"]) for r in rows), Counter())),
}
years = 5.64
agg["annualized_pct"] = round(((1 + agg["equal_weight_return_pct"] / 100) ** (1 / years) - 1) * 100, 2)

(OUT / "v23_single_results.json").write_text(
    json.dumps({"rows": rows, "aggregate": agg}, ensure_ascii=False, indent=1), encoding="utf-8")

print("=" * 64)
print(f"v2.3 信号 × 单标的分层(无评分无Top-3)· 10 池 · 2021-01 → 缓存末日")
print(f"等权收益: {agg['equal_weight_return_pct']:+.2f}%  (年化 ≈{agg['annualized_pct']}%)"
      f"  正收益 {agg['n_positive']}/10")
print(f"回撤区间: {agg['dd_range'][0]:.2f}% ~ {agg['dd_range'][1]:.2f}%"
      f"  总笔数: {agg['trades_total']}  胜率区间: {agg['win_rate_range'][0]}~{agg['win_rate_range'][1]}%")
print("买入理由:", agg["buy_reasons_total"])
print("卖出理由:", agg["sell_reasons_total"])
