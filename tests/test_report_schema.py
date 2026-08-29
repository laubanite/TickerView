"""结论卡 schema 回归(2026-08-27,结论层结构化)。"""
from __future__ import annotations

from alphaprism.planner.report_schema import (
    fallback_conclusion, locate_conclusion, normalize_conclusion,
    program_priority_sentence, render_conclusion_card, validate_conclusion,
)

GOOD = (
    "## 十、资金面\n份额 -34.42%。\n"
    '{"conclusion":{"one_sentence":"日线超卖积蓄反弹动能,但30分KDJ超买过热压制短线,'
    '反弹空间有限,以观察为主","signal_type":"观望","time_sensitivity":"当日收盘",'
    '"priority":"日线MACD定方向、30分KDJ定节奏;零下空头环境30分超买=回调压力",'
    '"trigger":{"verb":"回踩","price_level":"0.836-0.837","confirm":"企稳信号",'
    '"action":"试多候选"},"invalidation":{"verb":"跌破","price_level":"0.836",'
    '"source":"回踩带下沿","action":"减仓"}}}\n## 建议类别: 观望'
)

SNAP = ("## 标的\n| 分时 | 现价 | 0.838 (-0.48%) |\n"
        "| 日线 | 均线 | M5=0.845 M20=0.851 |\n"
        "| 30分 | 均线 | M5=0.837 M10=0.836 M20=0.840 |\n"
        "| 盘口 | 五档 | 买4 0.835/1.22万手 |\n"
        "## 盘面状态\n| 操作参数 | 突破加仓位 0.894(近20日上沿) / 回踩 0.836-0.837 / "
        "减仓 跌破0.836 / 清仓止损 0.740 |")
ANCHORS = {"anchor_price": 0.838, "breakout_add": 0.894, "pullback_add": 0.837,
           "cut_loss": 0.836, "stop_loss": 0.740}


def test_normalize_good_and_fenced():
    c = normalize_conclusion(GOOD)
    assert c and c["signal_type"] == "观望"
    assert c["trigger"]["verb"] == "回踩" and c["invalidation"]["source"] == "回踩带下沿"
    # ```json 代码块包裹也能解析
    json_part = GOOD[GOOD.index('{"conclusion"'):]
    fenced = "## 十、资金面\n份额 -34.42%。\n```json\n" + json_part + "\n```\n## 建议类别: 观望"
    c2 = normalize_conclusion(fenced)
    assert c2 and c2["one_sentence"].startswith("日线超卖")
    assert c2["signal_type"] == "观望"


def test_normalize_none():
    assert normalize_conclusion("## 八、操作建议\n观望。\n## 建议类别: 观望") is None
    assert normalize_conclusion("") is None


def test_validate_ok_and_errors():
    ok, issues = validate_conclusion(normalize_conclusion(GOOD), SNAP, ANCHORS, 0.838)
    assert ok is True and issues == []
    # 越界 signal_type + 白名单外价位 + 动词方向矛盾(跌破 0.850:下跌动词却指向现价上方)
    bad = normalize_conclusion(
        '{"conclusion":{"one_sentence":"结论一句话要足够长且不复制状态词才行啊",'
        '"signal_type":"梭哈","time_sensitivity":"任意后","priority":"日线MACD定方向",'
        '"trigger":{"verb":"跌破","price_level":"0.850","confirm":"企稳信号",'
        '"action":"加仓候选"},"invalidation":{"verb":"跌破","price_level":"0.835",'
        '"source":"买4挂单","action":"减仓"}}}')
    ok2, issues2 = validate_conclusion(bad, SNAP, ANCHORS, 0.838)
    assert ok2 is False
    joined = "; ".join(issues2)
    assert "signal_type 越界" in joined and "time_sensitivity 越界" in joined
    assert "trigger.price_level 不在白名单" in joined      # 0.850 非锚点∪关键位
    assert "invalidation.price_level 不在白名单" in joined  # 0.835 是盘口价
    assert "动词方向与现价矛盾" in joined                    # 站上 0.850 < 现价
    # v2.10:action 越界("观望"作 action=弱结论)与两位小数 price_level 校验
    bad2 = normalize_conclusion(
        '{"conclusion":{"one_sentence":"结论一句话要足够长且不复制状态词才行啊",'
        '"signal_type":"观望","time_sensitivity":"盘中","priority":"日线MACD定方向",'
        '"trigger":{"verb":"回踩","price_level":"0.84","confirm":"企稳信号",'
        '"action":"观望"},"invalidation":{"verb":"跌破","price_level":"0.836",'
        '"source":"回踩带下沿","action":"减仓"}}}')
    _, issues3 = validate_conclusion(bad2, SNAP, ANCHORS, 0.838)
    j3 = "; ".join(issues3)
    assert "trigger.price_level 必须 3 位小数" in j3       # 0.84 → 缺三位小数
    assert "trigger.action 越界" in j3                     # 观望 作 action → 拒
    # 区间内非三位小数(0.822-0.84 → 0.84)也拦
    bad3 = normalize_conclusion(
        '{"conclusion":{"one_sentence":"足够长的一句结论判断而不是复述状态啊",'
        '"signal_type":"减仓参考","time_sensitivity":"盘中","priority":"日线MACD定方向",'
        '"trigger":{"verb":"回踩","price_level":"0.822-0.84","confirm":"企稳信号",'
        '"action":"减仓"},"invalidation":{"verb":"跌破","price_level":"0.822",'
        '"source":"回踩带下沿","action":"减仓"}}}')
    _, issues4 = validate_conclusion(bad3, SNAP, ANCHORS, 0.838)
    assert "trigger.price_level 含非 3 位小数" in "; ".join(issues4)


def test_locate_and_strip():
    loc = locate_conclusion(GOOD)
    assert loc is not None
    head, tail = GOOD[:loc[0]], GOOD[loc[1]:]
    assert '{"conclusion"' not in head + tail
    assert "one_sentence" not in head + tail
    assert normalize_conclusion(head + tail) is None   # 剥离后无结论卡


def test_fallback_and_render():
    state = {"state_word": "左侧观望", "scenario": "趋势"}
    anchors = {"anchor_price": 0.838, "breakout_add": 0.894,
               "pullback_zone": {"lower": 0.836, "upper": 0.837},
               "cut_loss": 0.836, "cut_src": "回踩带下沿",
               "stop_loss": 0.740, "stop_src": "阶段低"}
    c = fallback_conclusion(state, anchors)
    assert c["signal_type"] == "观望"
    assert c["trigger"]["verb"] == "回踩" and c["trigger"]["price_level"] == "0.836-0.837"
    assert c["invalidation"]["action"] == "减仓"
    md = render_conclusion_card(c)
    assert "核心判断" in md and "信号类型" in md and "触发" in md and "失效" in md
    # 兜底结论卡可被校验(价位都在白名单/锚点)
    snap = ("## 标的\n| 分时 | 现价 | 0.838 |\n| 30分 | 均线 | M5=0.837 M10=0.836 |\n"
            "## 盘面状态\n| 操作参数 | 回踩 0.836-0.837 / 减仓 跌破0.836 / 清仓止损 0.740 |")
    ok, _ = validate_conclusion(c, snap, anchors, 0.838)
    assert ok is True


def test_generate_conclusion_card_llm_and_fallback():
    """卡先行:LLM 卡校验过 → source=llm;垃圾输出/失败 → source=fallback。"""
    from alphaprism.planner.report_schema import generate_conclusion_card

    anchors = {"anchor_price": 0.838, "breakout_add": 0.894,
               "pullback_zone": {"lower": 0.836, "upper": 0.837},
               "cut_loss": 0.836, "stop_loss": 0.740}
    state = {"state_word": "左侧观望"}
    snap = ("## 标的\n| 分时 | 现价 | 0.838 |\n| 30分 | 均线 | M5=0.837 M10=0.836 |\n"
            "## 盘面状态\n| 操作参数 | 突破 0.894 / 回踩 0.836-0.837 / 减仓 0.836 / 清仓 0.740 |")

    def good_chat(messages, cfg, **kw):
        return ('{"conclusion":{"one_sentence":"日线超卖但30分超买过热,反弹空间有限,以观察为主",'
                '"signal_type":"观望","time_sensitivity":"当日收盘","priority":"日线MACD定方向、'
                '30分KDJ定节奏",'
                '"trigger":{"verb":"回踩","price_level":"0.836-0.837","confirm":"企稳信号",'
                '"action":"试多候选"},"invalidation":{"verb":"跌破","price_level":"0.836",'
                '"source":"回踩带下沿","action":"减仓"}}}')
    card, src = generate_conclusion_card(snap, anchors, state, None, chat_call=good_chat)
    assert src == "llm" and card["signal_type"] == "观望"

    def bad_chat(messages, cfg, **kw):
        return "我不知道,随便写写"
    card2, src2 = generate_conclusion_card(snap, anchors, state, None, chat_call=bad_chat)
    assert src2 == "fallback" and card2["signal_type"] == "观望"


def test_program_priority_sentence():
    """程序多周期优先级句:从快照标注解析(零下+30分超买 → 空头环境细分句)。"""
    snap = ("## 标的\n"
            "| 日线 | MACD | DIF=-0.008 DEA=-0.01 柱=0.003(红柱·柱缩小·零下) |\n"
            "| 30分 | KDJ | K=69.0 D=57.33 J=92.35(超买·多排·J上翘·超买过热·回调风险) |\n"
            "## 大盘\n| 日线 | 均线 | M5=3894.958 |")
    s = program_priority_sentence(snap)
    assert "零下(空头环境)" in s and "30分KDJ定节奏:超买" in s
    assert "30分 超买=回调压力" in s
    # 零上 + 数据缺失 fallback
    snap2 = ("## 标的\n| 日线 | MACD | DIF=0.05 DEA=0.02 柱=0.01(红柱·柱扩大·零上) |\n"
             "| 30分 | KDJ | K=30.0 D=35.0 J=28.0(中性·空排·三线收敛) |\n## 大盘\n| 日线 | 均线 | - |")
    s2 = program_priority_sentence(snap2)
    assert "零上(多头环境)" in s2 and "30分 超卖=低吸机会" in s2
    s3 = program_priority_sentence("## 标的\n无 MACD/KDJ 行\n## 大盘\nx")
    assert "日线MACD定方向、30分KDJ定节奏" in s3