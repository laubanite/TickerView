# -*- coding: utf-8 -*-
"""P0 CLI:NL 策略 → 回测 YAML 的端到端交互面。

    python -m alphaprism.nl2strat.cli "突破20日高点买入,跌破成本8%止损"
选项:--tier free|paid  --no-input(非交互,挂起即返回)  --out <dir>
      --resume <run_id>(跨进程恢复:仅重放挂起点后的理解层续流,修复段恢复 P2)
退出码:0 READY / 2 UNSUPPORTED / 3 NEEDS_ASK(未决) / 4 FAILED|EXHAUSTED
"""
from __future__ import annotations

import argparse
import sys

from .contracts import ExitStatus
from .session import Session, SessionOutcome

_EXIT = {ExitStatus.READY: 0, ExitStatus.UNSUPPORTED: 2,
         ExitStatus.NEEDS_ASK: 3, ExitStatus.FAILED: 4, ExitStatus.EXHAUSTED: 4}


def _default_chat():
    from alphaprism.llm import chat
    return chat


def render(so: SessionOutcome) -> None:
    print(f"\n[run {so.run_id}] 出口={so.exit_status} 原因={so.reason or '-'}")
    out = so.outcome
    if out is not None:
        print(f"  LLM 调用 {out.llm_calls}(分阶段 {out.cost_versions}),"
              f"修复轮 {out.repair_rounds}/步 {out.repair_steps},"
              f"越界拒 {out.scope_denied},语义失败 {out.semantic_fail_count}")
    if so.exit_status == ExitStatus.READY and out is not None:
        print(f"  策略 YAML → {so.yaml_path}")
        m = out.metrics
        print("  回测指标:", {k: m.get(k) for k in
                              ("total_return_pct", "max_drawdown_pct", "trade_count")})
        print("\n" + (out.report or "")[:2000])
    elif so.exit_status == ExitStatus.UNSUPPORTED:
        print("  无法表达的语义(显式拒绝,未生成近似规则):")
        for u in so.unsupported or (out.unsupported if out else []) or []:
            print(f"   - {u.text}:{u.reason};最近似方案:{u.nearest_alternative}")
    elif so.exit_status == ExitStatus.NEEDS_ASK:
        for q in so.questions:
            print(f"  待答:{q.text}")
    elif out is not None and out.errors:
        print("  未决错误:")
        for e in out.errors[:8]:
            print(f"   - {e.code} @ {e.where}: {e.msg[:100]}")
    print(f"  trace → {so.trace_path}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nl2strat", description="自然语言 → 回测策略")
    ap.add_argument("text", nargs="*", help="策略口语描述")
    ap.add_argument("--tier", default="free", choices=["free", "paid"])
    ap.add_argument("--no-input", action="store_true", help="非交互(挂起即退出)")
    ap.add_argument("--out", default="", help="YAML 输出目录(默认 data 根)")
    ap.add_argument("--resume", default="", help="run_id(跨进程恢复,P2 提供)")
    args = ap.parse_args(argv)
    if args.resume:
        print("跨进程恢复在 P2(API 化)提供;当前请同一会话内交互续跑。", file=sys.stderr)
        return 3
    text = " ".join(args.text).strip()
    if not text:
        try:
            text = input("策略描述> ").strip()
        except EOFError:
            print("需要策略描述", file=sys.stderr)
            return 4
    sess = Session(chat=_default_chat(), tier=args.tier,
                   interactive=not args.no_input,
                   out_dir=args.out or None)
    so = sess.run(text)
    render(so)
    return _EXIT.get(so.exit_status, 4)


if __name__ == "__main__":
    sys.exit(main())
