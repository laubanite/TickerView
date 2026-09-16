# -*- coding: utf-8 -*-
"""vocabulary.py 测试:注册表漂移守卫 + 参数派生正确性 + 检索面。"""
from __future__ import annotations

from alphaprism.nl2strat import vocabulary as vocab
from backtest_engine.core.strategy_v2 import _CONDS


class TestDriftGuard:
    def test_every_registered_cond_has_card(self):
        v = vocab.get_vocabulary()
        assert set(v.conds) == set(_CONDS), "词汇卡与 _CONDS 注册表漂移"

    def test_new_cond_auto_appears_without_semantic_blurb(self):
        # 注册表加新条件 → 卡片仍自动生成(说明可缺,参数不缺)——防"文档漂移"的机制面
        v = vocab.build_vocabulary()
        assert all(v.conds[k].required is not None for k in _CONDS)

    def test_series_covers_engine_builtins(self):
        v = vocab.get_vocabulary()
        for name in ("m_20", "prev20_high", "vol20", "vol5", "low20_close",
                     "low250_close", "peak120", "prev_close", "atr20_pct",
                     "kdj_k", "kdj_j", "adx14", "rs20"):
            assert name in v.series, name


class TestParamDerivation:
    def test_required_from_subscript(self):
        v = vocab.get_vocabulary()
        assert v.conds["vol_ratio_above"].required == ["ref", "threshold"]
        assert v.conds["close_below_cost"].required == []
        assert v.conds["close_below_cost"].optional.get("multiplier") == 0.92
        assert "layer" in v.conds["layers_held"].required
        assert "layers" in v.conds["layers_any"].optional
        assert "le" in v.conds["days_since"].optional        # `"le" in cond` 检出
        assert v.conds["days_since"].required == ["since", "ge"]
        assert v.conds["and"].required == ["conds"]
        assert v.conds["recovery_pct"].required == ["pct"]

    def test_ref_flag_and_hints(self):
        v = vocab.get_vocabulary()
        assert v.conds["drawdown_above"].takes_ref
        assert not v.conds["pct_drop"].takes_ref
        assert "multiplier" in v.conds["close_below_cost"].params_hint


class TestSearch:
    def test_query_by_semantic_word(self):
        v = vocab.get_vocabulary()
        txt = v.search("止损")
        assert "close_below_cost" in txt

    def test_query_by_name_prefix(self):
        v = vocab.get_vocabulary()
        assert "vol_ratio_below" in v.search("vol_ratio")

    def test_no_hit_rejects_hallucination(self):
        v = vocab.get_vocabulary()
        txt = v.search("xyzzy_not_a_cond")
        assert "无命中" in txt

    def test_unsupported_list(self):
        v = vocab.get_vocabulary()
        assert any("ROE" in u.term for u in v.unsupported)
        assert any(u.nearest for u in v.unsupported)


class TestP0Subset:
    def test_closed_keys(self):
        v = vocab.get_vocabulary()
        assert v.buy_layers == ("anchor",)                 # 单层规范形(§7#2)
        assert "clear_anchor" in v.sell_actions
        assert "halt_add" in v.closed_signal_keys
        assert "shared_cap" in v.closed_action_keys

    def test_indicator_cards_signature_params(self):
        v = vocab.get_vocabulary()
        assert "period" in v.indicators["sma"].params
        assert set(("adx", "stoch_kd")) <= set(v.indicators)

    def test_cache_singleton(self):
        assert vocab.get_vocabulary() is vocab.get_vocabulary()
