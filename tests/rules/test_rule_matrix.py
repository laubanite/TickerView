# -*- coding: utf-8 -*-
"""合成规则矩阵测试:遍历 gen.MATRIX × 断言不变量全过。

规则漏洞循环(2026-08-29 建立):
  合成形态 → 断言不变量 → 发现违反(红) → 补规则 → 固化新样例 → 再跑
任何形态违反不变量 = 规则漏洞(测试红,报形态名+违反内容)。

运行:python tests/rules/test_rule_matrix.py
     (无 pytest 环境;沙箱网络受限装不了 pytest,本文件自带 runner)
"""
from __future__ import annotations

import os
import sys
import importlib.util

# ---- 加载同目录 gen/invariants(不依赖包结构,pytest/独立运行均可)----
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # 项目根(tests 上一级)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)                            # 项目根(alphaprism 所在)
from alphaprism.planner.intraday_engine import param_anchors, sanitize_v2, signal_state

gen = _load("rule_gen", os.path.join(_HERE, "gen.py"))
inv = _load("rule_invariants", os.path.join(_HERE, "invariants.py"))


def run_matrix(verbose: bool = False) -> list[str]:
    """遍历矩阵,返回全部违反报告(空 = 全过)。"""
    reports = []
    for name, facts, expect in gen.MATRIX:
        state = signal_state(facts)
        anchors = param_anchors(facts)
        if expect.get("ok") is False:
            # 预期档位不完整:必须 ok=False 且带 reason
            if anchors.get("ok"):
                reports.append(f"[{name}] 预期档位不完整(ok=False),实际 ok=True")
            elif not anchors.get("reason"):
                reports.append(f"[{name}] 预期 ok=False 但无 reason")
            continue
        if not anchors.get("ok"):
            # 预期完整档位但实际缺档:可能是数据构造没给够,或规则漏洞
            reports.append(f"[{name}] 预期 ok=True,实际 ok=False "
                           f"(reason={anchors.get('reason')})")
            continue
        issues = inv.check_all(facts, state, anchors)
        if issues:
            reports.append(f"[{name}] {len(issues)} 条违反:\n  " + "\n  ".join(issues))
        elif verbose:
            sc, sw = state.get("scenario"), state.get("state_word")
            print(f"  PASS {name}: {sc}/{sw} "
                  f"stop={anchors.get('stop_loss')}({anchors.get('stop_src')}) "
                  f"ok={anchors.get('ok')}")
    return reports


# ------------------------------------------------------------------ pytest 风格入口

def test_rule_matrix_no_violations():
    reports = run_matrix()
    assert not reports, "\n".join(reports)


def test_matrix_not_empty():
    assert len(gen.MATRIX) >= 15, "合成矩阵过小,规则分支覆盖不足"


def run_sanitize_matrix(verbose: bool = False) -> list[str]:
    """遍历 sanitize 注入用例,返回全部违反报告。"""
    reports = []
    for name, case in gen.S_CASES:
        issues = inv.check_sanitize_case(case, sanitize_v2)
        if issues:
            reports.append(f"[{name}] {len(issues)} 条违反:\n  " + "\n  ".join(issues))
        elif verbose:
            print(f"  PASS {name}")
    return reports


def test_sanitize_matrix_no_violations():
    reports = run_sanitize_matrix()
    assert not reports, "\n".join(reports)


def test_sanitize_matrix_not_empty():
    assert len(gen.S_CASES) >= 20, "sanitize 注入用例过少"
    names = {n for n, _ in gen.S_CASES}
    assert any("S18" in n for n in names), "缺大盘两维矩阵用例(规则20)"


def test_covers_stop_branches():
    """止损 v2.8 四分支(近沿/兜底远/无止损类/空头)必须都覆盖。"""
    names = {n for n, _, _ in gen.MATRIX}
    assert any("E1" in n for n in names), "缺 E1 止损近沿分支"
    assert any("E2" in n for n in names), "缺 E2 止损兜底远分支"
    assert any("E3" in n for n in names), "缺 E3 无止损类分支"
    assert any("E4" in n for n in names), "缺 E4 空头止损分支"


if __name__ == "__main__":
    verbose = "-v" in sys.argv
    reports = run_matrix(verbose=verbose)
    sreports = run_sanitize_matrix(verbose=verbose)
    all_r = reports + sreports
    if all_r:
        print("### 规则漏洞报告(需补洞):")
        for r in all_r:
            print(r)
        print(f"\n档位矩阵 {len(gen.MATRIX)} 形态 / sanitize {len(gen.S_CASES)} 用例,"
              f"共 {len(all_r)} 个违反")
        sys.exit(1)
    print(f"OK: 档位矩阵 {len(gen.MATRIX)} 形态 + sanitize {len(gen.S_CASES)} 用例全部满足")
    sys.exit(0)