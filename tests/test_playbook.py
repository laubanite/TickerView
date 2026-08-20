"""盘前生成回归测试(playbook.py,里程碑2)。

- _fallback_rows:LLM 降级确定性模板
- format_draft:草稿 markdown 结构(与手写盘前条目一致)
- append_to_journal:按日期/时段追加/替换「每日盯盘记录」逻辑(临时文件)
"""
from __future__ import annotations

from datetime import datetime

from alphaprism.planner.playbook import _fallback_rows, append_to_journal, format_draft
from alphaprism.planner.rulemodel import Global, MarketGate, RuleModel


def _model() -> RuleModel:
    m = RuleModel()
    m.global_ = Global(market_gate=MarketGate(conclusion="大盘破 MA60,等 J<60 前不加仓"))
    m.global_.discipline = ["J 值>85 不追高", "回踩买、突破买,绝不追买"]
    return m


def _draft() -> dict:
    return {
        "date": "2026-08-21",
        "summary": "隔夜美股反弹,化工消息面最顺",
        "rows": [
            {"code": "516020", "name": "化工ETF华宝",
             "overnight": "石化利润+54.9%,焦炭提涨", "action": "回踩 0.855-0.860 缩量企稳→加仓①"},
            {"code": "159516", "name": "半导体设备ETF国泰",
             "overnight": "无新增消息", "action": "低开 0.72-0.75 缩量企稳→加仓①;破 0.72→放弃"},
        ],
    }


def _draft_md() -> str:
    return format_draft(_draft(), _model(), now=datetime(2026, 8, 21, 8, 55))


def test_fallback_rows_deterministic():
    """降级模板:引可计算规则条件 + 新闻摘要,不编造。"""
    rows = [{
        "code": "516020", "name": "化工ETF华宝", "sector": "化工",
        "news": [{"time": "2026-08-21 07:00:00", "text": "石化行业利润大增"}],
        "anomaly_count": 0,
        "tech": {},
        "levels": [],
        "rules": [
            {"action": "加仓①", "condition": "回踩 0.855-0.860 缩量企稳",
             "operation": "加第一批(预算 25%)", "computable": True},
            {"action": "减仓红线", "condition": "放量跌破 0.849",
             "operation": "减 1/3-1/2", "computable": False},
        ],
    }]
    out = _fallback_rows(rows)
    assert out[0]["overnight"] == "石化行业利润大增"
    assert "回踩 0.855-0.860 缩量企稳" in out[0]["action"]
    assert "放量跌破 0.849" not in out[0]["action"]  # 不可计算条件不引入规则动作


def test_format_draft_structure():
    """草稿 markdown 含标题/剧本表/纪律。"""
    md = format_draft(_draft(), _model(), now=datetime(2026, 8, 21, 8, 55))
    assert md.startswith("### 📅 2026-08-21(五)盘前 08:55(剧本草稿·待确认)")
    assert "今日剧本修正(草稿)" in md
    assert "| 化工ETF华宝 516020 |" in md
    assert "回踩 0.855-0.860 缩量企稳→加仓①" in md
    assert "今日纪律(沿用 §六)" in md


def test_append_new_date(tmp_path):
    """无该日期条目 → 追加到盯盘记录段落末尾。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 每日盯盘记录\n\n### 📅 2026-08-20(四)盘前 9:15\n\n旧条目\n",
                 encoding="utf-8")
    append_to_journal(_draft_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert "### 📅 2026-08-21(五)盘前 08:55" in text
    assert "### 📅 2026-08-20(四)盘前 9:15" in text
    assert text.index("2026-08-20") < text.index("2026-08-21")


def test_append_replace_same_date_playbook(tmp_path):
    """已有同日期盘前草稿 → 替换,不重复追加。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 每日盯盘记录\n\n"
                 "### 📅 2026-08-21(五)盘前 8:00(剧本草稿·待确认)\n\n旧草稿\n",
                 encoding="utf-8")
    append_to_journal(_draft_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert text.count("2026-08-21(五)盘前") == 1
    assert "旧草稿" not in text
    assert "回踩 0.855-0.860" in text


def test_append_playbook_before_close(tmp_path):
    """同日已有盘后 → 盘前块插入到盘后之前。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 每日盯盘记录\n\n"
                 "### 📅 2026-08-21(五)盘后 18:00\n\n收盘对账\n",
                 encoding="utf-8")
    append_to_journal(_draft_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert "盘前 08:55" in text
    assert "盘后 18:00" in text
    assert text.index("盘前 08:55") < text.index("盘后 18:00")


def test_append_no_section(tmp_path):
    """无盯盘记录段落 → 自动创建。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 一、大盘环境\n\n内容\n", encoding="utf-8")
    append_to_journal(_draft_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert "每日盯盘记录" in text
    assert "### 📅 2026-08-21(五)盘前 08:55" in text