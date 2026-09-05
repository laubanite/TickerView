# -*- coding: utf-8 -*-
"""个股状态机回测·扩池版(方案 §14.2):pool_stock_mvp 全池合格成员,两臂同批。

合格性(数据口径,不挑个体):
  - csv 存在;窗口起点 2021-06-01 前有 ≥260 根(250日暖机;次新股自动出局);
  - 数据存续至 2026-06-01 之后(退市/停牌过长出局)——保证等权组合曲线同长。
分散性呈现:板块前缀(30创业板/60沪主板/00深主板/68科创板) × 窗口年化波动三分位。
判据仍为 §14 预注册三条件(≥2/3 放行),扩池是稳健性检验,非重新注册。

运行: python scripts/backtest_stock_risk_wide.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "bsr", ROOT / "scripts" / "backtest_stock_risk.py")
bsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bsr)

from backtest_engine.core.data_loader import load_daily_csv  # noqa: E402

CACHE = ROOT / "data" / "kline_cache"
POOL = ROOT / "backtest_engine" / "configs" / "pool_stock_mvp.yaml"
START = bsr.START                      # 2021-06-01
COVERAGE_END = "2026-06-01"            # 存续性门槛


def load_pool() -> list[tuple[str, str, str]]:
    """[(code, name, layer)] from pool yaml(UTF-8;控制台乱码不影响解析)。"""
    data = yaml.safe_load(POOL.read_text(encoding="utf-8"))
    return [(str(m["symbol"]), str(m.get("name") or ""), str(m.get("layer") or ""))
            for m in (data.get("members") or [])]


def annualized_vol(df: pd.DataFrame) -> float:
    w = df[(df["trade_date"].astype(str) >= START)
           & (df["trade_date"].astype(str) <= "2099")]
    c = w["close"].astype(float)
    ret = np.log(c / c.shift(1)).dropna()
    return float(ret.std() * np.sqrt(252)) if len(ret) > 60 else float("nan")


def main() -> None:
    pool = load_pool()
    print(f"=== 扩池回测(§14.2):候选 {len(pool)} 只 ===")
    eligible: list[tuple[str, str, str, pd.DataFrame]] = []
    excluded: dict[str, int] = {}
    for code, name, layer in pool:
        path = CACHE / f"{code}_daily.csv"
        if not path.exists():
            excluded["无缓存"] = excluded.get("无缓存", 0) + 1
            continue
        df = load_daily_csv(path)
        df["trade_date"] = df["trade_date"].astype(str)
        dates = df["trade_date"].tolist()
        i0 = next((i for i, d in enumerate(dates) if d >= START), None)
        if i0 is None or i0 < 260:
            excluded["暖机不足(次新/上市晚)"] = excluded.get("暖机不足(次新/上市晚)", 0) + 1
            continue
        if dates[-1] < COVERAGE_END:
            excluded["存续不足"] = excluded.get("存续不足", 0) + 1
            continue
        eligible.append((code, name, layer, df))
    print(f"合格 {len(eligible)} 只;剔除 {sum(excluded.values())} 只 {excluded}")
    if len(eligible) < 10:
        print("合格池过小,中止")
        sys.exit(1)

    # 分散性:板块前缀 × 波动三分位
    vols = {code: annualized_vol(df) for code, _, _, df in eligible}
    vv = sorted(v for v in vols.values() if v == v)
    q1, q2 = vv[len(vv) // 3], vv[2 * len(vv) // 3]

    def vol_band(v: float) -> str:
        return "低波" if v <= q1 else ("中波" if v <= q2 else "高波")

    def board(code: str) -> str:
        return {"30": "创业板", "68": "科创板", "60": "沪主板", "00": "深主板",
                "301": "创业板"}.get(code[:2], code[:3])

    boards: dict[str, int] = {}
    bands: dict[str, int] = {}
    for code, _, _, _df in eligible:
        boards[board(code)] = boards.get(board(code), 0) + 1
        bands[vol_band(vols[code])] = bands.get(vol_band(vols[code]), 0) + 1
    print(f"板块分布: {boards}")
    print(f"波动分布(年化,三分位 {q1:.2f}/{q2:.2f}): {bands}")

    for arm, confirm in (("v1", False), ("v1b", True)):
        rows = []
        for code, name, _layer, df in eligible:
            r = bsr.backtest_one(df, confirm=confirm, code=code)
            if "error" in r:
                continue
            r["name"], r["band"], r["board"] = name, vol_band(vols[code]), board(code)
            rows.append(r)
        if not rows:
            continue
        n = len(rows)
        sret = sum(r["ret_pct"] for r in rows) / n
        bret = sum(r["bh_ret_pct"] for r in rows) / n
        pmdd = bsr._portfolio_mdd([r["equity"] for r in rows])
        bpmdd = bsr._portfolio_mdd([r["bh_equity"] for r in rows])
        sells = sum(r["sells"] for r in rows)
        imp = (bpmdd - pmdd) / bpmdd * 100 if bpmdd else 0
        win_mdd = sum(1 for r in rows if r["mdd_pct"] < r["bh_mdd_pct"])
        win_ret = sum(1 for r in rows if r["ret_pct"] > r["bh_ret_pct"])
        print(f"\n--- {arm} 臂(n={n}): 策略 {sret:+.1f}% (组合MDD {pmdd:.1f}%) "
              f"vs 持有 {bret:+.1f}% (组合MDD {bpmdd:.1f}%) | 回撤改善 {imp:.0f}% "
              f"| 卖出 {sells} 次 | 个股MDD改善 {win_mdd}/{n} | 个股收益占优 {win_ret}/{n}")
        c1, c2, c3 = imp >= 15, sells >= 30, (sret - bret) >= -2
        print(f"判据(§14): c1 [{'过' if c1 else '不过'}] c2 [{'过' if c2 else '不过'}] "
              f"c3 [{'过' if c3 else '不过'}] → {'放行' if (c1 + c2 + c3) >= 2 else '不放行'}")
        # 分组明细(波动带 × 板块)
        for band in ("低波", "中波", "高波"):
            grp = [r for r in rows if r["band"] == band]
            if not grp:
                continue
            g = len(grp)
            gs = sum(r["ret_pct"] for r in grp) / g
            gb = sum(r["bh_ret_pct"] for r in grp) / g
            gm = bsr._portfolio_mdd([r["equity"] for r in grp])
            gbm = bsr._portfolio_mdd([r["bh_equity"] for r in grp])
            gimp = (gbm - gm) / gbm * 100 if gbm else 0
            print(f"  {band}组(n={g}): 策略 {gs:+.1f}% vs 持有 {gb:+.1f}% "
                  f"| 组合MDD {gm:.1f}% vs {gbm:.1f}% (改善 {gimp:.0f}%)")
        # 排序逐名(收益差升序前5 + 回撤改善前5)
        diffs = sorted(rows, key=lambda r: r["ret_pct"] - r["bh_ret_pct"])
        print("  收益差最差5:",
              "; ".join(f"{r['symbol']}{r['name']}({r['band']}):"
                        f"{r['ret_pct'] - r['bh_ret_pct']:+.0f}pp" for r in diffs[:5]))
        imps = sorted(rows, key=lambda r: (r["bh_mdd_pct"] - r["mdd_pct"])
                      / r["bh_mdd_pct"] if r["bh_mdd_pct"] else -1, reverse=True)
        print("  回撤改善最好5:",
              "; ".join(f"{r['symbol']}{r['name']}:"
                        f"{(r['bh_mdd_pct'] - r['mdd_pct']) / r['bh_mdd_pct'] * 100:.0f}%"
                        for r in imps[:5]))


if __name__ == "__main__":
    main()
