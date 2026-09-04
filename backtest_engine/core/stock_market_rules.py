# -*- coding: utf-8 -*-
"""A 股个股市场规则适配层(股票 MVP 专用;ETF 口径零改动)。

覆盖个股与 ETF 的四类结构性差异:
1. 成交价限界:涨停价上方不追买(买价 ≥ 买限 → 拒单,信号作废不重试);
   跌停价下方卖不出(卖价 ≤ 卖限 → 整笔顺延次日,持仓不变);
2. 成本口径:卖出印花税 0.05%(2023-08-28 降费后口径,全程统一不模拟历史档位
   —— [声明] 影响为多计早期卖出费用,方向保守),买入仅佣金;ETF 免印花税不受影响;
3. 停牌估值:持仓标的当日无 bar → 沿用最近收盘价估值(冻结标记),不按 0 计价;
4. 涨跌停幅度:主板 ±10%;创业板(300/301)与科创板(688/689)±20%;
   [声明] ST ±5% 不建模(MVP 池为 2026-09-03 Wind 市值/流动性筛选,假设窗口内非 ST,
   若含历史 ST 段,其涨跌停限界会被高估 → 拒单/顺延偏少,记入报告局限)。

限界容差:GUARD=0.2% —— qfq 前复权价非真实 tick 价(四舍五入/折算伪差),
以比例限界 × (1∓GUARD) 判界,恰好触限拒/顺延,限内 0.2% 以上可成交。
"""
from __future__ import annotations

GUARD = 0.002          # 限界判定容差带 0.2%
STAMP_DUTY = 0.0005    # 印花税(卖出单边,2023-08-28 起 0.05%)


def limit_pct(code: str) -> float:
    """按代码前缀返回涨跌停幅度(%):创/科 20,其余 10。"""
    c = str(code).strip()
    if c.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def limit_prices(prev_close: float, pct: float) -> tuple[float, float]:
    """返回 (buy_max, sell_min):超出 buy_max 的买入拒单,低于 sell_min 的卖出顺延。

    exchange 规则本应 round(prev_close×(1±pct),2),qfq 伪差用 GUARD 比例容差吸收。
    """
    if not prev_close or prev_close <= 0:
        return 0.0, 0.0                      # 无法定界(如上市首日无前收)→ 不设限
    buy_max = prev_close * (1 + pct / 100.0) * (1 - GUARD)
    sell_min = prev_close * (1 - pct / 100.0) * (1 + GUARD)
    return buy_max, sell_min
