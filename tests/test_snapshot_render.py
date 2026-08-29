# -*- coding: utf-8 -*-
"""渲染层回归:数据快照卡文字(用户直接看到的输出)。

与 test_intraday_engine.py(引擎数值层)互补:这里断言渲染文本的语义
(行结构、关键措辞、数字精度、口径标注),防止规则改动静默改变
用户看到的输出,或在用户已确认的表述上无意识回归。

样例:515050(通信ETF,2026-08-28 收盘后快照)真实数字,取自
docs/盘中数据快照.md;515790(光伏ETF)作为第二案例。
source: 用户 2026-08-29 复盘列出的 9 个口径问题 → 逐条固化当前行为。
"""
from alphaprism.planner.intraday import _asset_rows


def mk_515050() -> dict:
    """2026-08-28 收盘后 515050 事实(盘中数据快照.md 同源)。"""
    return {
        "code": "515050", "name": "通信ETF华夏",
        "daily": {
            "ma": {5: 1.021, 10: 1.041, 20: 1.028, 60: 1.142,
                   100: 1.108, 120: 1.056, 250: 0.878},
            "kdj": {"K": 39.36, "D": 39.92, "J": 38.25},
            "macd": {"dif": -0.014, "dea": -0.02, "hist": 0.011},
            "recent_high": 1.119, "recent_low": 0.887,
            "swing_high": 1.439, "swing_high_date": "2026-06-25",
            "avg5_vol": 8828600.0, "avg10_vol": 9741200.0,
        },
        "m30": {
            "ma": {5: 1.043, 10: 1.050, 20: 1.040, 60: 1.024, 250: 1.023},
            "kdj": {"K": 16.74, "D": 33.23, "J": -16.24},
            "macd": {"dif": 0.007, "dea": 0.009, "hist": -0.003},
            "range_low": 0.975, "range_high": 1.078,
        },
        "minute": {
            "price": 1.036, "change_pct": -1.33, "avg": 1.0553,
            "high": 1.078, "high_time": None, "low": 1.034,
            "vs_avg": -1.83, "intraday_dd": 3.90,
            "series": "09:30 1.046 / 10:00 1.073", "tail_vol": "尾盘缩量",
        },
        "orderbook": {
            "buy": [{"price": 1.036, "vol": 5190}, {"price": 1.035, "vol": 32400}],
            "sell": [{"price": 1.037, "vol": 12900}],
            "turnover_pct": 5.03, "waipan": 3745800.0, "neipan": 5014200.0,
            "weicha": 70459.0, "iopv": 1.0362, "premium_pct": -0.02,
            "vol_ratio": 0.98,
        },
        "fund_flow": {"share": 179.15, "date": "2026-06-30",
                      "share_chg": 69.20, "share_chg_pct": 62.94,
                      "scale_est": 185.64},
        "vol_ratio": 0.98, "range_pos": 64, "swing_dd": 28.01,
        "minutes_passed": 0,
    }


def mk_index() -> dict:
    """2026-08-28 上证指数事实(同上)。"""
    return {
        "code": "000001.SH", "name": "上证指数",
        "daily": {
            "ma": {5: 3918.544, 10: 3926.901, 20: 3916.064, 60: 3957.864,
                   100: 4012.926, 120: 4008.532, 250: 3983.616},
            "kdj": {"K": 55.63, "D": 47.62, "J": 71.63},
            "macd": {"dif": 2.971, "dea": -2.355, "hist": 10.652},
            "recent_high": 3994.18, "recent_low": 3797.64,
            "swing_high": 4258.86, "swing_high_date": "2026-05-14",
            "avg5_vol": 1000000.0, "avg10_vol": 1000000.0,
        },
        "m30": {"ma": {}, "kdj": {}, "macd": {},
                "range_low": None, "range_high": None},
        "minute": {"price": 3952.18, "change_pct": -0.11,
                   "high": 3970.31, "high_time": None, "low": 3947.80,
                   "series": "", "tail_vol": ""},
        "orderbook": {}, "fund_flow": {},
        "vol_ratio": 1.06, "range_pos": 79, "swing_dd": 7.20,
        "minutes_passed": 0,
    }


def _rows(facts, is_index=False) -> list[str]:
    return _asset_rows(facts, is_index=is_index)


# ------------------------------------------------------------------ 标的行结构

def test_avg_line_shows_vs_avg():
    """问题①:均价行必须同时呈现均价与 vs_avg,且百分比为 2 位。"""
    rows = _rows(mk_515050())
    avg = next(r for r in rows if r.startswith("| 分时 | 均价"))
    assert "1.055" in avg and "-1.83%" in avg


def test_intraday_hl_line_contains_both_extremes():
    """问题②:日内高低行含 高@时刻/低 与回撤%,主语未写明(现状)。"""
    rows = _rows(mk_515050())
    hl = next(r for r in rows if r.startswith("| 分时 | 日内高低"))
    assert "1.078@-" in hl and "1.034" in hl and "3.90%" in hl
    assert "日内分位 5%(低位区)" in hl


def test_position_line_band():
    """次要⑧:64% 显示「中上轨」(保留连续数值,不强行多档;2026-08-29 用户拍板)。"""
    rows = _rows(mk_515050())
    pos = next(r for r in rows if r.startswith("| 日线 | 位置"))
    assert "64%" in pos and "中上轨" in pos
    assert "0.887-1.119" in pos and "回撤 28.01%" in pos


def test_position_line_band_lower_half():
    """位置 40% → 中下轨(<50);且 <30/≥70 仍为下/上轨区。"""
    f = mk_515050()
    f["range_pos"] = 40
    assert "中下轨" in next(r for r in _rows(f) if r.startswith("| 日线 | 位置"))
    f["range_pos"] = 25
    assert "下轨区" in next(r for r in _rows(f) if r.startswith("| 日线 | 位置"))
    f["range_pos"] = 75
    assert "上轨区" in next(r for r in _rows(f) if r.startswith("| 日线 | 位置"))


def test_30m_rows_split():
    """30分均线/KDJ/MACD/区间 各自成行(2026-08-29 拆分后)。"""
    rows = _rows(mk_515050())
    assert any(r.startswith("| 30分 | 均线") for r in rows)
    assert any(r.startswith("| 30分 | KDJ") for r in rows)
    assert any(r.startswith("| 30分 | MACD") for r in rows)
    assert any(r.startswith("| 30分 | 区间") for r in rows)


def test_orderbook_rows_split():
    """盘口五档/委差/外内盘/IOPV/折溢价 各自成行;外内盘带结论词+来源。"""
    rows = _rows(mk_515050())
    assert any(r.startswith("| 盘口 | 五档") for r in rows)
    assert any(r.startswith("| 盘口 | 委差") for r in rows)
    wp = next(r for r in rows if r.startswith("| 盘口 | 外内盘"))
    assert "抛压占优(内盘大)" in wp        # 结论词打头(LLM 规则①依赖)
    assert "374.58万手" in wp and "501.42万手" in wp and "来源:腾讯" in wp
    assert any(r.startswith("| 盘口 | IOPV") for r in rows)
    assert any(r.startswith("| 盘口 | 折溢价") for r in rows)


def test_vol_ratio_label():
    """量能行:量比 0.98 → 平量(0.8~1.5);换手带腾讯流通份额口径标注(问题⑤)。"""
    rows = _rows(mk_515050())
    vol = next(r for r in rows if r.startswith("| 量能 | 量能"))
    assert "0.98(平量)" in vol and "换手 5.03%(腾讯流通份额口径)" in vol
    assert "尾盘缩量" in vol


def test_fund_rows():
    """资金行:份额/估算规模。"""
    rows = _rows(mk_515050())
    assert any("179.15亿份(2026-06-30)" in r and "+69.20亿份" in r
               for r in rows)
    assert any("185.64亿元" in r for r in rows)


# ------------------------------------------------------------------ 大盘行

def test_index_verdict_line():
    """问题⑥:大盘方向定性已解耦为 趋势维度 + 动能维度 两行事实(不再打包)。"""
    rows = _rows(mk_index(), is_index=True)
    trend = next(r for r in rows if "趋势维度" in r)
    mom = next(r for r in rows if "动能维度" in r)
    assert "站上 M20(现价 3952.180 vs M20 3916.064)" in trend
    assert "DIF 2.971 零上" in mom and "DEA -2.355 零下" in mom
    assert not any("方向定性" in r for r in rows)


def test_index_skips_avg_and_orderbook():
    """指数无均价/五档/外内盘行。"""
    rows = _rows(mk_index(), is_index=True)
    assert not any(r.startswith("| 分时 | 均价") for r in rows)
    assert not any(r.startswith("| 盘口") for r in rows)


def test_index_verdict_suppression_branch():
    """大盘两维分支:跌破 M20 + DIF/DEA 均零下 → 两行都报空头事实。"""
    idx = mk_index()
    d = idx["daily"]
    d["ma"] = {5: 3960.0, 10: 3970.0, 20: 3980.0, 60: 3990.0,
               100: 4000.0, 120: 4010.0, 250: 4020.0}
    d["macd"] = {"dif": -1.0, "dea": -2.0, "hist": 0.5}
    rows = _rows(idx, is_index=True)
    trend = next(r for r in rows if "趋势维度" in r)
    mom = next(r for r in rows if "动能维度" in r)
    assert "跌破 M20(现价 3952.180 vs M20 3980.000)" in trend
    assert "DIF -1.000 零下" in mom and "DEA -2.000 零下" in mom


if __name__ == "__main__":
    import traceback
    fn = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ok = fail = 0
    for f in fn:
        try:
            f()
            ok += 1
            print(f"PASS {f.__name__}")
        except Exception as e:
            fail += 1
            print(f"FAIL {f.__name__}: {e}")
            traceback.print_exc(limit=2)
    print(f"{ok} passed, {fail} failed")
    raise SystemExit(1 if fail else 0)