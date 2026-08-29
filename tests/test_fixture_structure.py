# -*- coding: utf-8 -*-
"""真实快照 fixture 结构回归(2026-08-29 生成,7 只池内 ETF)。

断言「数据快照 + 盘面状态」的**结构语义**,不断言具体行情数值(数据会变):
- 行结构:两维解耦行存在、盘口拆分、30分拆分
- 口径标注:① 基准=均价 ② 现价距日内最高回撤 ④ 均线极差(非均价线)
- 止损档距离合理性:stage低 距现价过远时 — 记录问题,不要求修复(规则需 §6.1 校准)

fixture 由 scripts/_gen_fixtures.py 生成;数据源失败时跳过(不因网络/停牌红)。
"""
import glob
import os
import re

FIX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "snapshots")
STOP_FAR_DISTANCE = 0.25   # 止损档距现价 >25% → 记录过远(待 §6.1 校准)


def _load_all() -> dict[str, str]:
    out = {}
    for path in sorted(glob.glob(os.path.join(FIX_DIR, "*.md"))):
        with open(path, encoding="utf-8") as f:
            out[os.path.basename(path)[:-3]] = f.read()
    return out


def test_fixtures_exist():
    assert len(_load_all()) >= 3, "fixture 不足,先跑 scripts/_gen_fixtures.py"


def test_avg_basis_note_all_fixtures():
    """① 均价行:标注「基准=均价」的行必须存在(所有 fixture)。"""
    for code, md in _load_all().items():
        rows = [l for l in md.splitlines() if l.startswith("| 分时 | 均价")]
        assert rows, f"{code}: 缺均价行"
        assert any("基准=均价" in r for r in rows), f"{code}: 缺基准标注"


def test_intraday_dd_subject_all_fixtures():
    """② 日内高低行:主语为「现价距日内最高回撤」。"""
    for code, md in _load_all().items():
        rows = [l for l in md.splitlines() if l.startswith("| 分时 | 日内高低")]
        assert rows, f"{code}: 缺日内高低行"
        assert any("现价距日内最高回撤" in r for r in rows), \
            f"{code}: 回撤主语未写'现价'"


def test_spread_label_all_fixtures():
    """④ 盘面状态:标签为「均线极差(M5/M10/M20)」,不再叫'均价线极差'。"""
    for code, md in _load_all().items():
        assert "| 均线极差(M5/M10/M20) |" in md, \
            f"{code}: 标签未改名"
        assert "均价线极差" not in md, f"{code}: 残留旧标签"


def test_index_dims_decoupled_all_fixtures():
    """⑥ 大盘:趋势维度/动能维度两行存在,且无「方向定性(程序)」打包行。"""
    for code, md in _load_all().items():
        assert "| 大盘 | 趋势维度(程序) |" in md, f"{code}: 缺趋势维度"
        assert "| 大盘 | 动能维度(程序) |" in md, f"{code}: 缺动能维度"
        assert "方向定性(程序)" not in md, f"{code}: 残留打包标签"
        # 动能维度必须同时含 DIF 与 DEA 的零轴判断
        mom = next(l for l in md.splitlines()
                   if l.startswith("| 大盘 | 动能维度(程序) |"))
        assert "DIF" in mom and "DEA" in mom and "零上" in mom or "零下" in mom


def test_orderbook_split_all_fixtures():
    """盘口拆分行:五档/委差/外内盘/IOPV/折溢价各自存在。"""
    for code, md in _load_all().items():
        for marker in ("| 盘口 | 五档", "| 盘口 | 委差", "| 盘口 | 外内盘",
                       "| 盘口 | IOPV", "| 盘口 | 折溢价"):
            assert marker in md, f"{code}: 缺 {marker}"


def test_stop_loss_distance_sanity_report() -> str:
    """③ 止损档距离合理性:阶段低 距现价 >25% 的标的,打印报告(不红)。

    这是 §6.1 校准观测点:真实数据已示 515050(-50%)/159516(-61%)
    等止损远距问题。断言只报告,不要求修复——修复需先定规则。
    """
    report = []
    for code, md in _load_all().items():
        m = re.search(r"清仓止损 ([\d.]+)\((\S+?)\)", md)
        if not m:
            continue
        stop, src = float(m.group(1)), m.group(2)
        pm = re.search(r"\| 分时 \| 现价 \| ([\d.]+)", md)
        if not pm:
            continue
        price = float(pm.group(1))
        ratio = (price - stop) / price
        if ratio >= STOP_FAR_DISTANCE:
            report.append(f"{code}: 现价 {price} vs 止损 {stop}({src}) "
                          f"距现价 {ratio*100:.0f}% 过远")
    if report:
        print("### 止损远距观测(§6.1 待校准):")
        for line in report:
            print(" -", line)
    return "\n".join(report)


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