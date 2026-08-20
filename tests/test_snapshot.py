"""盘中快照回归测试(snapshot.py,里程碑3 延伸/§5.4)。"""
from __future__ import annotations

from datetime import datetime

from alphaprism.planner.rulemodel import Global, MarketGate, RuleModel
from alphaprism.planner.snapshot import _rule_takeaway, build_snapshot


def _model() -> RuleModel:
    m = RuleModel()
    m.global_ = Global(market_gate=MarketGate(conclusion="大盘破 MA60"))
    m.global_.discipline = ["J 值>85 不追高", "回踩买、突破买,绝不追买"]
    return m


def _check_result() -> dict:
    return {
        "gate": {"open": False, "j": 58.2, "rule": "J<60 不加仓", "action": "所有 ETF 不加仓"},
        "verdicts": [
            {"code": "516020", "name": "化工ETF华宝", "price": 0.860, "change_pct": 1.2,
             "vol_label": "缩量", "conclusion": "接近买点", "near": "买区 0.855-0.860"},
            {"code": "515050", "name": "通信ETF华夏", "price": 1.010, "change_pct": -1.0,
             "vol_label": "放量", "conclusion": "平静", "near": ""},
        ],
    }


def test_rule_takeaway_differentiates_active():
    """差异输出:有动作的重点写,平静的一行带过。"""
    txt = _rule_takeaway(_check_result()["verdicts"], _check_result()["gate"],
                         _model().global_.discipline)
    assert "大盘门控关闭" in txt
    assert "化工ETF华宝" in txt and "**接近买点**" in txt
    assert "🔴 化工ETF华宝" in txt
    assert "其余 1 只平静" in txt
    assert "接下来只看一件事" in txt


def test_rule_takeaway_all_quiet():
    """全平静 → 无差异重点,只看大盘。"""
    verdicts = [dict(v, conclusion="平静") for v in _check_result()["verdicts"]]
    txt = _rule_takeaway(verdicts, {"open": True, "j": 80.0}, [])
    assert "无触发,全部观望" in txt
    assert "保持观察" in txt


def test_build_snapshot_without_llm(monkeypatch):
    """LLM 失败 → 降级为纯规则事实(不编造解读)。"""
    def _no_llm(*a, **k):
        return None
    monkeypatch.setattr("alphaprism.llm.chat", _no_llm)
    md = build_snapshot(_model(), _check_result(), now=datetime(2026, 8, 20, 14, 30))
    assert md.startswith("# 盘中快照 14:30")
    assert "**大盘**" in md
    assert "**现状**" in md
    assert "**差异重点**" in md
    assert "**接下来只看一件事**" in md