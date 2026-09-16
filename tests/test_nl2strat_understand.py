# -*- coding: utf-8 -*-
"""understand 三态与确定性核验测试(fake chat 注入,不烧配额)。"""
from __future__ import annotations

import json

from alphaprism.nl2strat.contracts import (
    STATE_NEEDS_ASK, STATE_READY, STATE_UNSUPPORTED,
)
from alphaprism.nl2strat.understand import UnderstandService, merge_answers
from alphaprism.nl2strat.contracts import IntentSpec, Fragment


def _chat(payload: dict):
    def chat(messages, **kw):
        return json.dumps(payload, ensure_ascii=False)
    return chat


def _understand_payload(**over):
    base = {
        "state": "READY",
        "spec": {
            "symbol": "159516", "symbol_name": "半导体设备ETF",
            "start": "2023-01-01", "end": "2024-12-31",
            "fragments": [
                {"fragment_id": "f1", "text": "突破20日高点买入", "kind": "entry",
                 "origin": "user_explicit"},
                {"fragment_id": "f2", "text": "跌破成本8%止损", "kind": "stop",
                 "origin": "user_explicit"},
            ],
            "inference_authority": "default_ok", "notes": [],
        },
        "questions": [], "unsupported": [],
    }
    base.update(over)
    return base


ORIG = "突破20日高点买入,跌破成本8%止损"


class TestUnderstand:
    def test_ready(self):
        us = UnderstandService(_chat(_understand_payload()))
        r = us.run(ORIG)
        assert r.state == STATE_READY
        assert r.spec.symbol == "159516"
        assert len(r.spec.fragments) == 2

    def test_unsupported_exits(self):
        p = _understand_payload(
            state="UNSUPPORTED",
            unsupported=[{"text": "ROE>15%才买", "reason": "引擎无财务因子",
                          "nearest_alternative": "技术面趋势过滤"}])
        r = UnderstandService(_chat(p)).run("ROE>15%才买")
        assert r.state == STATE_UNSUPPORTED
        assert "ROE" in r.unsupported[0].text

    def test_junk_llm_output_is_needs_ask_not_crash(self):
        def bad_chat(messages, **kw):
            return "抱歉,我不确定"
        r = UnderstandService(bad_chat).run("随便说点什么")
        assert r.state == STATE_NEEDS_ASK
        assert r.questions[0].qid == "q_parse"

    def test_unknown_symbol_asks(self):
        p = _understand_payload()
        p["spec"]["symbol"] = "999999"
        r = UnderstandService(_chat(p)).run(ORIG)
        assert r.state == STATE_NEEDS_ASK
        assert any(q.field_hint == "symbol" for q in r.questions)

    def test_missing_window_default_ok_injects(self):
        p = _understand_payload()
        p["spec"]["start"] = ""
        p["spec"]["end"] = ""
        r = UnderstandService(_chat(p)).run(ORIG)
        assert r.state == STATE_READY
        assert r.spec.start and r.spec.end
        assert any("缺省注入" in n for n in r.spec.notes)

    def test_missing_window_explicit_asks(self):
        p = _understand_payload()
        p["spec"]["start"] = ""
        p["spec"]["end"] = ""
        p["spec"]["inference_authority"] = "explicit"
        r = UnderstandService(_chat(p)).run(ORIG)
        assert r.state == STATE_NEEDS_ASK
        assert any(q.field_hint == "start" for q in r.questions)

    def test_no_semantic_fragments_asks(self):
        p = _understand_payload()
        p["spec"]["fragments"] = []
        r = UnderstandService(_chat(p)).run(ORIG)
        assert r.state == STATE_NEEDS_ASK
        assert any(q.field_hint == "语义" for q in r.questions)


class TestMergeAnswers:
    def test_merge_bumps_revision(self):
        spec = IntentSpec(symbol="", fragments=[Fragment("f1", "x")])
        spec2 = merge_answers(spec, {"symbol": "159516", "start": "2023-01-01"})
        assert spec2.symbol == "159516" and spec2.revision == 2
