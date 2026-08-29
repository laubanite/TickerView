# -*- coding: utf-8 -*-
"""segments 分层管线回归(深入分析 v3.0:骨架底线句 + LLM 片段填充)。

覆盖:
- filter_analysis_input:内外盘/资金信号行剔除(架构关键:不制造"参考性弱还分析"矛盾)
- skeleton_baselines:8 段底线句生成(事实+方向,确定性)
- assemble_report:10 段齐全 + 程序段 + 结论卡 + 建议类别
- 段失败回退:LLM 片段缺失 → 用底线句(输出永远完整)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alphaprism.planner.segments import (assemble_report, filter_analysis_input,
                                         skeleton_baselines, segment_prompt)
from alphaprism.planner.intraday_engine import param_anchors, signal_state

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "snapshots", "515050.md")


def _snap() -> str:
    with open(_FIX, encoding="utf-8") as f:
        return f.read()


def _facts():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "snap_render", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "test_snapshot_render.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {"etf": mod.mk_515050()}


def test_filter_removes_orderbook_and_signal():
    """内外盘/资金·信号行剔除;五档/份额保留。"""
    filtered = filter_analysis_input(_snap())
    assert "外内盘" not in filtered
    assert "| 资金 | 信号 |" not in filtered
    assert "| 盘口 | 五档 |" in filtered
    assert "| 资金 | 份额 |" in filtered


def test_baselines_have_anchor_numbers():
    """底线句含程序事实(现价/价位),且不含 LLM 可篡改的自由表述(2026-08-29 结构:五=程序表,六=合并,八=资金)。"""
    base = skeleton_baselines(_facts(), signal_state(_facts()),
                              param_anchors(_facts()), None)
    assert set(base) == {"一、大盘环境", "二、标的·日线", "三、标的·30分",
                         "四、分时盘口", "六、操作建议与风险提示", "八、资金面"}
    assert "1.036" in base["二、标的·日线"]
    assert "0.887" in base["六、操作建议与风险提示"] or "减仓" in base["六、操作建议与风险提示"]


def test_assemble_ten_sections():
    """组装:8 段齐全(五=程序表,六=合并建议,八=资金),程序段/建议类别正确。"""
    base = skeleton_baselines(_facts(), signal_state(_facts()),
                              param_anchors(_facts()), None)
    md = assemble_report({}, base, {
        "五、关键价位表": "现价 1.036 对应的关键价位:\n\n| 类型 | 价位 | 说明 |",
        "七、市场状态": "趋势 · 左侧观望"}, "观望")
    for seg in ("一、大盘环境", "二、标的·日线", "三、标的·30分", "四、分时盘口",
                "五、关键价位表", "六、操作建议与风险提示", "七、市场状态",
                "八、资金面"):
        assert f"## {seg}" in md, f"缺段 {seg}"
    assert "## 建议类别: 观望" in md
    assert "结论卡" not in md          # 卡不在 assemble 插入


def test_assemble_llm_segment_replaces_baseline():
    """LLM 片段覆盖底线句;缺段回退底线句(输出永远完整)。"""
    base = skeleton_baselines(_facts(), signal_state(_facts()),
                              param_anchors(_facts()), None)
    md = assemble_report({"二、标的·日线": "LLM 扩写:现价紧贴 M10,上方压力近在咫尺。"},
                         base, {"五、关键价位表": "程序表", "七、市场状态": "趋势"},
                         "观望")
    assert "LLM 扩写" in md and "M10" in md          # LLM 片段生效
    assert "## 四、分时盘口" in md and "程序事实" in md  # 缺失段回退底线句


def test_segment_prompt_contains_baseline_and_tone():
    """片段 prompt 六要素:结构化 facts + 底线句 + 解释范围 + 禁词 + 全局基调 + 长度。"""
    p = segment_prompt("八、操作建议", "程序锚点:锚定价 1.036;突破 1.119",
                       "## 标的\n| 分时 | 现价 | 1.036 |",
                       {"etf": {"minute": {"price": 1.036}},
                        "rs_5d": None, "index": None, "catalyst": {}},
                       {"state_word": "左侧观望", "scenario": "趋势"},
                       {"anchor_price": 1.036, "breakout_add": 1.119,
                        "cut_loss": 1.028, "stop_loss": 0.887,
                        "breakout_src": "近20日上沿"}, "卡")
    assert "禁止修改" in p and "禁止" in p        # 禁止事项
    assert "左侧观望" in p                         # 全局基调
    assert "1.036" in p and "1.119" in p           # 底线句数字
    assert "本段事实(JSON" in p                    # ① 结构化 facts
    assert "本段解释范围" in p                     # ③ 范围
    assert "3-5 句" in p                           # ⑥ 长度
    assert "满仓" in p or "禁止" in p              # ④ 禁词


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