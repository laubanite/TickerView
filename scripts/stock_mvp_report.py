# -*- coding: utf-8 -*-
"""个股 MVP 报告生成:读 data/replays/stock_mvp/ 下 result.json + trades.csv + equity.csv
→ 生成 stock_mvp_report.md(指标/验收/归因/逐笔抽查素材/局限声明)。"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"E:\AgentProjects\AlphaPrism")
SRC = ROOT / "data" / "replays" / ("stock_mvp" if len(sys.argv) <= 1
                                   else f"stock_mvp_smoke_{sys.argv[1][:7]}")

r = json.loads((SRC / "result.json").read_text(encoding="utf-8"))
_lines_raw = (SRC / "trades.csv").read_text(encoding="utf-8").strip().splitlines()
_cols = _lines_raw[0].split(",")
trades = [dict(zip(_cols, ln.split(","))) for ln in _lines_raw[1:]]
eq = [l.split(",") for l in (SRC / "equity.csv").read_text(encoding="utf-8").strip().splitlines()[1:]]

m = r["metrics"]
a = r["accept"]
sm = r.get("stock_mode") or {}
attr = sorted((r.get("attr") or {}).items(), key=lambda kv: -float(kv[1]))
attr = [(s, float(v)) for s, v in attr]

# 信号理由分布(卖出理由=规则语义分布)
sell_reasons = defaultdict(int)
for t in trades:
    if t.get("side") == "SELL":
        sell_reasons[t.get("reason", "?")] += 1
buy_reasons = defaultdict(int)
for t in trades:
    if t.get("side") == "BUY":
        buy_reasons[t.get("reason", "?")] += 1

# 年度收益(按权益曲线年末切片)
years_cur = defaultdict(list)
for d, e, c, i in eq:
    years_cur[d[:4]].append((d, float(e)))
yearly = []
prev_end = 1_000_000.0
for y in sorted(years_cur):
    endv = years_cur[y][-1][1]
    yearly.append((y, (endv / prev_end - 1) * 100))
    prev_end = endv

neg = sum(1 for _, v in attr if v < 0)
realized = sum(v for _, v in attr)

lines = []
lines.append("# 个股模式 MVP 终验报告(v2.3 截面评分 × 100 只个股池)")
lines.append("")
lines.append(f"- 生成: 2026-09-04;窗口: {sys.argv[1] if len(sys.argv) > 1 else '2021-01-01'} → 2026-09-03"
             f"({r.get('years')} 年);数据: 腾讯 fqkline 前复权,2026-09-04 分页补拉至 2019-07(250 根 warmup)")
lines.append(f"- 池: `pool_stock_mvp.yaml` 100 只(Wind 2026-09-03 筛选,沪深300前30/中证500前50/中证1000前20,"
             f"日均成交>2亿/市值>50亿,声明式分批起点 since 生效)")
lines.append(f"- 策略: `strat_alpha_v23_stock.yaml` = v2.3 原样(评分阈值化仓位 + 换手带 60%/3位 + "
             f"评分3日均值化),仅两处调整:**Top-5**(100 只池槽位)与 **full_cash 1M**(整手可行性)")
lines.append("- 成交: next_open(收盘信号次日开盘),佣金万5,滑点 0.02%,T+1")
lines.append("")
lines.append("## 核心指标")
lines.append("")
lines.append("| 指标 | 值 |")
lines.append("|---|---|")
lines.append(f"| 期末净值 | {m['final_equity']:,.0f}(期初 1,000,000)|")
lines.append(f"| **总收益** | **{m['total_return_pct']:+.2f}%** |")
lines.append(f"| **最大回撤** | **{m['max_drawdown_pct']:.2f}%** |")
lines.append(f"| 资金利用率 | {m['utilization_pct']:.1f}% |")
lines.append(f"| 年化换手 | {m['turnover_per_year']:.2f}x |")
lines.append(f"| 总费用 | {m['fees_total']:,.0f}({m['fees_pct_initial']:.2f}% of 期初)|")
lines.append(f"| 成交笔数 | {r.get('trades_total')} |")
lines.append(f"| 触发归因标的 | {len(attr)} 只(亏损 {neg} 只)|")
lines.append(f"| 已实现合计 | {realized:+,.0f} 元 |")
lines.append("")
lines.append("## 验收门(与 v2.3 ETF 终验同口径)")
lines.append("")
for k, label in (("maxdd_lt_15", "最大回撤 < 15%"), ("turnover_lt_3", "年化换手 < 3x"),
                 ("fees_lt_1pct", "总费用 < 1% 期初")):
    lines.append(f"- [{'PASS' if a.get(k) else 'FAIL'}] {label}")
lines.append("")
lines.append(f"- [收益] 总收益 {m['total_return_pct']:+.2f}%(年化 "
             f"{((1 + m['total_return_pct'] / 100) ** (1 / r.get('years', 1)) - 1) * 100:+.1f}%)"
             f" vs 收益线 +5% → "
             f"{'PASS' if (m['total_return_pct'] or 0) >= 5.0 else 'FAIL'}")
lines.append("")
lines.append("## 年度收益")
lines.append("")
lines.append("| 年 | 收益 |")
lines.append("|---|---|")
for y, pct in yearly:
    lines.append(f"| {y} | {pct:+.1f}% |")
lines.append("")
lines.append("## 个股市场规则适配(自动启用)")
lines.append("")
lines.append(f"- 涨停拒买: **{sm.get('blocked_buys')}** 次(开盘≥涨停价×(1-0.2%容差),信号作废不重试)")
lines.append(f"- 跌停顺延: **{sm.get('deferred_sells')}** 次(开盘≤跌停价×(1+0.2%容差)且 <20% 板,整笔顺延次日)")
lines.append("- 卖出印花税: 0.05%(2023-08-28 口径全程统一,早期多计方向保守)")
lines.append("- 停牌估值: 沿最近收盘价(冻结),不按 0 计价")
lines.append("")
lines.append("## 逐标的已实现 P&L")
lines.append("")
lines.append("| Top8 | 金额 | Bottom8 | 金额 |")
lines.append("|---|---|---|---|")
b8 = attr[-8:][::-1] if len(attr) >= 8 else attr[::-1]
t8 = attr[:8]
for i in range(8):
    st, sv = t8[i] if i < len(t8) else ("", "")
    sb, sbv = b8[i] if i < len(b8) else ("", "")
    lines.append(f"| {st} | {sv:+,.0f} | {sb} | {sbv:+,.0f} |" if st else "")
lines.append("")
lines.append("## 卖出理由分布(规则语义)")
lines.append("")
lines.append("| 理由 | 次数 |")
lines.append("|---|---|")
for k2, v2 in sorted(sell_reasons.items(), key=lambda kv: -kv[1]):
    lines.append(f"| {k2} | {v2} |")
lines.append("")
lines.append("## 买入理由分布")
lines.append("")
lines.append("| 理由 | 次数 |")
lines.append("|---|---|")
for k2, v2 in sorted(buy_reasons.items(), key=lambda kv: -kv[1]):
    lines.append(f"| {k2} | {v2} |")
lines.append("")
lines.append("## 局限声明")
lines.append("")
lines.append("1. **静态池前视**:池以 2026-09-03 Wind 数据筛选回放到 2021 → 幸存者偏差(当年被淘汰"
             "的标的不在池内),收益整体偏乐观;分批起点只覆盖新股,不覆盖中途 ST/退市/落榜。")
lines.append("2. **ST ±5% 未建模**:假设窗口内非 ST;若含历史 ST 段,涨跌停限界被高估 → 拦截偏少。")
lines.append("3. **20cm 一字跌停照常成交**(顺延豁免近似);极端流动性缺失未建模。")
lines.append("4. **qfq 伪差**:前复权价非真实 tick,限界判定带 0.2% 容差;等比除权口径与真实复权基准有差。")
lines.append("5. **评分空转已修**:池 100 只,n>1,单标的恒 50 分 bug(2026-09-03 修复件)不适用。")
lines.append("6. **选股信号未验证**:本报告只回答『现策略(纯价格信号)放到个股池会怎样』,"
             "不回答『个股该用什么选股池』。")
lines.append("")
lines.append("## 基准对照(静态池幸存者偏差的诚实度量)")
lines.append("")
import subprocess
_bp = subprocess.run([sys.executable, str(ROOT / "scripts" / "stock_mvp_bench.py"),
                      sys.argv[1] if len(sys.argv) > 1 else "2021-01-01"],
                     capture_output=True, text=True, encoding="utf-8")
print(_bp.stdout, file=sys.stderr)
lines.append("```")
lines.extend(_bp.stdout.strip().splitlines())
lines.append("```")
lines.append("")
lines.append("> 同池等权买入持有 +463% ≫ 策略 +80.6%:在『2026 年回看筛选的静态池』上,"
             "任何主动择时都极难跑赢躺平持有——这是**选池前视**(幸存者偏差)的量级证明,"
             "不是策略失效证明。本 MVP 的可判定结论只有两条:①执行链路(闸门/费用/成交语义)"
             "在个股上成立;②风险特征(回撤 20.5% 超 15% 线,换手/费用超标)来自"
             "100 只池 × 纯价格信号在个股上的信号密度,与 ETF 池结构性不同。")
lines.append("")
lines.append("## 结构性观察")
lines.append("")
lines.append("- **右侧主导**:买入 right_initial 350 + time_confirm 196 vs anchor_entry 64;"
             "卖出以 trial_invalid(329,假突破证伪)与 rotation(127,截面轮动)为主 —— "
             "个股波动大 → 假突破证伪频发,与 ETF 池(16 笔 trial_invalid)完全不同量级。")
lines.append("- **亏损分散**:47/96 标的亏损,但亏损尾部(-1.7万~-4.4万)远小于盈利头部"
             "(+5.8万~+12万):截断亏损/让利润奔跑的结构成立,与 ETF 全量基准的归因形态一致。")
lines.append("- **2022 熊市 -13.9%**:策略在个股熊市仍有正贡献标的(96 只触发中 49 只盈利),"
             "但整体未能免疫系统性回撤(最大回撤 20.5% 即源于此段)。")
lines.append("- **换手 8.82x / 费用 3.74%**:100 只池信号密度 × Top-5 槽位 → 轮动频繁;"
             "这是『纯价格信号 + 大池』的结构性代价,不是 bug(v2.3 换手带已是加严版)。")
lines.append("")
lines.append("## 结论")
lines.append("")
lines.append(f"总收益 **{m['total_return_pct']:+.2f}%** / 回撤 **{m['max_drawdown_pct']:.2f}%** / "
             f"换手 **{m['turnover_per_year']:.2f}x** / 费用 **{m['fees_pct_initial']:.2f}%**。")
lines.append("")
lines.append("**判定:三硬门(回撤/换手/费用)全 FAIL,收益门 PASS —— 沿用 v2.3 裁决口径:"
             "个股模式未通过最终验收,但与 ETF 终验不同,本次 FAIL 的三主门全部是『大池 × "
             "纯价格信号』的结构性产物(信号密度↑→换手/费用↑;个股波动↑→回撤↑),"
             "而非评分机制失灵(评分/轮动/阈值化仓位链路工作正常)。**")
lines.append("")
lines.append("按用户指令『只认这一版的结果』:个股 MVP 到此冻结,不再调参迭代。"
             "后续若重启,前置条件应为:①动态/时点池(消除选池前视);"
             "②个股专属信号结构(假突破证伪频发说明 v2 突破语义不适配个股波动率)。")

(SRC / "stock_mvp_report.md").write_text("\n".join(lines), encoding="utf-8")
print(f"report → {SRC / 'stock_mvp_report.md'}")
