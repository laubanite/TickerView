# -*- coding: utf-8 -*-
"""小样本验证 · 通过标准执行器(E1-E4,文档:策略规则-执行版.md §E)。

E1 逐笔条件组件真值断言(自动;人工抽查为独立复核层)
E2 动作覆盖统计(broker 注入式动作单测在 tests/ 侧;此处只统计实际动作)
E3 撮合卫生(已在 LayeredBroker._assert_health 全程断言;此处输出汇总)
E4 资金曲线恒等式 + 单日跳变告警
返回 markdown 报告。
"""
from __future__ import annotations

from collections import Counter


def _num(s: dict, k: str):
    v = s.get(k)
    return v if isinstance(v, (int, float)) else None


def signal_components_ok(trade: dict) -> tuple[bool, str]:
    """按 trade['reason'](=信号 id)核对触发组件真值。返回 (ok, 说明)。"""
    s = trade.get("cond_snapshot") or {}
    close, vol = _num(s, "close"), _num(s, "volume")
    sig = str(trade.get("reason", ""))

    def gt(k, v):  # 序列值严格大于
        x = _num(s, k)
        return x is not None and close is not None and close > x

    def lt(k, v):
        x = _num(s, k)
        return x is not None and close is not None and close < x

    if sig in ("right_initial", "chase_back"):
        ok = (gt("prev20_high", None) and _num(s, "adx14") is not None
              and (sig == "right_initial" or _num(s, "adx14") is not None)
              and vol is not None and _num(s, "vol20") and vol >= 1.5 * _num(s, "vol20"))
        return ok, "close>prev20_high 且量>=1.5×vol20"
    if sig == "trial_invalid":
        return lt("entry_break_level", None), "close<突破位"
    if sig == "time_confirm":
        return gt("entry_break_level", None), "close>=突破位"
    if sig in ("pullback_confirm", "second_pullback"):
        k, d, j, pk, pd_ = (_num(s, "kdj_k"), _num(s, "kdj_d"), _num(s, "kdj_j"),
                            _num(s, "kdj_k_prev"), _num(s, "kdj_d_prev"))
        cond1 = gt("m_20", None)
        cond2 = j is not None and j < 30 and k is not None and d is not None \
            and pk is not None and pd_ is not None and pk <= pd_ and k > d
        return cond1 or cond2, "企稳(站回M20 或 KDJ金叉)"
    if sig == "break_m20":
        return lt("m_20", None) and vol is not None and _num(s, "vol20") \
            and vol >= 1.5 * _num(s, "vol20"), "放量收盘<M20"
    if sig == "cut_level":
        return lt("low20_close", None), "收盘<近20日最低收盘"
    if sig == "stop_level":
        return lt("low250_close", None), "收盘<近250日最低收盘"
    return True, "无组件断言(状态相关,人工抽查覆盖)"


def health_report(symbol: str, result: dict) -> str:
    trades = result["trades"]
    equity = result["equity"]
    lines = [f"# 小样本卫生报告 · {symbol} · {result['strategy']}", ""]

    # E1 组件真值
    bad = [(t["date"], t["reason"], t["side"]) for t in trades
           if not signal_components_ok(t)[0]]
    lines += ["## E1 信号组件断言", "",
              f"- 交易 {len(trades)} 笔;组件断言失败 {len(bad)} 笔 "
              f"({'✅ 100% 通过' if not bad else '❌ FAIL'})"]
    for b in bad[:10]:
        lines.append(f"  - FAIL {b}")
    lines.append("")

    # E2 动作覆盖
    acts = Counter((t.get("action") or "") for t in trades)
    sells = {a: n for a, n in acts.items() if a.startswith("clear_")}
    lines += ["## E2 动作覆盖", "",
              f"- 卖出动作: {sells or '无'}"]
    for must in ("clear_right_main", "clear_all", "clear_trial"):
        if must not in sells:
            lines.append(f"  - ⚠ 软目标未触发:{must}(非失败,延长样本或记录)")
    lines.append("")

    # E3 撮合卫生(broker 已全程断言;此处复核无同日买卖)
    same_day = [(t["date"], t["side"]) for i, t in enumerate(trades[:-1])
                if t["date"] == trades[i + 1]["date"]]
    neg = [t["date"] for t in trades if (t.get("shares") or 0) < 0]
    lines += ["## E3 撮合卫生", "",
              f"- 同日买卖对: {len(same_day)}({'✅' if not same_day else '❌'})",
              f"- 负股数: {len(neg)}({'✅' if not neg else '❌'})",
              ""]

    # E4 资金曲线恒等式 + 跳变告警
    id_bad = []
    jumps = []
    prev_eq = None
    for rec in equity:
        calc = rec["cash"] + (rec["anchor"] + rec["trial"] + rec["main"]) * rec["close"]
        if abs(calc - rec["equity"]) > 0.02:
            id_bad.append(rec["date"])
        if prev_eq is not None and rec["close"] > 0:
            chg = abs(rec["equity"] - prev_eq) / prev_eq * 100
            if chg > 20:
                jumps.append((rec["date"], round(chg, 1)))
        prev_eq = rec["equity"]
    lines += ["## E4 资金曲线", "",
              f"- 恒等式违例: {len(id_bad)}({'✅' if not id_bad else '❌'})",
              f"- 单日跳变>20%: {jumps or '无'}",
              ""]
    return "\n".join(lines)