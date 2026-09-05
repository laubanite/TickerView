# -*- coding: utf-8 -*-
"""个股合成规则矩阵测试:v3 自适应层的"发现问题-补漏洞-断路器"。

镜像 tests/rules/test_rule_matrix.py 架构:
  合成形态(不同市场体质) → evaluate_stock_risk → 断言不变量 → 违反=漏洞。

核心检验目标(v3 方案 §一 的教训):
  固定阈值 R6/R7 对不同体质(科技股 vs 公用事业)误报 → 自适应层
  (自身分位/通道计数/区间分位)必须对 A1-A5、D1-D2 这类"同指标不同体质"
  的形态给出一致合理的行为。

运行: pytest tests/rules/test_stock_rule_matrix.py -q
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, ROOT)

from alphaprism.planner.stock_risk import (  # noqa: E402
    build_stock_risk_advice, evaluate_stock_risk, render_stock_lines)

import importlib.util


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("stock_gen", os.path.join(_HERE, "stock_gen.py"))
inv = _load("stock_inv", os.path.join(_HERE, "stock_invariants.py"))

# 形态 → 期望档位(显式声明,防"测了但没断言结果"的自欺)
EXPECTED_LEVEL = {
    "A1-健康多头-低价股": "正常", "A2-健康多头-高价股": "正常",
    "A3-健康多头-高波动体质(科技股常态ATR7%)": "正常",
    "A4-健康多头-低波动体质(公用事业ATR1%)": "正常",
    "A5-高ATR但自身常态(旧R7会误报)": "正常",
    "B1-空头排列-底部区": "关注", "B2-空头排列-但位置高(不触发)": "正常",
    "B3-纠缠排列(不触发)": "正常",
    "C1-走弱1次(不触发)": "正常", "C2-走弱2次(关注)": "关注",
    "C3-走弱3次(风险)": "风险", "C4-走弱4次封顶(风险)": "风险",
    "D1-ATR自身90分位(高波动体制)": "关注",
    "D2-同ATR但自身低分位(不触发)": "正常",
    "D3-历史不足无分位(固定阈值兜底)": "关注",
    "E1-跌破20日低(R3风险)": "风险", "E2-跌破250日低(R4严重)": "严重",
    "E3-成本-8%(R5风险)": "风险", "E4-破位M20放量(R2风险)": "风险",
    "F1-触及跌停(R9风险)": "风险", "F2-停牌守卫(陈旧价不作判定)": "正常",
    "F3-无有效行情": "正常",
    "G1-走弱3次+高波动体制(风险)": "风险",
    "G2-破20日低+走弱2(风险,取最高)": "风险",
    "G3-全绿但250日低位纠缠(仅关注)": "关注",
}


def run_matrix(verbose: bool = False) -> list[str]:
    reports = []
    for name, ctx, _expect in gen.MATRIX:
        result = evaluate_stock_risk(ctx)
        if name in EXPECTED_LEVEL and result["level"] != EXPECTED_LEVEL[name]:
            ids = [r["id"] for r in result["rules"]]
            reports.append(f"[{name}] 期望档位 {EXPECTED_LEVEL[name]},"
                           f"实得 {result['level']}(规则:{ids})")
            continue
        issues = inv.check_level_invariants(ctx, result)
        issues += inv.check_hard_rule_levels(ctx, result)
        issues += inv.check_no_codes_frontend(ctx, result)
        issues += inv.check_observation_invariants(ctx)
        issues += inv.check_state_monotonic(ctx, evaluate_stock_risk)
        if issues:
            reports.append(f"[{name}] {len(issues)} 条违反:\n  " + "\n  ".join(issues))
        elif verbose:
            print(f"  PASS {name}: {result['level']} "
                  f"({[r['id'] for r in result['rules']]})")
    return reports


def run_end_to_end(verbose: bool = False) -> list[str]:
    """端到端:mk 基线走完整快照管线(注入 ctx,跳过网络),断言快照卡与深入骨架不变量。"""
    reports = []
    import re
    from alphaprism.planner import stock_risk as sr
    baseline = gen.mk()
    _M30 = {"day": "2026-09-04", "last": 11.0, "m5": 10.9, "m20": 10.8,
            "above_m20": True, "path": "冲高回落", "tail": "走弱",
            "open_gap": 1.2, "fade": -2.2, "recover": 0.8,
            "day_high": 11.3, "day_low": 10.9, "day_open": 11.1}
    for name, ctx, _e in gen.MATRIX:
        full = dict(baseline)
        full.update({k: v for k, v in ctx.items() if k in baseline or v is None})
        full.update({k: v for k, v in ctx.items() if k not in baseline or v is not None})
        result = evaluate_stock_risk(full)
        full["_m30"] = dict(_M30, above_m20=bool(full.get("p250_pos", 0.5) > 0.2),
                            last=full.get("price") or 11.0)
        try:
            sk = sr._skeleton(full)
            markdown = sr._facts_table(full, result, sk)
            deep = sr._deep_skeleton(full, result, full["_m30"])
        except Exception as exc:  # noqa: BLE001
            reports.append(f"[{name}] 卡片构建异常: {type(exc).__name__}: {exc}")
            continue
        issues = inv.check_no_codes_frontend(full, result, markdown)
        for label, blob in (("深入骨架", deep),):
            if re.search(r"\bR\d{1,2}\b", blob):
                issues.append(f"{label} 出现代号")
        for sec in ("多周期矛盾定性", "证据天平", "情景推演映射", "核心观察点"):
            if sec not in deep:
                issues.append(f"{label} 缺小节 {sec}")
        if issues:
            reports.append(f"[{name}] {len(issues)} 条违反:\n  " + "\n  ".join(issues))
        elif verbose:
            print(f"  E2E PASS {name}")
    return reports


def test_stock_matrix_no_violations():
    reports = run_matrix()
    assert not reports, "\n".join(reports)


def test_matrix_covers_adaptive_branches():
    names = {n for n, _, _ in gen.MATRIX}
    for frag in ("A5", "B1", "C2", "C3", "D1", "D2", "D3", "E1", "E2", "F2"):
        assert any(frag in n for n in names), f"矩阵缺 {frag} 分支"
    assert len(gen.MATRIX) >= 20, "个股合成矩阵过小"


def test_end_to_end_snapshot_card():
    reports = run_end_to_end()
    assert not reports, "\n".join(reports)


if __name__ == "__main__":
    verbose = "-v" in sys.argv
    reports = run_matrix(verbose=verbose)
    reports += run_end_to_end(verbose=verbose)
    if reports:
        print("### 个股规则漏洞报告(需补洞):")
        for r in reports:
            print(r)
        sys.exit(1)
    print(f"OK: 个股矩阵 {len(gen.MATRIX)} 形态全部满足(不变量+期望档位+前端无代号)")
