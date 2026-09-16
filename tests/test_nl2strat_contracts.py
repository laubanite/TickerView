# -*- coding: utf-8 -*-
"""contracts.py 测试:节点路径/授权集/Draft 装配/语义准入/续流契约。

§8 纪律:授权集检查与 semantic_verify 准入**先写测试后写实现**——本文件即其
验收面,tools/repair/loop 全部经这里冻结的语义消费契约。
"""
from __future__ import annotations

import pytest

from alphaprism.nl2strat.contracts import (
    RUN_TABLE_DDL, AuthzSet, BUDGET_FREE, BUDGET_PAID, Draft, ErrCode,
    ERROR_META, Fragment, IntentSpec, RunRecord, SemanticEvidence,
    SemanticVerdict, StratError, admissible_verdicts, assign_path, budget_for,
    delete_path, normalize_path, path_join, path_parent, path_tokens,
    path_within, render_tokens, resolve_path, tree_diff,
)


# ---------------------------------------------------------------- 节点路径

class TestPaths:
    def test_normalize_roundtrip(self):
        assert normalize_path("signals[3].conds[1].threshold") == \
            "signals[3].conds[1].threshold"
        assert normalize_path("  series.vol5 ") == "series.vol5"
        assert normalize_path("fragment:f-2") == "fragment:f-2"

    def test_invalid_paths_rejected(self):
        for bad in ["", "signals[", "a..b", "1abc", "signals[x]", "a b"]:
            with pytest.raises(ValueError):
                normalize_path(bad)

    def test_tokens_render(self):
        toks = path_tokens("signals[3].conds[1]")
        assert toks == ["signals", 3, "conds", 1]
        assert render_tokens(toks) == "signals[3].conds[1]"
        assert path_tokens("fragment:x") == ["fragment:x"]

    def test_within(self):
        assert path_within("signals[3]", "signals[3]")
        assert path_within("signals[3]", "signals[3].conds[1].ref")
        assert not path_within("signals[3].conds[1]", "signals[3]")   # 祖先方向不成立
        assert not path_within("signals[3]", "signals[30]")           # 前缀≠子树(按 token)
        assert path_within("series", "series.vol5")

    def test_resolve_assign_delete(self):
        tree = {"signals": [{"id": "a", "conds": [{"type": "x", "t": 1}]}]}
        assert resolve_path(tree, "signals[0].conds[0].type") == "x"
        assign_path(tree, "signals[0].conds[0].t", 2)
        assert tree["signals"][0]["conds"][0]["t"] == 2
        assign_path(tree, "signals[0].conds[1]", {"type": "y"})
        assert len(tree["signals"][0]["conds"]) == 2
        assert delete_path(tree, "signals[0].conds[1]") == {"type": "y"}
        with pytest.raises(KeyError):
            resolve_path(tree, "series.nope")

    def test_parent_join(self):
        assert path_parent("signals[0].conds[1]") == "signals[0].conds"
        assert path_parent("series") is None
        assert path_parent("fragment:x") is None
        assert path_join("", "signals", 3) == "signals[3]"
        assert path_join("signals[0]", "conds", 1) == "signals[0].conds[1]"


# ---------------------------------------------------------------- 错误码表

class TestErrorCodes:
    def test_four_frozen_codes_present(self):
        for code in ("MISSING_FRAGMENT", "EXTRA_SIGNAL", "SEMANTIC_DRIFT", "SCOPE_DENIED"):
            assert getattr(ErrCode, code) == code
            assert code in ERROR_META

    def test_route_values(self):
        assert ERROR_META[ErrCode.SCOPE_DENIED]["route"] == "guard"       # 不计轮耗步
        assert ERROR_META[ErrCode.MISSING_FRAGMENT]["route"] == "repair"  # 注入修复
        assert ERROR_META[ErrCode.SUSPECT_TURNOVER]["route"] == "ask_user"


# ---------------------------------------------------------------- 授权集

def _e(code, where, related=()):
    return StratError(code=code, where=where, related_paths=list(related))


class TestAuthz:
    def test_build_from_errors_union(self):
        errs = [_e(ErrCode.REF_UNDEFINED, "signals[2].conds[0]",
                   ["series.vol5", "signals[5]"]),
                _e(ErrCode.MISSING_FRAGMENT, "fragment:f3")]
        az = AuthzSet.build(errs)
        assert az.allows("signals[2].conds[0].ref")    # where 子树内
        assert az.allows("series.vol5")                # related 派生(机械扩权不惊动模型)
        assert az.allows("signals[5].conds[1]")        # related 指向另一 signal
        assert az.allows_fragment("f3")
        assert not az.allows("signals[1].conds[0]")    # 兄弟越界
        assert not az.allows("signals[2]")             # 祖先=波及兄弟,拒绝
        assert not az.allows_fragment("f9")

    def test_granted_expansion(self):
        az = AuthzSet.build([], granted=["series.low20_close", "fragment:f1"])
        assert az.allows("series.low20_close.lookback")
        assert az.allows_fragment("f1")

    def test_invalid_path_ignored_not_raised(self):
        az = AuthzSet.build([_e(ErrCode.UNKNOWN_COND, "not a path!")])
        assert not az.allows("name")

    def test_describe_sorted(self):
        az = AuthzSet.build([_e(ErrCode.UNKNOWN_COND, "series.b"),
                             _e(ErrCode.UNKNOWN_COND, "series.a")])
        assert az.describe() == ["series.a", "series.b"]


# ---------------------------------------------------------------- Draft 装配模型

def _two_signal_draft() -> Draft:
    return Draft.from_entries(
        "t", [
            {"fragment_id": "f1",
             "signal": {"type": "cross_above_series", "ref": "prev20_high",
                        "action": {"buy": "anchor", "pct_of_full": 100.0}}},
            {"fragment_id": "f2",
             "signal": {"action": {"sell": "clear_anchor"},
                        "conds": [{"type": "layers_held", "layer": "anchor"},
                                  {"type": "close_below_cost", "multiplier": 0.92}]}},
        ],
        series={"vol5": {"type": "rolling_mean", "source": "volume", "lookback": 5}},
    )


class TestDraftBuild:
    def test_canonical_form(self):
        d = _two_signal_draft()
        assert d.tree["accounts"]["full_allocation"] == 0.95   # 佣金/取整缓冲(见 contracts 注释)
        assert d.tree["layers"] == {"anchor": 100.0}
        # 平铺条件归一化为 conds 列表;signal id 代码生成
        assert d.signals()[0]["type"] == "and"
        assert d.signals()[0]["conds"][0]["type"] == "cross_above_series"
        assert d.fragment_of["sid_f1"] == "f1"
        assert d.indexes_of_fragment("f2") == [1]

    def test_bad_entries_raise(self):
        with pytest.raises(ValueError):
            Draft.from_entries("t", [{"signal": {"type": "and"}}])          # 缺 fid
        with pytest.raises(ValueError):
            Draft.from_entries("t", [{"fragment_id": "f1",
                                      "signal": {"action": {"sell": "clear_all"}}}])  # 无 type

    def test_conds_as_single_object_normalized(self):
        """真实模型把 conds 写成单对象(非列表)也要能装配(冒烟回归)。"""
        d = Draft.from_entries("t", [
            {"fragment_id": "f1", "signal": {
                "action": {"buy": "anchor", "pct_of_full": 100.0},
                "conds": {"type": "cross_above_series", "ref": "prev20_high"}}}])
        assert d.signals()[0]["conds"] == [
            {"type": "cross_above_series", "ref": "prev20_high"}]

    def test_conds_nested_and_object(self):
        d = Draft.from_entries("t", [
            {"fragment_id": "f2", "signal": {
                "action": {"sell": "clear_anchor"},
                "conds": {"type": "and", "conds": [
                    {"type": "layers_held", "layer": "anchor"},
                    {"type": "close_below_cost", "multiplier": 0.92}]}}}])
        assert d.signals()[0]["conds"][0]["type"] == "and"
        assert len(d.signals()[0]["conds"][0]["conds"]) == 2

    def test_roundtrip_dict(self):
        d = _two_signal_draft()
        d2 = Draft.from_dict(d.to_dict())
        assert d2.yaml_hash() == d.yaml_hash()
        assert d2.state_hash() == d.state_hash()
        assert d2.fragment_of == d.fragment_of

    def test_state_hash_tracks_mapping(self):
        d = _two_signal_draft()
        h0 = d.state_hash()
        d.fragment_of["sid_f1"] = "f9"          # 只动回链,不动树
        assert d.state_hash() != h0


class TestDraftRemap:
    def _az_f2_cond(self):
        return AuthzSet.build([_e(ErrCode.PARAM_INVALID, "signals[1].conds[1]")])

    def test_param_patch_authorized(self):
        d = _two_signal_draft()
        r = d.apply_remap("f2", {"mode": "param_patch", "cond_index": 1,
                                 "params": {"multiplier": 0.95}}, self._az_f2_cond())
        assert r.ok, r.error_codes()
        assert d.signals()[1]["conds"][1]["multiplier"] == 0.95
        assert r.diffs[0].path == "signals[1].conds[1].multiplier"
        assert r.diffs[0].origin == "fragment:f2"

    def test_param_patch_out_of_scope_zero_side_effect(self):
        d = _two_signal_draft()
        before = d.yaml_hash()
        r = d.apply_remap("f1", {"mode": "param_patch", "params": {"ref": "vol99"}},
                          self._az_f2_cond())
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED
        assert d.yaml_hash() == before           # 应用时拒绝=零副作用

    def test_cond_replace_and_insert(self):
        d = _two_signal_draft()
        az = AuthzSet.build([_e(ErrCode.UNKNOWN_COND, "signals[1].conds[1]"),
                             _e(ErrCode.MISSING_FRAGMENT, "fragment:f3")])
        r = d.apply_remap("f2", {"mode": "cond_replace", "cond_index": 1,
                                 "new_cond": {"type": "close_below_rolling",
                                              "ref": "low20_close"}}, az)
        assert r.ok and d.signals()[1]["conds"][1]["type"] == "close_below_rolling"
        r2 = d.apply_remap("f3", {"mode": "cond_replace",
                                  "new_cond": {"type": "hold_days_ge", "days": 20},
                                  "action": {"sell": "clear_anchor"}}, az)
        assert r2.ok and r2.diffs[0].op == "insert"
        assert d.fragment_of["sid_f3"] == "f3" and d.indexes_of_fragment("f3") == [2]
        # 缺失片段新建不带 action → 契约拒绝(在干净草稿上验)
        d2 = _two_signal_draft()
        r3 = d2.apply_remap("f3", {"mode": "cond_replace",
                                   "new_cond": {"type": "hold_days_ge", "days": 20}},
                            AuthzSet.build([_e(ErrCode.MISSING_FRAGMENT, "fragment:f3")]))
        assert not r3.ok and r3.errors[0].code == ErrCode.CONTRACT_INVALID
        assert len(d2.signals()) == 2

    def test_insert_without_fragment_authz_denied(self):
        d = _two_signal_draft()
        r = d.apply_remap("f7", {"mode": "cond_replace",
                                 "new_cond": {"type": "j_below", "threshold": 10},
                                 "action": {"buy": "anchor"}}, AuthzSet())
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED
        assert len(d.signals()) == 2

    def test_relink_pair_fix(self):
        # 构造贴错标签:孤儿 s_orphan 挂 fX(不存在),f1 缺映射
        d = _two_signal_draft()
        d.fragment_of["sid_f1"] = "fX"
        az = AuthzSet.build([_e(ErrCode.MISSING_FRAGMENT, "fragment:f1"),
                             _e(ErrCode.EXTRA_SIGNAL, "signals[0]")])
        r = d.apply_remap("f1", {"mode": "cond_replace", "adopt_signal_id": "sid_f1"}, az)
        assert r.ok, r.error_codes()
        assert d.indexes_of_fragment("f1") == [0] and "fX" not in d.fragments_mapped()

    def test_relink_denied_without_authz(self):
        d = _two_signal_draft()
        r = d.apply_remap("f1", {"mode": "cond_replace", "adopt_signal_id": "sid_f2"},
                          AuthzSet())
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED

    def test_action_on_existing_remap_rejected(self):
        d = _two_signal_draft()
        az = AuthzSet.build([_e(ErrCode.PARAM_INVALID, "signals[1]")])
        r = d.apply_remap("f2", {"mode": "cond_replace", "cond_index": 1,
                                 "new_cond": {"type": "j_below", "threshold": 5},
                                 "action": {"sell": "clear_all"}}, az)
        assert not r.ok and r.errors[0].code == ErrCode.CONTRACT_INVALID


class TestDraftPatchNode:
    def _d(self):
        return _two_signal_draft()

    def test_series_fix_via_related(self):
        d = self._d()
        az = AuthzSet.build([_e(ErrCode.REF_UNDEFINED, "signals[0].conds[0]",
                                ["series.vol5"])])
        r = d.apply_patch_node("series.vol5",
                               {"type": "rolling_mean", "source": "volume", "lookback": 10}, az)
        assert r.ok and d.tree["series"]["vol5"]["lookback"] == 10
        assert r.diffs[0].op == "replace"

    def test_out_of_scope_denied(self):
        d = self._d()
        az = AuthzSet.build([_e(ErrCode.REF_UNDEFINED, "signals[0].conds[0]")])
        before = d.to_dict()
        r = d.apply_patch_node("series.vol5", {"type": "junk"}, az)
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED
        assert d.to_dict() == before

    def test_signals_reorder(self):
        d = self._d()
        az = AuthzSet.build([_e(ErrCode.EXIT_BEFORE_ENTRY, "signals")])
        sigs = d.signals()
        r = d.apply_patch_node("signals", [sigs[1], sigs[0]], az)
        assert r.ok and r.diffs[0].op == "reorder"
        assert d.signals()[0]["id"] == "sid_f2"
        assert d.indexes_of_fragment("f2") == [0]

    def test_reorder_set_inequality_rejected(self):
        d = self._d()
        az = AuthzSet.build([_e(ErrCode.EXIT_BEFORE_ENTRY, "signals")])
        r = d.apply_patch_node("signals", [d.signals()[0]], az)   # 少一条=增删意图
        assert not r.ok and r.errors[0].code == ErrCode.CONTRACT_INVALID

    def test_fragment_pseudo_path_rejected(self):
        d = self._d()
        r = d.apply_patch_node("fragment:f1", {}, AuthzSet.build(
            [], granted=["fragment:f1"]))
        assert not r.ok and r.errors[0].code == ErrCode.CONTRACT_INVALID

    def test_value_none_deletes_node(self):
        d = self._d()
        d.tree.setdefault("series", {})["junk"] = {"type": "rolling_mean"}
        az = AuthzSet.build([_e(ErrCode.SERIES_DEF_INVALID, "series.junk")])
        r = d.apply_patch_node("series.junk", None, az)
        assert r.ok and r.diffs[0].op == "remove"
        assert "junk" not in d.tree["series"]

    def test_delete_missing_target_contract_invalid(self):
        d = self._d()
        az = AuthzSet.build([_e(ErrCode.SERIES_DEF_INVALID, "series.nope")])
        r = d.apply_patch_node("series.nope", None, az)
        assert not r.ok and r.errors[0].code == ErrCode.CONTRACT_INVALID

    def test_delete_out_of_scope_denied(self):
        d = self._d()
        before = d.to_dict()
        az = AuthzSet.build([_e(ErrCode.REF_UNDEFINED, "signals[0].conds[0]")])
        r = d.apply_patch_node("signals[0]", None, az)
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED
        assert d.to_dict() == before


class TestDraftDegradeTrack:
    """降级轨:整篇 YAML → 树 diff → 变更集 ⊄ 授权集 → reject 本轮回传。"""

    def test_within_scope_full_yaml_applied(self):
        d = _two_signal_draft()
        az = AuthzSet.build([_e(ErrCode.PARAM_INVALID, "signals[1].conds[1].multiplier")])
        mod = d.to_dict()["tree"]
        mod["signals"][1]["conds"][1]["multiplier"] = 0.85
        r = d.apply_full_yaml(mod, az)
        assert r.ok
        assert d.signals()[1]["conds"][1]["multiplier"] == 0.85
        assert any(x.path.endswith("multiplier") for x in r.diffs)

    def test_out_of_scope_full_yaml_rejected(self):
        d = _two_signal_draft()
        az = AuthzSet.build([_e(ErrCode.PARAM_INVALID, "signals[1].conds[1].multiplier")])
        mod = d.to_dict()["tree"]
        mod["accounts"]["full_cash"] = 999999        # 顺手改引擎参数=越界
        r = d.apply_full_yaml(mod, az)
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED
        assert d.tree["accounts"]["full_cash"] == 100000   # reject=零副作用

    def test_dropped_linkage_rejected(self):
        d = _two_signal_draft()
        mod = d.to_dict()["tree"]
        mod["signals"] = [mod["signals"][0]]         # 吞掉一条=隐式删映射
        r = d.apply_full_yaml(mod, AuthzSet.build([_e(ErrCode.EXIT_BEFORE_ENTRY, "signals")]))
        assert not r.ok and r.errors[0].code == ErrCode.SCOPE_DENIED


class TestTreeDiff:
    def test_granularity(self):
        a = {"signals": [{"id": "x", "conds": [{"t": 1}]}, {"id": "y"}]}
        b = {"signals": [{"id": "x", "conds": [{"t": 2}]}, {"id": "y"}]}
        ds = tree_diff(a, b)
        assert [d.path for d in ds] == ["signals[0].conds[0].t"]
        assert ds[0].op == "replace" and ds[0].before == "1" or ds[0].before == 1


# ---------------------------------------------------------------- 语义裁决准入

def _v(fid, dim, verdict, quote="", ypath="", src="semantic_verify"):
    return SemanticVerdict(fragment_id=fid, dimension=dim, verdict=verdict,
                           evidence=SemanticEvidence(quote=quote, yaml_path=ypath),
                           source=src)


class TestSemanticAdmission:
    def test_drift_requires_both_evidence(self):
        assert _v("f1", "direction", "drift", "缩量回调", "signals[2].conds[0]").is_admissible()
        assert not _v("f1", "direction", "drift", "", "signals[2]").is_admissible()
        assert not _v("f1", "direction", "drift", "原话", "").is_admissible()

    def test_ok_and_ambiguous_rules(self):
        assert _v("f1", "param", "ok").is_admissible()
        assert _v("f1", "combo", "ambiguous", "拿住不动", "signals[0]").is_admissible()
        assert not _v("", "param", "ok").is_admissible()          # 无主体无效

    def test_bad_enum_invalid(self):
        assert not _v("f1", "profitability", "drift", "q", "p").is_admissible()
        assert not _v("f1", "direction", "not_a_verdict").is_admissible()

    def test_filter_keeps_valid_only(self):
        vs = [_v("f1", "direction", "drift", "q", "p"), _v("f2", "direction", "drift")]
        assert [v.fragment_id for v in admissible_verdicts(vs)] == ["f1"]


# ---------------------------------------------------------------- 预算表(v6 账目)

class TestBudget:
    def test_free_track(self):
        b = budget_for("free")
        assert b is BUDGET_FREE
        assert b.happy_path_max == 10       # plan1+draft1+verify1+synth1+repair6
        assert b.worst_case_max == 13       # +meta1+replan1+redraft1
        assert b.draft_selfcheck == 0       # 免费档产源自查关
        assert b.repair_steps == 6 and b.repair_rounds == 3

    def test_paid_track(self):
        b = budget_for("paid")
        assert b is BUDGET_PAID
        assert b.happy_path_max == 17
        assert b.worst_case_max == 21
        assert b.draft_selfcheck == 1 and b.draft == 2 and b.draft_blind_eval == 2
        assert b.repair_steps == 9


# ---------------------------------------------------------------- 续流/落库契约

class TestRunRecordAndSpec:
    def test_intent_spec_roundtrip_with_hooks(self):
        spec = IntentSpec(session_id="s1", run_id="r1", revision=2,
                          symbol="159516", start="2023-01-01", end="2023-12-31",
                          fragments=[Fragment("f1", "突破20日高点买入"),
                                     Fragment("f2", "跌破成本8%止损", "stop",
                                              "inferred_default")])
        spec2 = IntentSpec.from_dict(spec.to_dict())
        assert spec2.session_id == "s1" and spec2.revision == 2
        assert spec2.fragments[1].origin == "inferred_default"
        assert len(spec2.semantic_fragments()) == 2

    def test_from_dict_ignores_unknown_keys(self):
        spec = IntentSpec.from_dict({"symbol": "x", "brand_new_field_p2": 1})
        assert spec.symbol == "x"

    def test_run_row_columns_aligned(self):
        rec = RunRecord(run_id="r1", session_id="s1", exit_status="READY")
        row = rec.to_row()
        assert row[0] == "r1" and len(row) == 22
        cols = ["run_id", "session_id", "parent_run_id", "spec_revision", "tier",
                "input_digest", "intent_json", "yaml_text", "metrics_json", "rounds",
                "steps", "llm_calls", "exit_status", "resume_from", "user_verdict",
                "semantic_fail_count", "scope_denied_count", "meta_decision", "error",
                "cost_json", "created_at", "updated_at"]
        back = RunRecord.from_row(dict(zip(cols, row)))
        assert back.exit_status == "READY"

    def test_run_table_ddl_compiles(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(RUN_TABLE_DDL)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nl2strat_run)")}
        assert {"session_id", "parent_run_id", "spec_revision", "resume_from",
                "user_verdict", "semantic_fail_count", "meta_decision"} <= cols
