# -*- coding: utf-8 -*-
"""个股 MVP 终验(2026-09-04):v2.3 截面评分口径 × pool_stock_mvp(100 只)。

- 2021-01-01 → 2026-09-03 全量,声明式分批起点(since)生效;
- 个股市场规则自动启用(涨跌停闸门/跌停顺延/印花税/停牌估值);
- 输出:data/replays/stock_mvp/ 下 trades.csv + equity.csv + result.json,
  控制台打印核心指标 + 验收门 + 逐标的归因 Top/Bottom + 拦截统计 + 基准对照。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AgentProjects\AlphaPrism")
from backtest_engine.main import run_portfolio_scored

ROOT = Path(r"E:\AgentProjects\AlphaPrism")
OUT = ROOT / "data" / "replays" / ("stock_mvp" if len(sys.argv) <= 1
                                   else f"stock_mvp_smoke_{sys.argv[1][:7]}")
OUT.mkdir(parents=True, exist_ok=True)

r = run_portfolio_scored(
    str(ROOT / "backtest_engine" / "configs" / "pool_stock_mvp.yaml"),
    str(ROOT / "backtest_engine" / "configs" / "strat_alpha_v23_stock.yaml"),
    start=sys.argv[1] if len(sys.argv) > 1 else "2021-01-01",
)
_out = "stock_mvp" if len(sys.argv) <= 1 else f"stock_mvp_smoke_{sys.argv[1][:7]}"

m = r["metrics"]
sm = r.get("stock_mode") or {}

# ---- 落盘 ----
with open(OUT / "trades.csv", "w", encoding="utf-8", newline="") as f:
    import csv
    cols = ["date", "sym", "side", "layer", "price", "shares", "amount", "fee", "reason"]
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(r.get("trades") or [])
with open(OUT / "equity.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "equity", "cash", "invested"])
    w.writerows([[e["date"], e["equity"], e["cash"], e["invested"]] for e in r["equity"]])
(OUT / "result.json").write_text(json.dumps(
    {k: v for k, v in r.items() if k not in ("equity",)}, ensure_ascii=False,
    indent=1, default=str), encoding="utf-8")

# ---- 控制台报告 ----
print("=" * 64)
_win = sys.argv[1] if len(sys.argv) > 1 else "2021-01-01"
print(f"{'个股 MVP 烟雾测试' if len(sys.argv) > 1 else '个股 MVP 终验'} · "
      f"v2.3 截面评分 × pool_stock_mvp(100 只)")
print(f"窗口: {_win} → 2026-09-03   年数: {r['years']}")
print("=" * 64)
print(f"期末净值:      {m['final_equity']:>14,.0f}   (期初 1,000,000)")
print(f"总收益:        {m['total_return_pct']:>13.2f}%")
print(f"最大回撤:      {m['max_drawdown_pct']:>13.2f}%")
print(f"资金利用率:    {m['utilization_pct']:>13.1f}%")
print(f"年化换手:      {m['turnover_per_year']:>13.2f}x")
print(f"总费用:        {m['fees_total']:>14,.0f}  ({m['fees_pct_initial']}% of 期初)")
print(f"成交笔数:      {r['trades_total']:>14}")
print(f"归因标的数:    {len(r['attr']):>14}")
print("-" * 64)
print(f"[个股适配] 启用: {sm.get('enabled')}  标的数: {sm.get('n_stocks')}  "
      f"涨停拒买: {sm.get('blocked_buys')}  跌停顺延: {sm.get('deferred_sells')}")
print("-" * 64)
a = r["accept"]
print(f"验收门: 回撤<15% {'PASS' if a['maxdd_lt_15'] else 'FAIL'} | "
      f"换手<3x {'PASS' if a['turnover_lt_3'] else 'FAIL'} | "
      f"费用<1% {'PASS' if a['fees_lt_1pct'] else 'FAIL'}")
print("-" * 64)
attr = sorted(r["attr"].items(), key=lambda kv: -kv[1])
print("逐标的已实现 P&L Top8:")
for s, v in attr[:8]:
    print(f"  {s:>8}  {v:>+12,.0f}")
print("逐标的已实现 P&L Bottom8:")
for s, v in attr[-8:]:
    print(f"  {s:>8}  {v:>+12,.0f}")
neg = sum(1 for _, v in attr if v < 0)
realized = sum(v for _, v in attr)
print(f"\n已实现合计: {realized:+,.0f} 元  亏损标的: {neg}/{len(attr)}")
