# -*- coding: utf-8 -*-
"""内部标识后处理过滤器回归(2026-08-29 系统修复#⑦):C8'/§4.2 等不得泄漏。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from alphaprism.planner.intraday import _sanitize_internal_labels


def test_c8_replaced():
    md = "企稳信号需关注C8'三选一。企稳只许 C8' 三选一(缩量十字星)。"
    out = _sanitize_internal_labels(md)
    assert "C8'" not in out and "C8" not in out
    assert "企稳信号三选一" in out


def test_section_replaced():
    md = "依据「盘面状态」段的确定性结论。程序锚点:突破 0.894。"
    out = _sanitize_internal_labels(md)
    assert "盘面状态" not in out
    assert "程序计算结果" in out


def test_42_replaced():
    md = "写'试多'前必须先列 §4.2 准入链缺项。"
    out = _sanitize_internal_labels(md)
    assert "4.2" not in out and "试多准入条件" in out


def test_no_whitelist_leak():
    md = "价位来自白名单。sanitize 已校验。"
    out = _sanitize_internal_labels(md)
    assert "白名单" not in out and "sanitize" not in out


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = fail = 0
    for f in fns:
        try:
            f()
            ok += 1
            print(f"PASS {f.__name__}")
        except Exception as e:
            fail += 1
            print(f"FAIL {f.__name__}: {e}")
            traceback.print_exc(limit=2)
    print(f"{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)