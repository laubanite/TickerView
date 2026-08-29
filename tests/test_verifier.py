"""verifier 模块回归(纯函数,A/B 实验前一版,2026-08-26)。"""
from __future__ import annotations

from alphaprism.planner.verifier import _parse_scores, _verifier_prompt


def test_verifier_prompt_has_card_candidate_and_criteria():
    p = _verifier_prompt("【卡片】M5=0.845", "【候选】结论:……")
    assert "【事实卡片】" in p and "【候选报告】" in p
    for key in ("assertiveness", "priority", "fact_align", "executable", "clarity"):
        assert key in p            # 五条准则都存在
    assert "Ground Truth Note" in p


def test_parse_scores_valid():
    s = _parse_scores(
        '{"assertiveness":82,"priority":64,"fact_align":90,"executable":70,"clarity":75,'
        '"overall":76,"weakest":"priority","feedback":"30分超买被说成动能积蓄"}')
    assert s["assertiveness"] == 82 and s["overall"] == 76
    assert s["weakest"] == "priority" and "动能积蓄" in s["feedback"]


def test_parse_scores_clamps_and_tolerates_garbage():
    s = _parse_scores('数值 96 # 注释\n{"priority": 140, "clarity": -5, "overall": 66}')
    assert s["priority"] == 100 and s["clarity"] == 0 and s["overall"] == 66


def test_parse_scores_non_json_fallback():
    assert _parse_scores("完全不是 JSON,评审觉得报告还行") == {}
    assert _parse_scores("") == {}


def test_score_scores_absent_without_call():
    """无 chat 时不自动调用(注入 chat 才打分);_parse_scores 空 → 0 分兜底。"""
    from alphaprism.planner.verifier import score_candidate

    def fake_chat(messages, cfg, temperature=0.1, max_tokens=500, timeout=150):
        return ('{"assertiveness":80,"priority":70,"fact_align":85,"executable":65,'
                '"clarity":72,"overall":74,"weakest":"executable",'
                '"feedback":"触发缺确认刻度",'
                '"executable_evidence":"若突破0.894,可加仓(无放量/收盘确认)"}')

    res = score_candidate("卡", "候选", None, chat_call=fake_chat, samples=1)
    assert res["overall"] == 74 and res["_n"] == 1
    assert res["executable_evidence"] and "可加仓" in res["executable_evidence"]

    res2 = score_candidate("卡", "候选", None, chat_call=lambda *a, **k: "无法解析", samples=2)
    assert res2.get("overall") == 0 and res2.get("reason")


def test_verifier_prompt_includes_rule_issues():
    """规则链删句记录作为强证据喂给 verifier(prompt 可见)。"""
    from alphaprism.planner.verifier import _verifier_prompt
    p = _verifier_prompt("卡", "候选", rule_issues=["价位不在白名单:0.835", "KDJ 标签矛盾"])
    assert "规则链前置发现" in p and "0.835" in p and "KDJ 标签矛盾" in p
    p2 = _verifier_prompt("卡", "候选")
    assert "规则链前置发现" not in p2


def test_select_best_with_refine_guard():
    """refine 复评退路:分数未升 → 保留原候选;升 → 采纳 refine。"""
    from alphaprism.planner.verifier import select_best_with_refine

    base = '{"assertiveness":70,"priority":70,"fact_align":80,"executable":60,'
    def chat(messages, cfg, **kw):
        prompt = messages[0]["content"]
        if "修改员" in prompt:                       # refine 生成
            return "【修订后】候选0 补齐触发确认。"
        if "修订后" in prompt:                       # refine 后复评(分数 60 < 66 → 拒绝)
            return base + '"clarity":80,"overall":60,"weakest":"executable","feedback":"仍缺"}'
        if "候选0" in prompt:
            return base + '"clarity":70,"overall":66,"weakest":"executable","feedback":"补确认刻度"}'
        return base + '"clarity":60,"overall":60,"weakest":"executable","feedback":"补确认刻度"}'

    out, scored, used = select_best_with_refine("卡", ["候选0", "候选1"], None,
                                                chat_call=chat, samples=1)
    assert used is False and "修订后" not in out          # 分数未升 → 回退原候选
    assert out.startswith("候选0")
    # 升高场景
    def chat_up(messages, cfg, **kw):
        prompt = messages[0]["content"]
        if "修改员" in prompt:                       # refine 生成
            return "【修订后】候选0 补齐触发确认+失效来源。"
        if "修订后" in prompt:                       # refine 后复评(88 > 66 → 采纳)
            return base + '"clarity":85,"overall":88,"weakest":"executable","feedback":"ok"}'
        if "候选0" in prompt:
            return base + '"clarity":70,"overall":66,"weakest":"executable","feedback":"补确认刻度"}'
        return base + '"clarity":60,"overall":60,"weakest":"executable","feedback":"x"}'
    out2, _s2, used2 = select_best_with_refine("卡", ["候选0", "候选1"], None,
                                               chat_call=chat_up, samples=1)
    assert used2 is True and "修订后" in out2


def test_evidence_persist_and_load():
    """步骤2:verifier 扣分证据结构化归档 → 结论卡先行可注入提醒(含过滤/过期)。"""
    import os
    import shutil

    from alphaprism.planner import verifier
    from alphaprism.planner.report_schema import conclusion_card_prompt

    tmp = os.path.join(os.path.dirname(__file__), "_tmp_evidence")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        verifier._EVIDENCE_DIR = tmp
        scores = {"overall": 86, "weakest": "executable",
                  "feedback": "触发应明确确认刻度",
                  "executable_evidence": "若突破0.894,可加仓(无放量/收盘确认)",
                  "priority_evidence": "未展开零下空头对30分超买的压制"}
        assert verifier.persist_evidence("999999", "2026-08-26", scores) is True
        got = verifier.load_evidence("999999", max_items=5)
        assert got and any("executable" in g and "0.894" in g for _, g in got)
        # 注入到卡 prompt
        ev = verifier.load_evidence("999999", max_items=1)
        p = conclusion_card_prompt("## 标的\n| 分时 | 现价 | 0.838 |\n## 大盘\n-",
                                   {"breakout_add": 0.894, "cut_loss": 0.836,
                                    "stop_loss": 0.740},
                                   {"state_word": "左侧观望"}, prior_evidence=ev)
        assert "近期评审缺陷提醒" in p and "0.894" in p
        # 无证据(空) → 不归档
        assert verifier.persist_evidence("888888", "2026-08-26",
                                         {"overall": 40}) is False
        # 过期条目不注入(30 天前)
        verifier.persist_evidence("777777", "2026-06-01", scores)
        assert verifier.load_evidence("777777") == []
    finally:
        verifier._EVIDENCE_DIR = None
        shutil.rmtree(tmp, ignore_errors=True)