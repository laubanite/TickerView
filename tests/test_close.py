"""盘后生成回归测试(close.py,里程碑4)。

- _format_review:收盘对账 markdown 结构
- append_to_journal:追加/替换"每日盯盘记录"逻辑(用临时文件,不碰真实作战地图)
"""
from __future__ import annotations

from datetime import datetime

import pytest

from alphaprism.planner.close import _format_review, append_to_journal


def _review_md(date_str="2026-08-20"):
    return (f"### 📅 {date_str}(四)盘后 21:00\n\n"
            "**收盘对账**:\n- 大盘:上证收盘 3903.72 (+0.24%),门控 J=-0.08\n\n"
            "| 标的 | 收盘 | 涨跌 | 结论 |\n|---|---|---|---|\n"
            "| 化工 | 0.862 | -0.12% | 平静 |\n\n**复盘五问**:\n1. 大盘环境如何?")


def test_format_review_structure():
    """对账 markdown 含标题/对账表/复盘五问。"""
    md = _format_review(
        [{"name": "化工", "code": "516020", "close": 0.862, "change_pct": -0.12,
          "conclusion": "平静", "near": ""}],
        {"close": 3903.72, "change_pct": 0.24, "j": -0.08},
        "1. 大盘环境如何?", now=datetime(2026, 8, 20, 21, 0),
    )
    assert md.startswith("### 📅 2026-08-20")
    assert "收盘对账" in md
    assert "复盘五问" in md
    assert "| 化工 | 0.862 | -0.12% | 平静 |" in md


def test_append_to_journal_new_date(tmp_path):
    """无该日期条目 → 追加到盯盘记录段落末尾。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 每日盯盘记录\n\n### 📅 2026-08-19(三)盘前 9:15\n\n旧条目\n",
                 encoding="utf-8")
    append_to_journal(_review_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert "### 📅 2026-08-20(四)盘后 21:00" in text   # 新日期已追加
    assert "### 📅 2026-08-19(三)盘前 9:15" in text     # 旧条目保留
    # 新条目在旧条目之后
    assert text.index("2026-08-19") < text.index("2026-08-20")


def test_append_to_journal_replace_same_date(tmp_path):
    """已有同日期盘后条目 → 替换,不重复追加。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 每日盯盘记录\n\n"
                 "### 📅 2026-08-20(四)盘后 18:00\n\n旧盘后\n"
                 "### 📅 2026-08-20(四)盘前 9:15\n\n旧盘前\n",
                 encoding="utf-8")
    append_to_journal(_review_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert text.count("2026-08-20(四)盘后") == 1        # 不重复
    assert "旧盘后" not in text                          # 旧盘后被替换
    assert "旧盘前" in text                              # 盘前保留
    assert "1. 大盘环境如何?" in text                    # 新内容写入


def test_append_to_journal_no_section(tmp_path):
    """无盯盘记录段落 → 自动创建段落。"""
    f = tmp_path / "map.md"
    f.write_text("# 作战地图\n\n## 一、大盘环境\n\n内容\n", encoding="utf-8")
    append_to_journal(_review_md(), str(f))
    text = f.read_text(encoding="utf-8")
    assert "每日盯盘记录" in text
    assert "### 📅 2026-08-20(四)盘后 21:00" in text
