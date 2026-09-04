# -*- coding: utf-8 -*-
"""T1 盘前市场状态标签(2026-09,docs/盘前方案.md v3.3)回归。

覆盖:关键词命中/否定排除、极性定级(利好不进灯)、组合/标的小灯、文案。
"""
from alphaprism.planner.prestate import (
    STATE_TEXT, match_text, compose_state, _tag_for)


def test_match_and_negation():
    # 立案 → 利空·高危
    hits = match_text("证监会决定对某公司立案调查")
    assert any(h["polarity"] == "利空" and h["severity"] == "high" for h in hits)
    # 否定排除:取消减持 → 股东减持不命中
    assert all(h["category"] != "股东减持"
               for h in match_text("股东取消减持计划"))
    assert any(h["category"] == "股东减持"
               for h in match_text("大股东拟减持不超过2%"))


def test_tag_for():
    assert "利空·高危" in _tag_for("证监会立案调查报告")
    assert "利空·中度" in _tag_for("美国拟加征关税")
    assert _tag_for("财政部下发大基金二期补贴") == "利好"
    assert _tag_for("今日天气不错") == ""


def test_tier_green_on_only_good():
    items = [{"text": "财政部给予半导体行业财政补贴", "source_grade": "官方"}]
    st = compose_state(items, [])
    assert st["level"] == "green"
    assert st["name"] == "今日正常"
    assert st["bonus"]["利好"] == 1


def test_tier_yellow_medium():
    items = [{"text": "美国拟对华加征关税", "source_grade": "媒体"}]
    st = compose_state(items, [])
    assert st["level"] == "yellow"
    assert st["name"] == "今日谨慎"
    assert st["hint"] == STATE_TEXT["yellow"]["hint"]


def test_tier_red_high_and_priority():
    items = [{"text": "补贴利好落地", "source_grade": "官方"},
             {"text": "证监会立案调查某光伏龙头", "source_grade": "官方"}]
    st = compose_state(items, [])
    assert st["level"] == "red"          # 高危优先,利好不抵消
    assert st["evidence"][0].startswith("利空·高危")
    # 同日中度取严:高危存在时仍是红
    items2 = [{"text": "大股东拟减持", "source_grade": "快讯"},
              {"text": "加征关税提高", "source_grade": "快讯"}]
    assert compose_state(items2, [])["level"] == "red"


def test_rumour_excluded():
    items = [{"text": "传闻某公司将被立案", "source_grade": "传闻"}]
    assert compose_state(items, [])["level"] == "green"


def test_per_symbol_lights():
    watch = [{"symbol": "515790", "name": "光伏ETF华泰柏瑞"},
             {"symbol": "159516", "name": "半导体设备ETF国泰"},
             {"symbol": "518850", "name": "黄金ETF华夏"}]
    items = [{"text": "美国拟对光伏产品加征关税", "source_grade": "媒体", "sector": "光伏"},
             {"text": "证监会立案调查某半导体公司", "source_grade": "官方", "sector": "半导体"}]
    st = compose_state(items, watch)
    levels = {p["symbol"]: p["level"] for p in st["per_symbol"]}
    assert levels["515790"] == "yellow"      # 光伏被关税(中度)波及
    assert levels["159516"] == "red"         # 半导体被立案(高危)波及
    assert levels["518850"] == "green"       # 黄金无命中


def test_sentence_boundary_snap():
    from alphaprism.planner.prestate import _snap
    assert _snap("一二三。四五六", 5) == "一二三。…"
    assert _snap("短句", 10) == "短句"


def test_morning_reason_snap_no_midsentence():
    from alphaprism.morning import _snap_sentence
    s = "因为资本开支大增,利润不及预期但对上游是利好;继续观察。" * 3
    out = _snap_sentence(s, 40)
    assert "…" in out or len(out) <= 40
    assert not out.endswith(("是", "期", "利", "察"))   # 不以句中字结尾