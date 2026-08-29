"""盘后增量1/增量4(纯规则,内存库测试,不联网、无文件残留)。"""
from __future__ import annotations

import sqlite3

from alphaprism.db import _SCHEMA


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _seed(conn, closes, anchor=0.850, state_word="右侧初现"):
    """插入一条右侧初现存档 + 模拟日K(索引0为突破日,其后为窗口日)。"""
    t0 = "2026-08-10"
    for i, c in enumerate(closes):
        d = t0 if i == 0 else f"2026-08-1{i}"
        conn.execute(
            "INSERT OR REPLACE INTO etf_kline_daily (symbol, trade_date, open, high,"
            " low, close, volume) VALUES (?,?,?,?,?,?,?)",
            ("515790", d, c, c + 0.01, c - 0.01, c, 100))
    conn.execute(
        "INSERT INTO advice_archive (trade_date, symbol, created_at, anchor_price,"
        " scenario, state_word, risk_level, category, advice_md, snapshot_md, degraded)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (t0, "515790", "22:12", anchor, "趋势", state_word, "正常", "观望", "md", "snap"))
    conn.commit()


def test_confirm_rally():
    from alphaprism.planner.intraday_confirm import _confirm_one

    conn = _mk_db()
    _seed(conn, [0.850, 0.855, 0.860, 0.858, 0.862, 0.865])   # 其后 5 日全部站稳
    arch = dict(conn.execute("SELECT * FROM advice_archive").fetchone())
    assert _confirm_one(conn, arch) == "右侧确认"


def test_confirm_breakdown():
    from alphaprism.planner.intraday_confirm import _confirm_one

    conn = _mk_db()
    _seed(conn, [0.850, 0.840, 0.845, 0.848, 0.850, 0.852])   # 首日即跌破
    arch = dict(conn.execute("SELECT * FROM advice_archive").fetchone())
    assert _confirm_one(conn, arch) == "证伪"


def test_verify_direction():
    """增量4 判据:减仓类跌超 5% → 应验;持有 → 无法判定;加仓类跌 → 未应验。"""
    from alphaprism.planner.advice_verify import _verify_one

    conn = _mk_db()
    conn.execute(
        "INSERT INTO etf_kline_daily (symbol, trade_date, open, high, low, close,"
        " volume) VALUES (?,?,?,?,?,?,?)",
        ("515790", "2026-08-11", 1.00, 1.02, 0.98, 0.90, 100))
    conn.commit()
    base = {"symbol": "515790", "trade_date": "2026-08-10", "anchor_price": 1.00}
    assert _verify_one(conn, {**base, "category": "减仓"}, 1)[0] == "应验"
    assert _verify_one(conn, {**base, "category": "砍仓"}, 1)[0] == "应验"
    assert _verify_one(conn, {**base, "category": "持有"}, 1)[0] == "无法判定"
    assert _verify_one(conn, {**base, "category": "加仓"}, 1)[0] == "未应验"


def test_complete_cycle():
    """端到端:存档(右侧初现)→ 跨日确认 → 增量4 验证(阈值+方向)。"""
    from alphaprism.planner.advice_verify import (_verify_one, advice_verification_stats)
    from alphaprism.planner.intraday_confirm import _confirm_one

    conn = _mk_db()
    # 突破日 0.850,随后 6 日:前 5 日站稳(确认),第 6 日大涨 12%(加仓类应验)
    closes = [0.850, 0.855, 0.860, 0.858, 0.862, 0.865, 0.955]
    t0 = "2026-08-10"
    for i, c in enumerate(closes):
        d = t0 if i == 0 else f"2026-08-1{i}"
        conn.execute(
            "INSERT INTO etf_kline_daily (symbol, trade_date, open, high, low, close,"
            " volume) VALUES (?,?,?,?,?,?,?)",
            ("515790", d, c, c + 0.01, c - 0.01, c, 100))
    conn.execute(
        "INSERT INTO advice_archive (trade_date, symbol, created_at, anchor_price,"
        " scenario, state_word, risk_level, category, advice_md, snapshot_md, degraded)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (t0, "515790", "22:12", 0.850, "趋势", "右侧初现", "正常", "加仓", "md", "snap"))
    conn.commit()
    arch = dict(conn.execute("SELECT * FROM advice_archive").fetchone())
    assert _confirm_one(conn, arch) == "右侧确认"
    # 增量4:5 日后收盘 0.865 → +1.8% < 5% → 部分应验;用第 6 日看 加仓 +12% → 应验
    assert _verify_one(conn, arch, 5)[0] == "部分应验"