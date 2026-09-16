# -*- coding: utf-8 -*-
"""repair.py 测试:工作记忆折叠(compaction)与修复子 agent 护栏。

§8 纪律:compaction 与越界关卡**先写测试后写实现**。
验证面(§4.2):
- 上下文固定构成 {最新 errors, 当前草稿全文, 尝试摘要序列, 剩余预算};
- 被覆盖历史 errors 压成计数行;失败尝试一行摘要;工具观察消费即截断;确定性零 LLM;
- 越界拒绝不计轮、计步;同目标连拒 2 次升级 ask_user(兜底防乒乓);
- selfcheck ≤1/轮、≤2/run;judge_candidates 仅付费档;
- give_up → gave_up(meta_eligible=False,v6"认输也是判断力");
  步/轮封顶 → exhausted(meta_eligible=True,被动耗尽才问)。
"""
from __future__ import annotations

from alphaprism.nl2strat.contracts import (
    AuthzSet, Draft, ErrCode, Fragment, IntentSpec, RepairAttempt, StratError,
)
from alphaprism.nl2strat import repair


def _spec():
    return IntentSpec(run_id="t", symbol="159516", start="2023-01-01", end="2024-12-31",
                      fragments=[Fragment("f1", "突破20日高点买入"),
                                 Fragment("f2", "跌破成本8%止损")])


def _draft() -> Draft:
    return Draft.from_entries("t", [
        {"fragment_id": "f1", "signal": {
            "type": "cross_above_series", "ref": "prev20_high",
            "action": {"buy": "anchor", "pct_of_full": 100.0}}},
        {"fragment_id": "f2", "signal": {
            "action": {"sell": "clear_anchor"},
            "conds": [{"type": "layers_held", "layer": "anchor"},
                      {"type": "close_below_rolling", "ref": "low99_close"}]}},
    ])


def _err(code, where, related=()):
    return StratError(code=code, where=where, msg=f"{code}@{where}",
                      related_paths=list(related))


# ---------------------------------------------------------------- compaction

class TestCompaction:
    def test_context_fixed_composition(self):
        mem = repair.WorkingMemory()
        errs = [_err(ErrCode.REF_UNDEFINED, "signals[1].conds[1]", ["series.low99_close"])]
        ctx = mem.context(errs, _draft(), steps_left=5, rounds_left=2)
        assert "REF_UNDEFINED" in ctx
        assert "signals[1].conds[1]" in ctx
        assert "cross_above_series" in ctx             # 当前草稿全文在场
        assert "步预算剩 5" in ctx and "轮预算剩 2" in ctx
        assert mem.ATTEMPT_HEADER in ctx               # 尝试摘要序列(空也要在场)

    def test_fold_errors_produces_count_line_not_raw(self):
        mem = repair.WorkingMemory()
        mem.fold_errors([_err(ErrCode.REF_UNDEFINED, "signals[1].conds[1]")])
        mem.add_attempt(RepairAttempt(1, 1, "remap", "f2", "改 low20_close 但仍 ref 未定义",
                                      [ErrCode.REF_UNDEFINED]))
        ctx = mem.context([], _draft(), 4, 2)
        assert "signals[1].conds[1] 已尝试修复 1 轮" in ctx
        assert mem.attempts[0].one_line() in ctx
        assert "REF_UNDEFINED@signals[1].conds[1]" not in ctx   # 原始 detail 被覆盖

    def test_repeated_same_target_merges_count(self):
        mem = repair.WorkingMemory()
        mem.fold_errors([_err(ErrCode.NO_TRADES, "signals")])
        mem.fold_errors([_err(ErrCode.NO_TRADES, "signals")])
        ctx = mem.context([], _draft(), 3, 3)
        assert "signals 已尝试修复 2 轮" in ctx

    def test_observation_consumed_immediately(self):
        mem = repair.WorkingMemory()
        mem.put_observation("dsl_card", "很长的卡片" * 50)
        ctx1 = mem.context([], _draft(), 3, 3)
        assert "很长的卡片" in ctx1
        ctx2 = mem.context([], _draft(), 3, 3)         # 消费即截断
        assert "很长的卡片" not in ctx2

    def test_zero_llm_deterministic(self):
        mem = repair.WorkingMemory()
        mem.add_attempt(RepairAttempt(1, 1, "patch_node", "series.vol5", "改 lookback", []))
        errs = [_err(ErrCode.NO_TRADES, "signals")]
        assert mem.context(errs, _draft(), 2, 1) == mem.context(errs, _draft(), 2, 1)


# ---------------------------------------------------------------- 修复子 agent

class _Queue:
    def __init__(self, actions):
        self.actions = list(actions)
        self.contexts = []

    def __call__(self, ctx):
        self.contexts.append(ctx)
        if not self.actions:
            return {"tool": "give_up", "args": {"reason": "脚本耗尽"}}
        return self.actions.pop(0)


class _FakeGates:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.script.pop(0) if self.script else []


def _agent(actions, *, gates=None, errors=None, paid=False, steps=6, rounds=3,
           selfcheck_fn=None, judge_fn=None):
    return repair.RepairAgent(
        draft=_draft(), spec=_spec(),
        errors=errors if errors is not None else [
            _err(ErrCode.REF_UNDEFINED, "signals[1].conds[1]", ["series.low99_close"])],
        budget=repair.RepairBudget(steps_left=steps, rounds_left=rounds),
        next_action=_Queue(actions),
        gates=gates or _FakeGates([[]]),
        paid=paid, selfcheck_fn=selfcheck_fn, judge_fn=judge_fn,
    )


class TestRepairLoop:
    def test_remap_then_resubmit_pass(self):
        ag = _agent([
            {"tool": "remap", "args": {
                "fragment_id": "f2",
                "change": {"mode": "param_patch", "cond_index": 1,
                           "params": {"ref": "low20_close"}}}},
            {"tool": "resubmit", "args": {}},
        ])
        out = ag.run()
        assert out.status == repair.R_STATUS_PASSED
        assert ag.draft.signals()[1]["conds"][1]["ref"] == "low20_close"
        assert len(ag.applied_patches) == 1            # 已应用 diff 无条件进审计

    def test_scope_denied_costs_step_not_round_then_force_ask(self):
        ag = _agent([
            {"tool": "patch_node", "args": {"path": "accounts.full_cash", "value": 1}},
            {"tool": "patch_node", "args": {"path": "accounts.full_cash", "value": 2}},
        ])
        out = ag.run()
        assert out.status == repair.R_STATUS_ASK_USER   # 同目标连拒 2 次兜底升级
        assert out.question is not None
        assert ag.budget.rounds_left == 3               # 不计轮
        assert ag.budget.steps_left == 4                # 计步

    def test_request_scope_grants_then_write_ok(self):
        ag = _agent([
            {"tool": "request_scope",
             "args": {"path": "series.low99_close", "reason": "补定义修正"}},
        ])
        out = ag.run()
        assert out.status == repair.R_STATUS_SCOPE_PENDING
        assert out.scope_request["path"] == "series.low99_close"
        # 用户确认 → 该路径授权扩入本轮;答复作为新观察回子循环
        ag.grant_scope("series.low99_close")
        ag.answer("确认,按此修复")
        ag.next_action = _Queue([
            {"tool": "patch_node", "args": {
                "path": "series.low99_close",
                "value": {"type": "rolling_min", "source": "close", "lookback": 99}}},
            {"tool": "resubmit", "args": {}},
        ])
        out2 = ag.resume()
        assert out2.status == repair.R_STATUS_PASSED
        assert ag.draft.tree["series"]["low99_close"]["lookback"] == 99

    def test_give_up_not_meta_eligible(self):
        ag = _agent([{"tool": "give_up", "args": {"reason": "改不动"}}])
        out = ag.run()
        assert out.status == repair.R_STATUS_GAVE_UP
        assert out.meta_eligible is False

    def test_steps_exhausted_is_meta_eligible(self):
        ag = _agent([{"tool": "dsl_card", "args": {"query": "止损"}}] * 8, steps=6)
        out = ag.run()
        assert out.status == repair.R_STATUS_EXHAUSTED
        assert out.meta_eligible is True                # 被动耗尽才问(v6)

    def test_rounds_exhausted_is_meta_eligible(self):
        ag = _agent([{"tool": "resubmit", "args": {}}] * 4, rounds=3, steps=8,
                    gates=_FakeGates([[_err(ErrCode.NO_TRADES, "signals")]] * 3))
        out = ag.run()
        assert out.status == repair.R_STATUS_EXHAUSTED   # 第 4 次 resubmit 撞轮封顶=被动
        assert out.meta_eligible is True

    def test_resubmit_passes_through_new_errors(self):
        ag = _agent([
            {"tool": "resubmit", "args": {}},
            {"tool": "resubmit", "args": {}},           # 第二轮:通过
        ], gates=_FakeGates([[_err(ErrCode.NO_TRADES, "signals")], []]))
        out = ag.run()
        assert out.status == repair.R_STATUS_PASSED
        assert ag.budget.rounds_left == 1

    def test_selfcheck_limits(self):
        """同轮 ≤1、run ≤2:第 2 次(同轮)拒,第 3 次(新轮)放行,第 4 次(run 封顶)拒。"""
        calls = []

        def sfn(fids):
            calls.append(list(fids))
            return []

        ag = _agent([
            {"tool": "selfcheck", "args": {"fragment_ids": ["f1"]}},
            {"tool": "selfcheck", "args": {"fragment_ids": ["f1"]}},     # 同轮第 2 次拒
            {"tool": "resubmit", "args": {}},                            # → 新轮(gates 再报错)
            {"tool": "selfcheck", "args": {"fragment_ids": ["f2"]}},     # 新轮放行
            {"tool": "selfcheck", "args": {"fragment_ids": ["f2"]}},     # run 封顶拒
        ], gates=_FakeGates([[_err(ErrCode.NO_TRADES, "signals")]]), steps=8)
        ag.selfcheck_fn = sfn
        ag.run()
        assert calls == [["f1"], ["f2"]]

    def test_judge_candidates_free_denied_paid_ok(self):
        ag = _agent([{"tool": "judge_candidates",
                      "args": {"fragment_id": "f2",
                               "candidates": ["close_below_cost", "close_below_rolling"]}}],
                    paid=False, steps=1)
        out = ag.run()
        assert out.status == repair.R_STATUS_EXHAUSTED  # 步耗尽(免费档该调用被拒但耗步)
        assert ag.unavailable_calls == 1

        def jfn(fid, cands):
            return {"ranking": cands[::-1], "reasons": ["更贴合原话" for _ in cands]}
        ag2 = _agent([{"tool": "judge_candidates",
                       "args": {"fragment_id": "f2",
                                "candidates": ["a", "b"]}},
                      {"tool": "give_up", "args": {"reason": "done"}}],
                    paid=True, steps=3, judge_fn=jfn)
        out2 = ag2.run()
        assert ag2.unavailable_calls == 0
        assert out2.status == repair.R_STATUS_GAVE_UP

    def test_ask_user_answer_becomes_observation(self):
        ag = _agent([{"tool": "ask_user", "args": {"question": "要缩量还是放量?"}}])
        out = ag.run()
        assert out.status == repair.R_STATUS_ASK_USER
        assert "缩量" in out.question.text
        ag.answer("放量突破才对")
        ag.next_action = _Queue([{"tool": "resubmit", "args": {}}])
        out2 = ag.resume()
        assert out2.status == repair.R_STATUS_PASSED
        assert any("放量突破才对" in a.summary for a in ag.memory.attempts)

    def test_degrade_track_full_yaml_in_action(self):
        """降级轨动作 {"yaml":…}:越界 reject 零副作用;授权内变更被吸收。"""
        ag = _agent([])
        bad = ag.draft.to_dict()["tree"]
        bad["signals"][0]["conds"][0]["ref"] = "prev60_high"
        ag.next_action = _Queue([{"yaml": bad},
                                 {"tool": "give_up", "args": {"reason": "x"}}])
        out = ag.run()
        assert ag.draft.signals()[0]["conds"][0]["ref"] == "prev20_high"  # 未被污染
        ag2 = _agent([])
        good = ag2.draft.to_dict()["tree"]
        good["signals"][1]["conds"][1]["ref"] = "low20_close"   # 在 REF_UNDEFINED 授权内
        ag2.next_action = _Queue([{"yaml": good}, {"tool": "resubmit", "args": {}}])
        out2 = ag2.run()
        assert out2.status == repair.R_STATUS_PASSED
        assert ag2.draft.signals()[1]["conds"][1]["ref"] == "low20_close"

    def test_meta_evidence_reuses_compaction(self):
        ag = _agent([
            {"tool": "remap", "args": {"fragment_id": "f2",
                                       "change": {"mode": "bogus"}}},
            {"tool": "dsl_card", "args": {"query": "止损"}},
        ], steps=2)
        out = ag.run()
        ev = ag.meta_evidence()
        assert out.meta_eligible
        assert ev["attempts"]                            # compaction 已在产的摘要序列
        assert "REF_UNDEFINED" in str(ev["code_dist"])
        assert ev["remaining"]["steps"] == 0

    def test_unknown_tool_rejected_as_observation(self):
        ag = _agent([{"tool": "write_python", "args": {}},
                     {"tool": "give_up", "args": {"reason": "乱试终止"}}], steps=3)
        out = ag.run()
        assert out.status == repair.R_STATUS_GAVE_UP     # 未注册动作=封闭动作集外,拒绝但循环继续
