# -*- coding: utf-8 -*-
"""无 pytest 的最小回归 runner:加载 tests/*.py 中所有 test_* 函数逐个执行。

沙箱网络受限装不了 pytest,而现有测试是纯函数式(无 fixture/mock)——
直接按函数执行即可拿到 pass/fail 真实状态。
用法:python scripts/_run_tests_no_pytest.py [模块路径...]
"""
import importlib.util
import sys
import traceback
import os

ROOT = r"E:\AgentProjects\AlphaPrism"
sys.path.insert(0, ROOT)


def run_module(path: str) -> tuple[int, int]:
    spec = importlib.util.spec_from_file_location("_tmod_" + os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fns = [(n, getattr(mod, n)) for n in dir(mod)
           if n.startswith("test_") and callable(getattr(mod, n))]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)
    print(f"== {os.path.basename(path)}: {passed} passed, {failed} failed, total {len(fns)}")
    return passed, failed


if __name__ == "__main__":
    targets = sys.argv[1:] or [os.path.join(ROOT, "tests", "test_intraday_engine.py")]
    tp = tf = 0
    for t in targets:
        p, f = run_module(t)
        tp += p
        tf += f
    print(f"\nTOTAL: {tp} passed, {tf} failed")
    sys.exit(1 if tf else 0)