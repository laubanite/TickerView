"""盘中深入分析 · A/B 实验台(2026-08-26):单次生成 vs 候选×N + verifier 打分选优(+refine)。

用法(仓库根目录):
  python scripts/ab_compare.py 515790 --n 3 --samples 2            # 实盘拉数
  python scripts/ab_compare.py 515790 --snapshot "docs\\标的 · 光伏ETF华泰柏瑞(515790).txt" --n 3

输出: reports/ab/<code>_<date>/ 下 a.md(A=现流程)、b.md(B=候选+verifier)、scores.json。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, ".")
from alphaprism.config import Config  # noqa: E402

import alphaprism.planner.intraday as intraday  # noqa: E402
from alphaprism.planner import verifier  # noqa: E402
from alphaprism.planner.intraday_engine import (param_anchors, render_state_md,  # noqa: E402
                                                sanitize_v2, signal_state)


def _live_facts(code: str, cfg):
    facts = intraday._load_facts(code, cfg, intraday.datetime.now())
    state = signal_state(facts)
    anchors = param_anchors(facts)
    snapshot_md = intraday._facts_markdown(facts) + \
        render_state_md(facts, state, anchors, None, None)
    return facts, state, anchors, snapshot_md


def _snapshot_facts(card_md: str):
    """从存档快照 md 解析最小 facts/state/anchors(A/B 离线演示用)。"""
    name = code = None
    m = re.search(r"·\s*(.+?)\((\d{6})\)", card_md.splitlines()[0] if card_md.splitlines() else "")
    if m:
        name, code = m.group(1), m.group(2)
    rs = None
    m = re.search(r"相对强弱[^\n]*?([+-]?\d+\.\d+)pp", card_md)
    if m:
        rs = float(m.group(1))
    state = {"scenario": "趋势", "state_word": "左侧观望", "sub_state": None, "spread_pct": 0}
    for line in card_md.splitlines():
        m = re.search(r"\|\s*场景\s*\|\s*([^|]+)\s*\|", line)
        if m:
            state["scenario"] = m.group(1).strip()
        m = re.search(r"\|\s*状态词\s*\|\s*([^|]+)\s*\|", line)
        if m:
            state["state_word"] = m.group(1).strip()
    price = None
    m = re.search(r"\|\s*分时\s*\|\s*现价\s*\|\s*([\d.]+)", card_md)
    if m:
        price = float(m.group(1))
    anchors = {"anchor_price": price, "ok": True, "no_pullback": False}
    for key, pat in (("breakout_add", r"突破加仓位?\s*([\d.]+)"),
                     ("pullback_add", r"回踩加仓位?\s*([\d.]+)"),
                     ("cut_loss", r"减仓\s*跌破([\d.]+)"),
                     ("stop_loss", r"清仓止损\s*([\d.]+)")):
        m = re.search(pat, card_md)
        if m:
            anchors[key] = float(m.group(1))
    facts = {"etf": {"name": name or "?",
                     "code": f"{code}.SH" if code else "999999.SH",
                     "daily": {}, "m30": {}, "minute": {}},
             "rs_5d": rs}
    return facts, state, anchors


def _analyze_a(facts, state, anchors, snapshot_md, cfg):
    md = intraday.build_tech_analysis(facts["etf"]["code"].split(".")[0], cfg)
    return md


def _analyze_b(facts, state, anchors, snapshot_md, cfg, n=3, samples=2):
    """候选×N(卡先行:结论卡独立生成→正文引用)→ sanitize 过滤 → verifier 盲评选优
    → refine 一轮 → 资金兜底。"""
    from alphaprism.planner.intraday import _analysis_prompt_v2, _ensure_fund_section
    from alphaprism.planner.report_schema import (generate_conclusion_card,
                                                  render_conclusion_card)

    card, card_src = generate_conclusion_card(snapshot_md, anchors, state, cfg)
    print(f"  结论卡来源: {card_src}")
    prompt = _analysis_prompt_v2(facts, snapshot_md, state, anchors, None,
                                 conclusion_card=card)
    temps = [0.25, 0.55, 0.85]
    candidates, issues_hist = [], []
    for i in range(max(1, n)):
        t = temps[i % len(temps)]
        text = intraday._llm_chat(prompt, cfg, max_tokens=2600, temperature=t)
        if not text:
            continue
        cand, degraded, cand_issues = sanitize_v2(text, snapshot_md, state, anchors, None)
        if not degraded and cand.strip():
            candidates.append(cand + "\n\n【结论卡】\n" + render_conclusion_card(card))
            issues_hist.append(cand_issues)
        else:
            print(f"  候选#{i} 被规则链剔除(deg={degraded})")
    if not candidates:
        return None, [], False
    best, scored, refined_used = verifier.select_best_with_refine(
        snapshot_md, candidates, cfg, samples=samples, rule_issues=(issues_hist or None))
    best2, _ = _ensure_fund_section(best, snapshot_md)
    return best2, scored, refined_used


def main() -> int:
    ap = argparse.ArgumentParser(description="盘中深入分析 A/B(A=现流程,B=候选+verifier)")
    ap.add_argument("code", help="标的代码(如 515790)")
    ap.add_argument("--n", type=int, default=3, help="候选数(默认 3)")
    ap.add_argument("--samples", type=int, default=2, help="verifier 采样均值次数(默认 2)")
    ap.add_argument("--snapshot", default=None, help="离线模式:存档快照 md 文件路径")
    ap.add_argument("--out", default="reports/ab", help="输出目录(默认 reports/ab)")
    args = ap.parse_args()

    cfg = Config()
    code = str(args.code).split(".")[0]

    if args.snapshot:
        with open(args.snapshot, encoding="utf-8") as f:
            full = f.read()
        parts = full.split("深入分析", 1)
        card_md = parts[0].strip()
        a_archived = parts[1].strip() if len(parts) > 1 else ""
        facts, state, anchors = _snapshot_facts(card_md)
        snapshot_md = card_md
        date_tag = re.sub(r"[^0-9A-Za-z_-]", "-", str(args.snapshot).split("\\")[-1])[:40]
    else:
        facts, state, anchors, snapshot_md = _live_facts(code, cfg)
        a_archived = None
        date_tag = f"{facts.get('date')}-{code}"

    if args.snapshot:
        a_md = a_archived or "(存档中无深入分析正文)"
        print("[A] 存档原报告(同卡对照)")
    else:
        print(f"[A] 现流程生成中…")
        a_md, _a_deg = intraday.build_tech_analysis(code, cfg)
    print(f"[B] 候选 ×{args.n} + verifier 打分(采样 ×{args.samples})…")
    b_md, scores, refined_used = _analyze_b(facts, state, anchors, snapshot_md, cfg,
                                            n=args.n, samples=args.samples)
    if b_md is None:
        print("B 全部候选被规则剔除或无输出 → 无法对比")
        return 1

    out_dir = os.path.join(args.out, date_tag)
    os.makedirs(out_dir, exist_ok=True)
    for name, md in (("a_现流程.md", a_md), ("b_候选+verifier.md", b_md)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(md)
    with open(os.path.join(out_dir, "scores.json"), "w", encoding="utf-8") as f:
        json.dump({"samples": args.samples, "candidates": scores}, f,
                  ensure_ascii=False, indent=1)
    print(f"\nB 各候选得分:")
    for s in scores:
        print(f"  #{s.get('_candidate')} overall={s.get('overall')} "
              f"weakest={s.get('weakest')} (a={s.get('assertiveness')},p={s.get('priority')},"
              f"f={s.get('fact_align')},e={s.get('executable')},c={s.get('clarity')})")
    if scores:
        best = max(scores, key=lambda s: s.get("overall") or 0)
        print(f"  选中: #{best.get('_candidate')} overall={best.get('overall')} "
              f"feedback={best.get('feedback')}")
        for k in ("assertiveness", "priority", "fact_align", "executable", "clarity"):
            ev = best.get(f"{k}_evidence")
            if ev:
                print(f"    {k} 扣分证据: {ev}")
        print(f"  refine 采纳: {refined_used}")
        # 步骤2:verifier 扣分证据结构化归档(下次结论卡先行注入,成为该标的已知坑)
        if verifier.persist_evidence(code, str(facts.get("date", "")), best):
            print(("  已写入评审缺陷档案 data/evidence(下轮结论卡先行将注入提醒)"))
    print(f"\n输出: {out_dir}(a_现流程.md / b_候选+verifier.md / scores.json)")
    print("请人工对比 A/B 的 硬伤数/注水句/结论可执行性。")
    return 0


if __name__ == "__main__":
    sys.exit(main())