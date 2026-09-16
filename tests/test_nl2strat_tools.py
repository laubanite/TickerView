# -*- coding: utf-8 -*-
"""tools.py 确定性关卡测试:reconcile 集合对账 / validate 错误喂修复质量 /
related_paths 引用图预计算 / judge 分级 / check_data / 真引擎冒烟。

§8 纪律:reconcile 与授权集注入面(related_paths)先写测试后写实现。
"""
from __future__ import annotations

import pytest

from alphaprism.nl2strat import tools
from alphaprism.nl2strat.contracts import Draft, ErrCode, Fragment, IntentSpec


def _spec(fids=("f1", "f2")) -> IntentSpec:
    return IntentSpec(
        run_id="pytest", symbol="159516", start="2023-01-01", end="2024-12-31",
        fragments=[Fragment(fid, f"语义{fid}") for fid in fids])


def _valid_draft() -> Draft:
    """P0 规范形最小合法草稿:突破买入 + 门控止损。"""
    return Draft.from_entries("ok", [
        {"fragment_id": "f1", "signal": {
            "type": "cross_above_series", "ref": "prev20_high",
            "action": {"buy": "anchor", "pct_of_full": 100.0}}},
        {"fragment_id": "f2", "signal": {
            "action": {"sell": "clear_anchor"},
            "conds": [{"type": "layers_held", "layer": "anchor"},
                      {"type": "close_below_cost", "multiplier": 0.92}]}},
    ])


def _codes(errs) -> list[str]:
    return [e.code for e in errs]


def _find(errs, code) -> list:
    return [e for e in errs if e.code == code]


# ---------------------------------------------------------------- reconcile

class TestReconcile:
    def test_equal_sets_pass(self):
        assert tools.reconcile(_spec(), _valid_draft()) == []

    def test_missing_fragment(self):
        d = _valid_draft()
        del d.tree["signals"][1]
        del d.fragment_of["sid_f2"]
        errs = tools.reconcile(_spec(), d)
        assert _codes(errs) == [ErrCode.MISSING_FRAGMENT]
        assert errs[0].where == "fragment:f2"

    def test_extra_signal(self):
        d = _valid_draft()
        d.fragment_of["sid_f2"] = "f9"      # 孤儿回链
        errs = tools.reconcile(_spec(), d)
        assert ErrCode.EXTRA_SIGNAL in _codes(errs)
        e = _find(errs, ErrCode.EXTRA_SIGNAL)[0]
        assert e.where == "signals[1]"

    def test_counts_equal_sets_not(self):
        # f2 缺失、f9 多余:计数相等集合不等——必须两错都出(对账基准是相等非计数)
        d = _valid_draft()
        d.fragment_of["sid_f2"] = "f9"
        spec = _spec(("f1", "f9x"))
        errs = tools.reconcile(spec, d)
        assert ErrCode.MISSING_FRAGMENT in _codes(errs)
        assert ErrCode.EXTRA_SIGNAL in _codes(errs)

    def test_inferred_default_counts_in_baseline(self):
        spec = _spec(("f1",))
        spec.fragments.append(Fragment("f2", "默认止损 8%", "stop", "inferred_default"))
        assert _codes(tools.reconcile(spec, _valid_draft())) == []   # 默认条目参与相等基准


# ---------------------------------------------------------------- validate

class TestValidateHappy:
    def test_minimal_valid(self):
        errs = tools.validate_yaml(_valid_draft(), _spec())
        assert errs == []

    def test_nested_or_valid(self):
        d = _valid_draft()
        d.tree["signals"][0]["conds"] = [
            {"type": "or", "conds": [
                {"type": "cross_above_series", "ref": "prev20_high"},
                {"type": "close_above_ma", "ref": "m_20"}]}]
        assert tools.validate_yaml(d, _spec()) == []


class TestValidateFeedsRepair:
    def test_unknown_cond_with_candidates(self):
        d = _valid_draft()
        d.tree["signals"][1]["conds"][1]["type"] = "close_under_ma"
        errs = tools.validate_yaml(d, _spec())
        e = _find(errs, ErrCode.UNKNOWN_COND)[0]
        assert e.where == "signals[1].conds[1]"
        assert "close_below_ma" in e.candidates      # 拼写近似给足

    def test_ref_undefined_related_series_definition(self):
        """kimi #3 场景:REF_UNDEFINED(vol5) 自动带出 series.vol5 定义处。"""
        d = _valid_draft()
        d.tree["series"]["vol5"] = {"type": "rolling_mean", "source": "volume",
                                    "lookback": 5}   # 撞内建名 → SHADOW(见下测)
        errs = tools.validate_yaml(d, _spec())
        assert _find(errs, ErrCode.SERIES_SHADOW)

        d2 = _valid_draft()
        d2.tree["signals"][0]["conds"][0]["ref"] = "vol55"
        errs = tools.validate_yaml(d2, _spec())
        e = _find(errs, ErrCode.REF_UNDEFINED)[0]
        assert e.where == "signals[0].conds[0]"
        assert "series.vol55" in e.related_paths     # 前向扩权边零试错
        assert "vol5" in e.candidates or "vol20" in e.candidates

    def test_series_def_error_brings_all_users(self):
        """反向边:series 定义错误 → related 带上引用它的全部 cond 路径。"""
        d = _valid_draft()
        d.tree["series"]["myvol"] = {"type": "rolling_avg", "source": "volume",
                                     "lookback": 5}
        d.tree["signals"][0]["conds"][0]["ref"] = "myvol"
        errs = tools.validate_yaml(d, _spec())
        e = _find(errs, ErrCode.SERIES_DEF_INVALID)[0]
        assert e.where.startswith("series.myvol")
        assert "signals[0].conds[0]" in e.related_paths

    def test_param_missing(self):
        d = _valid_draft()
        del d.tree["signals"][0]["conds"][0]["ref"]
        errs = tools.validate_yaml(d, _spec())
        e = _find(errs, ErrCode.PARAM_MISSING)[0]
        assert e.where == "signals[0].conds[0]" and "ref" in e.msg

    def test_param_invalid_type(self):
        d = _valid_draft()
        d.tree["signals"][1]["conds"][1]["multiplier"] = "0.92x"
        errs = tools.validate_yaml(d, _spec())
        assert _find(errs, ErrCode.PARAM_INVALID)

    def test_unknown_layer_action(self):
        d = _valid_draft()
        d.tree["signals"][0]["action"] = {"buy": "trial", "pct_of_full": 10}
        errs = tools.validate_yaml(d, _spec())
        e = _find(errs, ErrCode.UNKNOWN_ACTION)[0]
        assert "anchor" in e.candidates

    def test_closed_multilayer_keys(self):
        d = _valid_draft()
        d.tree["signals"][0]["halt_add"] = True
        d.tree["signals"][0]["action"]["shared_cap"] = "main_cap"
        errs = tools.validate_yaml(d, _spec())
        assert len(_find(errs, ErrCode.P0_SUBSET_VIOLATION)) >= 2

    def test_exit_ungated_flagged(self):
        """退出先于持仓成立:无门控 sell 在空仓吞信号 → 指路 layers_held。"""
        d = _valid_draft()
        d.tree["signals"][1]["conds"] = [{"type": "close_below_cost", "multiplier": 0.92}]
        errs = tools.validate_yaml(d, _spec())
        e = _find(errs, ErrCode.EXIT_BEFORE_ENTRY)[0]
        assert e.where == "signals[1]" and "layers_held" in str(e.candidates)

    def test_signal_id_ref_undefined(self):
        d = _valid_draft()
        d.tree["signals"][1]["conds"][1] = {"type": "days_since", "since": "right_initial",
                                            "ge": 20}
        errs = tools.validate_yaml(d, _spec())
        e = _find(errs, ErrCode.SIGNAL_ID_REF_UNDEFINED)[0]
        assert "sid_f1" in e.candidates     # 真 id 近似给足

    def test_no_entry_signal(self):
        d = _valid_draft()
        d.tree["signals"][0]["action"] = {"sell": "clear_anchor"}
        errs = tools.validate_yaml(d, _spec())
        assert _find(errs, ErrCode.SCHEMA_VIOLATION)

    def test_duplicate_ids(self):
        d = _valid_draft()
        d.tree["signals"][1]["id"] = "sid_f1"
        errs = tools.validate_yaml(d, _spec())
        assert _find(errs, ErrCode.DUPLICATE_SIGNAL_ID)

    def test_accounts_lock(self):
        d = _valid_draft()
        d.tree["accounts"]["commission_rate"] = 0.0
        errs = tools.validate_yaml(d, _spec())
        assert _find(errs, ErrCode.P0_SUBSET_VIOLATION)

    def test_layers_wrong_name(self):
        d = _valid_draft()
        d.tree["signals"][1]["conds"][0]["layer"] = "main"
        errs = tools.validate_yaml(d, _spec())
        assert _find(errs, ErrCode.P0_SUBSET_VIOLATION)

    def test_empty_and_nested_too_deep(self):
        d = _valid_draft()
        d.tree["signals"][0]["conds"] = [{"type": "and", "conds": []}]
        assert _find(tools.validate_yaml(d, _spec()), ErrCode.EMPTY_CONDS)
        d2 = _valid_draft()
        d2.tree["signals"][0]["conds"] = [{"type": "and", "conds": [
            {"type": "and", "conds": [
                {"type": "and", "conds": [
                    {"type": "j_below", "threshold": 1}]}]}]}]
        assert _find(tools.validate_yaml(d2, _spec()), ErrCode.SCHEMA_VIOLATION)


# ---------------------------------------------------------------- judge

class TestJudge:
    def _eq(self, n=250, start_year=2023, slope=1.0):
        """n 根日历日线、单调日期、净值随斜率波动(避免 FLAT_EQUITY 误触)。"""
        from datetime import date, timedelta
        d0 = date(start_year, 1, 2)
        out = []
        for i in range(n):
            dt = d0 + timedelta(days=i)
            out.append({"date": dt.isoformat(), "equity": 100000 + i * slope})
        return out

    def test_no_trades_hard(self):
        errs = tools.judge({"trades": [], "equity": self._eq()}, _spec())
        assert _codes(errs) == [ErrCode.NO_TRADES]

    def test_flat_equity_hard(self):
        t = [{"side": "BUY"}, {"side": "SELL"}]
        errs = tools.judge({"trades": t, "equity": self._eq(250, slope=0.0)}, _spec())
        assert ErrCode.FLAT_EQUITY in _codes(errs)

    def test_suspect_turnover_without_hint(self):
        t = [{"side": "BUY"}] * 600          # 600 笔 / 1 年 = 600 笔/年
        errs = tools.judge({"trades": t, "equity": self._eq(365)}, _spec())
        assert _codes(errs) == [ErrCode.SUSPECT_TURNOVER]

    def test_high_freq_hint_exempts(self):
        spec = _spec()
        spec.fragments[0].text = "做短线高频轮动"
        t = [{"side": "BUY"}] * 600
        assert tools.judge({"trades": t, "equity": self._eq(365)}, spec) == []

    def test_long_word_mismatch(self):
        spec = _spec()
        spec.fragments[0].text = "突破买入拿住不动"
        t = [{"side": "BUY"}] * 200          # 200 笔/年:>52 但 <250
        errs = tools.judge({"trades": t, "equity": self._eq(365)}, spec)
        assert _codes(errs) == [ErrCode.SUSPECT_FREQ_MISMATCH]

    def test_normal_run_passes(self):
        t = [{"side": "BUY"}] * 6
        eq = self._eq(365)
        assert tools.judge({"trades": t, "equity": eq}, _spec()) == []


# ---------------------------------------------------------------- check_data / dsl_card

class TestObservationTools:
    def test_check_data_ok_and_missing(self):
        r = tools.check_data("159516", "2023-01-01", "2023-12-31")
        assert r["ok"] and r["window_bars"] > 100
        r2 = tools.check_data("999999")
        assert not r2["ok"] and r2["error"] == ErrCode.DATA_MISSING

    def test_dsl_card_search(self):
        assert "close_below_cost" in tools.dsl_card("止损")

    def test_engine_error_where_parse(self):
        d = _valid_draft()
        assert tools._where_from_engine_msg("未知条件类型: foo", d) == "signals"


# ---------------------------------------------------------------- 真引擎冒烟(确定性零 LLM)

class TestEngineGateSmoke:
    def test_valid_draft_runs(self):
        """P0 规范形(anchor 单层满配)在真引擎上跑通且有成交。"""
        spec = _spec()
        result, errs = tools.run_backtest_gate(_valid_draft(), spec)
        assert errs == [], [e.to_dict() for e in errs]
        assert result and result["metrics"]["trade_count"] >= 1

    def test_engine_error_fed_back(self):
        d = _valid_draft()
        d.tree["signals"][0]["conds"][0]["ref"] = "nope99"     # validate 会拦;绕过直接跑
        spec = _spec()
        result, errs = tools.run_backtest_gate(d, spec)
        # 引擎对缺失 ref 返回 None 视为 False(显式失败在条件求值)→ 零成交硬伤
        assert errs and errs[0].code in (ErrCode.NO_TRADES, ErrCode.ENGINE_ERROR)
