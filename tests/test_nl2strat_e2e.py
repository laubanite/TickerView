# -*- coding: utf-8 -*-
"""P0 CLI 端到端(§8:手写案例跑通全链路;CI 无配额——chat 全部脚本化)。

覆盖:happy path、JUDGE 可疑三分支(确认续流/参数级/新增语义)、
语义陷阱方向反转(drift→修复→二次对账)、series 定义错误跨位置修复
(patch_node 经 related 反边授权)、修复认输 EXHAUSTED、UNSUPPORTED 出口。
真实回测引擎、真实 kline 数据(159516)参与 gates——只有 judge 判读在
部分案例被脚本化(其自身逻辑在 test_nl2strat_tools 单测)。
"""
from __future__ import annotations

import json
from collections import defaultdict, deque

import pytest

from alphaprism.nl2strat import tools as gates_mod
from alphaprism.nl2strat.contracts import ErrCode, ExitStatus, StratError
from alphaprism.nl2strat.session import Session

# ---------------------------------------------------------------- payloads

UNDERSTAND = {
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
PLAN = {"tasks": [
    {"fragment_id": "f1", "original": "突破20日高点买入",
     "mapping_hypothesis": "cross_above_series(prev20_high)"},
    {"fragment_id": "f2", "original": "跌破成本8%止损",
     "mapping_hypothesis": "layers_held + close_below_cost(0.92)"},
]}
DRAFT_OK = {"name": "突破持有", "entries": [
    {"fragment_id": "f2", "signal": {
        "action": {"sell": "clear_anchor"},
        "conds": [{"type": "layers_held", "layer": "anchor"},
                  {"type": "close_below_cost", "multiplier": 0.92}]}},
    {"fragment_id": "f1", "signal": {
        "action": {"buy": "anchor", "pct_of_full": 100.0},
        "conds": [{"type": "cross_above_series", "ref": "prev20_high"}]}},
]}
# 语义陷阱版:f2 方向译反(above 而非 below)——校验/回测全绿,只有语义关能抓
DRAFT_TRAP = {"name": "陷阱", "entries": [
    {"fragment_id": "f2", "signal": {
        "action": {"sell": "clear_anchor"},
        "conds": [{"type": "layers_held", "layer": "anchor"},
                  {"type": "close_above_series", "ref": "m_20"}]}},
    {"fragment_id": "f1", "signal": {
        "action": {"buy": "anchor", "pct_of_full": 100.0},
        "conds": [{"type": "cross_above_series", "ref": "prev20_high"}]}},
]}
# series 死声明版:f1 引用自定义 trend5(引擎不算=死条件)+ 内建名拼错 vol55
# ——双位置错误(series 声明段 + signals 段),组合修复
DRAFT_SERIESBUG = {"name": "序列病", "entries": [
    {"fragment_id": "f2", "signal": {
        "action": {"sell": "clear_anchor"},
        "conds": [{"type": "layers_held", "layer": "anchor"},
                  {"type": "close_below_cost", "multiplier": 0.92}]}},
    {"fragment_id": "f1", "signal": {
        "action": {"buy": "anchor", "pct_of_full": 100.0},
        "conds": [{"type": "cross_above_series", "ref": "trend5"},
                  {"type": "vol_ratio_above", "ref": "vol55",
                   "threshold": 1.5}]}},
], "series": {"trend5": {"type": "rolling_mean", "source": "close",
                         "lookback": 5}}}
# f3 扩展版(新增语义续流):draft 重跑须覆盖新 fragment
DRAFT_V2 = json.loads(json.dumps(DRAFT_OK))
DRAFT_V2["entries"].append(
    {"fragment_id": "f3", "signal": {
        "action": {"buy": "anchor", "pct_of_full": 100.0},
        "conds": [{"type": "close_below_series", "ref": "m_20"},
                  {"type": "vol_ratio_below", "ref": "vol20", "threshold": 0.8}]}})
SEM_OK = {"verdicts": [
    {"fragment_id": "f1", "dimension": "direction", "verdict": "ok"},
    {"fragment_id": "f2", "dimension": "param", "verdict": "ok"}]}
SEM_TRAP = {"verdicts": [
    {"fragment_id": "f1", "dimension": "direction", "verdict": "ok"},
    {"fragment_id": "f2", "dimension": "direction", "verdict": "drift",
     "expected": "跌破成本8%止损", "actual": "上穿 m_20 清仓(方向与对象双反)",
     "evidence": {"quote": "跌破成本8%止损", "yaml_path": "signals[0].conds[1]"}}]}
REPAIR_FIX_TRAP = {"tool": "remap", "args": {
    "fragment_id": "f2", "change": {
        "mode": "cond_replace", "cond_index": 1,
        "new_cond": {"type": "close_below_cost", "multiplier": 0.92}}}}
REPAIR_PATCH_SERIES = {"tool": "patch_node", "args": {
    "path": "series.trend5", "value": None}}            # 删除死声明
REPAIR_REMAP_REF = {"tool": "remap", "args": {
    "fragment_id": "f1", "change": {
        "mode": "param_patch", "cond_index": 0,
        "params": {"ref": "m_20"}}}}
REPAIR_REMAP_REF2 = {"tool": "remap", "args": {
    "fragment_id": "f1", "change": {
        "mode": "param_patch", "cond_index": 1,
        "params": {"ref": "vol20"}}}}
REPAIR_RESUBMIT = {"tool": "resubmit", "args": {}}
REPAIR_GIVEUP = {"tool": "give_up", "args": {"reason": "根因超出授权,认输"}}


# ---------------------------------------------------------------- fake chat

class Router:
    MARKERS = (("需求分析员", "understand"), ("翻译规划员", "plan"),
               ("装配员", "draft"), ("修复员", "repair"),
               ("语义忠实度审查", "semantic"), ("交付报告员", "synthesize"))

    def __init__(self):
        self.q = defaultdict(deque)
        self.used = defaultdict(int)

    def on(self, stage: str, *payloads):
        for p in payloads:
            self.q[stage].append(p)
        return self

    def stage_of(self, content: str) -> str:
        for marker, stage in self.MARKERS:
            if marker in content:
                return stage
        raise AssertionError(f"prompt 未命中任何阶段标记: {content[:60]}")

    def chat(self, messages, **kw):
        content = messages[-1]["content"]
        stage = self.stage_of(content)
        self.used[stage] += 1
        if not self.q[stage]:
            raise AssertionError(f"阶段 {stage} 脚本耗尽")
        item = self.q[stage].popleft()
        if stage == "synthesize":
            return str(item)
        return json.dumps(item, ensure_ascii=False)


def _mkrouter(**over):
    r = Router()
    r.on("understand", over.get("understand", UNDERSTAND))
    r.on("plan", *over.get("plan", [PLAN, PLAN]))
    r.on("draft", *over["draft"])
    for _ in over.get("semantic", [SEM_OK]):
        r.on("semantic", _)
    for _ in over.get("repair", []):
        r.on("repair", _)
    r.on("synthesize", "交付报告OK")
    return r


def _session(tmp_path, router, answers, monkeypatch, judge_script=None):
    if judge_script is None:
        monkeypatch.setattr(gates_mod, "judge", lambda r, s: [])
    else:
        seq = deque(judge_script)
        monkeypatch.setattr(gates_mod, "judge",
                            lambda r, s: seq.popleft() if seq else [])
    q = deque(answers)
    seen = []

    def ask(question):
        seen.append(question)
        return q.popleft() if q else ""
    sess = Session(chat=router.chat, ask_fn=ask,
                   out_dir=tmp_path / "out", db_path=tmp_path / "t.db",
                   trace_dir=tmp_path / "traces")
    sess.seen_questions = seen
    return sess


INPUT = "突破20日高点买入,跌破成本8%止损"


# ---------------------------------------------------------------- 案例

class TestE2E:
    def test_happy_path(self, tmp_path, monkeypatch):
        r = _mkrouter(draft=[DRAFT_OK])
        sess = _session(tmp_path, r, [], monkeypatch)
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        assert so.yaml_path and (tmp_path / "out").exists()
        assert "close_below_cost" in so.outcome.yaml_text
        assert so.outcome.llm_calls >= 4
        assert r.used["draft"] == 1 and r.used["repair"] == 0
        # trace + run 索引落盘
        assert (tmp_path / "traces" / f"{so.run_id}.jsonl").exists()
        from alphaprism.nl2strat.trace import load_run
        rec = load_run(so.run_id, conn=_conn(tmp_path))
        assert rec and rec.exit_status == "READY" and rec.llm_calls == so.outcome.llm_calls

    def test_suspect_confirm_skips_rerun(self, tmp_path, monkeypatch):
        """续流分支①:可疑·确认 → 直达 semantic_verify,不重跑 draft/run。"""
        r = _mkrouter(draft=[DRAFT_OK])
        sess = _session(tmp_path, r, ["1"], monkeypatch,
                        judge_script=[[StratError(ErrCode.SUSPECT_TURNOVER,
                                                  "signals", "年换手 300 笔偏高")]])
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        assert r.used["draft"] == 1                      # 确认后未重跑装配
        assert sess.seen_questions[0].qid == "judge_suspect"
        assert so.outcome.cost_versions.get("semantic_verify") == 1

    def test_suspect_adjust_param_reenters_draft(self, tmp_path, monkeypatch):
        """续流分支②:参数级调整 → IntentSpec patch → 重进 draft(下游全重跑)。"""
        r = _mkrouter(draft=[DRAFT_OK, DRAFT_OK])
        sess = _session(tmp_path, r, ["改成10%"], monkeypatch,
                        judge_script=[[StratError(ErrCode.SUSPECT_FREQ_MISMATCH,
                                                  "signals", "与长期持有描述不符")]])
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        assert r.used["draft"] == 2
        assert so.outcome.spec.revision >= 2
        assert any("参数调整" in n for n in so.outcome.spec.notes)

    def test_suspect_adjust_new_semantics(self, tmp_path, monkeypatch):
        """续流分支③:新增语义 → 片段入 spec → plan/draft 重跑并覆盖新片段。"""
        r = _mkrouter(draft=[DRAFT_OK, DRAFT_V2])
        sess = _session(tmp_path, r, ["缩量回调至均线企稳再买"], monkeypatch,
                        judge_script=[[StratError(ErrCode.SUSPECT_TURNOVER,
                                                  "signals", "年换手 300 笔偏高")]])
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        fids = [f.fragment_id for f in so.outcome.spec.fragments]
        assert "f3" in fids
        assert "vol_ratio_below" in so.outcome.yaml_text
        assert r.used["draft"] == 2

    def test_semantic_trap_repaired(self, tmp_path, monkeypatch):
        """语义陷阱:校验全绿的方向反转,被语义关抓住 → 修复 → 二次对账通过。"""
        r = _mkrouter(draft=[DRAFT_TRAP], semantic=[SEM_TRAP, SEM_OK],
                      repair=[REPAIR_FIX_TRAP, REPAIR_RESUBMIT])
        sess = _session(tmp_path, r, [], monkeypatch)
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        assert so.outcome.semantic_fail_count == 1
        assert "close_below_cost" in so.outcome.yaml_text   # 反转已被扳回
        assert r.used["repair"] == 2

    def test_series_bug_crosspath_fix(self, tmp_path, monkeypatch):
        """series 死声明跨位置修复:声明段删除 + 两处 cond ref 改内建,组合拳。"""
        r = _mkrouter(draft=[DRAFT_SERIESBUG],
                      repair=[REPAIR_PATCH_SERIES, REPAIR_REMAP_REF,
                              REPAIR_REMAP_REF2, REPAIR_RESUBMIT])
        sess = _session(tmp_path, r, [], monkeypatch)
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        import yaml
        tree = yaml.safe_load(so.outcome.yaml_text)
        assert "trend5" not in (tree.get("series") or {})      # 死声明已删
        conds = tree["signals"][1]["conds"]
        assert conds[0]["ref"] == "m_20" and conds[1]["ref"] == "vol20"
        assert so.outcome.scope_denied == 0                    # 未越权未乒乓
        assert r.used["repair"] == 4

    def test_semantic_multidim_drift_same_fragment_repairs(self, tmp_path, monkeypatch):
        """回归:同片段多维度 drift 只能记 1 次失败,须先给修复机会(而非秒裁)。"""
        SEM_MULTI = {"verdicts": [
            {"fragment_id": "f1", "dimension": "direction", "verdict": "ok"},
            {"fragment_id": "f2", "dimension": "direction", "verdict": "drift",
             "expected": "跌破成本", "actual": "上穿均线",
             "evidence": {"quote": "跌破成本8%止损", "yaml_path": "signals[0].conds[1]"}},
            {"fragment_id": "f2", "dimension": "param", "verdict": "drift",
             "expected": "8%", "actual": "1.08",
             "evidence": {"quote": "跌破成本8%止损", "yaml_path": "signals[0].conds[1]"}}]}
        r = _mkrouter(draft=[DRAFT_TRAP], semantic=[SEM_MULTI, SEM_OK],
                      repair=[REPAIR_FIX_TRAP, REPAIR_RESUBMIT])
        sess = _session(tmp_path, r, [], monkeypatch)
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.READY, so.reason
        assert so.outcome.semantic_fail_count == 1      # 两个 drift → 记 1 轮
        assert "close_below_cost" in so.outcome.yaml_text

    def test_repair_giveup_exhausted(self, tmp_path, monkeypatch):
        bad = json.loads(json.dumps(DRAFT_OK))
        bad["entries"][1]["signal"]["conds"][0]["type"] = "golden_cross"
        r = _mkrouter(draft=[bad], repair=[REPAIR_GIVEUP])
        sess = _session(tmp_path, r, [], monkeypatch)
        so = sess.run(INPUT)
        assert so.exit_status == ExitStatus.EXHAUSTED
        assert so.outcome.draft is not None        # 部分产物交还
        assert "认输" in so.reason or "give_up" in so.reason

    def test_unsupported_exit(self, tmp_path, monkeypatch):
        u = dict(UNDERSTAND)
        u2 = json.loads(json.dumps(UNDERSTAND))
        u2["unsupported"] = [{"text": "ROE大于15%才买", "reason": "引擎无财务因子",
                              "nearest_alternative": "用趋势过滤近似"}]
        r = _mkrouter(draft=[DRAFT_OK], understand=u2)
        sess = _session(tmp_path, r, [], monkeypatch)
        so = sess.run("ROE大于15%才买,突破20日高点买入,跌破成本8%止损")
        assert so.exit_status == ExitStatus.UNSUPPORTED
        assert so.outcome is None


# ---------------------------------------------------------------- helpers

def _conn(tmp_path):
    from alphaprism.nl2strat.trace import connect
    return connect(tmp_path / "t.db")
