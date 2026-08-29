# -*- coding: utf-8 -*-
"""结论卡状态词一致性:左侧观望下 LLM 输出'试多候选'必须被校验拦截(2026-08-29)。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from alphaprism.planner.report_schema import validate_conclusion, normalize_conclusion

SNAP = ("## 标的\n| 日线 | 均线 | M5=0.845 |\n"
        "| 分时 | 现价 | 0.838 |\n## 大盘\n| 日线 | 均线 | M5=3894 |\n")
ANCHORS = {"anchor_price": 0.838, "breakout_add": 0.894,
           "pullback_add": 0.837, "cut_loss": 0.836, "stop_loss": 0.740}


def _card(signal_type, sentence="因上方压力密集,当前只宜观望,不宜追多"):
    return {"one_sentence": sentence, "signal_type": signal_type,
            "time_sensitivity": "当日收盘", "priority": "日线MACD定方向",
            "trigger": {"verb": "回踩", "price_level": "0.837", "confirm": "企稳信号",
                        "action": "加仓候选"},
            "invalidation": {"verb": "跌破", "price_level": "0.836",
                             "source": "回踩带下沿", "action": "减仓"}}


def test_left_watch_rejects_trial():
    """左侧观望下 signal_type='试多候选' → 校验失败(LLM 越级被拦,走程序兜底)。"""
    ok, issues = validate_conclusion(_card("试多候选"), SNAP, ANCHORS, 0.838,
                                     {"state_word": "左侧观望"})
    assert ok is False
    assert any("状态词" in i and "左侧观望" in i for i in issues)


def test_left_watch_accepts_watch():
    """左侧观望下 signal_type='观望' → 通过。"""
    ok, _ = validate_conclusion(_card("观望"), SNAP, ANCHORS, 0.838,
                                {"state_word": "左侧观望"})
    assert ok is True


def test_oversold_trial_ok():
    """超跌试多下 '试多候选' → 通过(映射允许)。"""
    ok, _ = validate_conclusion(_card("试多候选"), SNAP, ANCHORS, 0.838,
                                {"state_word": "超跌试多"})
    assert ok is True


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