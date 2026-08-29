"""advice_verify 回归(2026-08-27,daily_stock_analysis decision_signal/outcome 迁移)。"""
from __future__ import annotations

from alphaprism.planner.advice_verify import HORIZONS, _direction, _signal_type_from_md, _verify_one


class _Row:
    def __init__(self, close):
        self._close = close

    def __getitem__(self, key):
        return self._close if key == "close" else None


class _FakeRows:
    def __init__(self, closes):
        self._closes = closes

    def fetchall(self):
        return [_Row(c) for c in self._closes]


class _FakeConn:
    def __init__(self, closes):
        self._closes = closes

    def execute(self, sql, params):
        return _FakeRows(self._closes)


def test_signal_type_from_md():
    assert _signal_type_from_md("- 信号类型: 试多候选 · 时效: 当日收盘") == "试多候选"
    assert _signal_type_from_md("- 信号类型: 破位退出 · 时效: 当日收盘") == "破位退出"
    assert _signal_type_from_md("无结论卡正文") is None
    assert _signal_type_from_md("") is None


def test_direction_signal_precedes_category():
    # 结论卡 signal_type 优先于建议类别
    arch = {"advice_md": "- 信号类型: 清仓止损 · 时效: 当日收盘", "category": "观望"}
    assert _direction(arch) == "down"
    arch2 = {"advice_md": "- 信号类型: 观望 · 时效: 当日收盘", "category": "试多"}
    assert _direction(arch2) is None          # 卡=观望(无方向)覆盖 类别=试多(看涨)
    arch3 = {"advice_md": "- 信号类型: 右侧初现候选 · 时效: 当日收盘", "category": "持有"}
    assert _direction(arch3) == "up"
    # 无卡 → 回退类别
    assert _direction({"advice_md": "", "category": "加仓"}) == "up"
    assert _direction({"advice_md": "", "category": "减仓"}) == "down"
    assert _direction({"advice_md": "", "category": "观望"}) is None


def test_verify_one_outcome_and_direction_correct():
    arch_up = {"symbol": "515790", "trade_date": "2026-08-25", "anchor_price": 0.842,
               "advice_md": "- 信号类型: 试多候选 · 时效: 当日收盘", "category": "观望"}
    conn = _FakeConn([0.90])          # 1 日后 +6.9% → 应验
    out, move, correct = _verify_one(conn, arch_up, 1)
    assert out == "应验" and correct == 1 and move == 6.89
    conn2 = _FakeConn([0.83])         # -1.4% → 部分应验(direction up,move>0? no -1.4 → 未应验)
    out2, move2, correct2 = _verify_one(conn2, arch_up, 1)
    assert out2 == "未应验" and correct2 == 0
    # 看跌方向 + 大跌 → 应验
    arch_down = {"symbol": "515790", "trade_date": "2026-08-25", "anchor_price": 0.842,
                 "advice_md": "- 信号类型: 破位退出 · 时效: 当日收盘", "category": "观望"}
    out3, _move3, correct3 = _verify_one(_FakeConn([0.79]), arch_down, 1)   # -6.2%
    assert out3 == "应验" and correct3 == 1
    # 无方向 → 无法判定
    arch_none = {"symbol": "515790", "trade_date": "2026-08-25", "anchor_price": 0.842,
                 "advice_md": "- 信号类型: 观望 · 时效: 当日收盘", "category": "观望"}
    out4, move4, correct4 = _verify_one(_FakeConn([0.90]), arch_none, 1)
    assert out4 == "无法判定" and correct4 is None
    # K线不足 → 无法判定
    out5, _, c5 = _verify_one(_FakeConn([]), arch_up, 3)
    assert out5 == "无法判定" and c5 is None


def test_horizons_migrated():
    assert HORIZONS == (1, 3, 5, 10)