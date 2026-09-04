# -*- coding: utf-8 -*-
"""盘中规则历史回放器(2026-09,docs/TODO · 盘中MVP核心:规则回测校准)。

语义:对每个历史交易日 D,用「当日收盘后可得」信息(日K≤D、当日收盘合成分时、
大盘≤D)重建快照 facts,跑确定性引擎(C5' 状态机 → C4' 参数档位 → 建议类别),
得到方向性建议(锚定价=当日收盘价),与未来 N 日收盘比对判应验(±5%,借 advice_verify 判据)。

前视偏差防护:
- 所有字段只用 ≤D 的数据(回放按 trade_date 切片);
- 盘中实时接口(分时序列/盘口/份额实时)不参与——分时以当日收盘合成(收盘决策语义);
- 30分执行刻度不参与(回放=日线粒度策略层;30分只是执行刻度,非策略参数)。

输出:逐日记录 + 总体/按状态词/按档位宽度分组/按年 + walk-forward 稳定性报告。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config import PROJECT_ROOT
from ..fetchers.etf_kline import fetch_daily
from .intraday import INDEX_NAME, INDEX_THS, _asset_facts
from .intraday_engine import CATEGORY_DIRECTION, param_anchors, signal_state, suggest_category

logger = logging.getLogger(__name__)

VERIFY_THRESHOLD_PCT = 5.0          # 应验阈值(与 advice_verify 同口径,初值)
HORIZONS = (3, 5, 10)               # 3/5/10 日(5 为主,同期盘后验证)
RISK_HORIZONS = (5, 10, 20)         # 风控有效性窗口(破位类信号)
CUT_RATIO = 0.5                     # 路径A:执行减仓后保留仓位(50%持仓,50%现金)
MIN_BARS = 120                      # 每个回放日至少需要的历史日K根数
CACHE_DIR = PROJECT_ROOT / "data" / "kline_cache"

# 大盘日K缓存(一键回放不重复拉历史)
_INDEX_DF: pd.DataFrame | None = None


def ensure_cache_dir() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def load_daily_cache(symbol: str) -> pd.DataFrame | None:
    """读 data/kline_cache/{symbol}_daily.csv;无缓存/太旧返回 None。"""
    p = CACHE_DIR / f"{symbol}_daily.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, dtype={"trade_date": str})
        df["trade_date"] = df["trade_date"].astype(str).str[:10]
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning("日K缓存读取失败 %s: %s", symbol, exc)
        return None


def ensure_daily_history(symbol: str, start: str = "2020-01-01",
                         end: str = "2030-12-31") -> pd.DataFrame:
    """日K全历史:缓存优先,缺则腾讯拉取并落 `data/kline_cache`(不入库)。"""
    df = load_daily_cache(symbol)
    if df is not None and len(df) > 60:
        return df
    df = fetch_daily(symbol, start, end)
    if df is None or df.empty:
        raise RuntimeError(f"{symbol} 日K拉取为空")
    df = df.reset_index(drop=True)
    p = ensure_cache_dir() / f"{symbol}_daily.csv"
    df.to_csv(p, index=False, encoding="utf-8")
    logger.info("日K缓存写入 %s (%d 根)", p, len(df))
    return df


def load_index_history() -> pd.DataFrame:
    """上证指数日K(全历史,拉一次缓存于内存/文件)。"""
    global _INDEX_DF
    if _INDEX_DF is not None:
        return _INDEX_DF
    p = CACHE_DIR / "sh000001_daily.csv"
    if p.exists():
        try:
            _INDEX_DF = pd.read_csv(p, dtype={"trade_date": str})
            _INDEX_DF["trade_date"] = _INDEX_DF["trade_date"].astype(str).str[:10]
            return _INDEX_DF
        except Exception:  # noqa: BLE001
            pass
    from ..fetchers.etf_kline import fetch_index_daily
    _INDEX_DF = fetch_index_daily(INDEX_THS, "2020-01-01", "2030-12-31").reset_index(drop=True)
    _INDEX_DF.to_csv(ensure_cache_dir() / "sh000001_daily.csv", index=False, encoding="utf-8")
    return _INDEX_DF


def _synthetic_minute(d: str, close: float, prev_close: float | None,
                      vol: float | None) -> pd.DataFrame:
    """收盘决策语义:当日收盘合成一根分时(价格=收盘,均价=收盘,量=当日量)。"""
    prev = prev_close if prev_close is not None else close
    return pd.DataFrame([{"ts": f"{d} 15:00", "price": close, "prev_close": prev,
                          "avg_price": close, "vol": float(vol or 0.0)}])


def asof_facts(symbol: str, name: str, d: str,
               daily: pd.DataFrame, index_daily: pd.DataFrame,
               now_dt: datetime | None = None) -> dict:
    """重建 D 日收盘时点 facts(只用 ≤D 的数据;30分/盘口/资金不参与回放)。"""
    now_dt = now_dt or datetime.strptime(d + " 15:00", "%Y-%m-%d %H:%M")
    d_daily = daily[daily["trade_date"] <= d].reset_index(drop=True)
    if d_daily.empty:
        raise ValueError(f"{symbol} {d} 无日K")
    last_close = float(d_daily["close"].iloc[-1])
    prev_close = (float(d_daily["close"].iloc[-2])
                  if len(d_daily) >= 2 else last_close)
    last_vol = float(d_daily["volume"].iloc[-1]) if "volume" in d_daily.columns else None
    mf = _synthetic_minute(d, last_close, prev_close, last_vol)
    etf = _asset_facts(symbol, name, d_daily, None, mf, now_dt, None, None)
    idx = None
    if index_daily is not None and not index_daily.empty:
        id_daily = index_daily[index_daily["trade_date"] <= d].reset_index(drop=True)
        if not id_daily.empty:
            i_close = float(id_daily["close"].iloc[-1])
            i_prev = (float(id_daily["close"].iloc[-2])
                      if len(id_daily) >= 2 else i_close)
            idx = _asset_facts(INDEX_THS, INDEX_NAME, id_daily, None,
                               _synthetic_minute(d, i_close, i_prev, None),
                               now_dt, None, None)
    rs_5d = None
    e5, i5 = etf.get("chg5d"), (idx or {}).get("chg5d")
    if e5 is not None and i5 is not None:
        rs_5d = round(e5 - i5, 2)
    return {"etf": etf, "index": idx, "rs_5d": rs_5d,
            "date": d, "now": now_dt.strftime("%H:%M")}


def _outcome_n(closes_after: list[float], anchor: float, direction: str | None,
               n: int) -> tuple[str | None, float | None]:
    """N 日判定(advice_verify 同口径):应验 | 部分应验 | 未应验 | 无法判定。"""
    if direction is None or not closes_after:
        return None, None
    if len(closes_after) < n:
        return None, None
    c = float(closes_after[n - 1])
    move = (c / anchor - 1) * 100
    if direction == "up":
        out = ("应验" if move >= VERIFY_THRESHOLD_PCT
               else "部分应验" if move > 0 else "未应验")
    else:
        out = ("应验" if move <= -VERIFY_THRESHOLD_PCT
               else "部分应验" if move < 0 else "未应验")
    return out, round(move, 2)


def replay_symbol(symbol: str, name: str, daily: pd.DataFrame,
                  index_daily: pd.DataFrame,
                  start: str = "2024-01-01", end: str = "") -> list[dict]:
    """逐交易日回放引擎 → 方向性建议 + N 日验证。返回逐日记录。"""
    daily = daily.copy()
    daily["trade_date"] = daily["trade_date"].astype(str).str[:10]
    daily = daily[daily["trade_date"] <= (end or "9999-12-31")]
    daily = daily.sort_values("trade_date").reset_index(drop=True)
    dates = sorted(daily["trade_date"].unique())
    closes = [float(x) for x in daily["close"].tolist()]
    date_idx = {str(d): i for i, d in enumerate(daily["trade_date"])}
    records: list[dict] = []
    for d in dates:
        if d < start:
            continue
        i = date_idx[d]
        if i + 1 < MIN_BARS:
            continue
        try:
            facts = asof_facts(symbol, name, d, daily, index_daily)
        except Exception as exc:  # noqa: BLE001
            logger.warning("回放 %s %s 失败: %s", symbol, d, exc)
            continue
        state = signal_state(facts)
        if state.get("state_word") in (None, "数据不足"):
            continue
        anchors = param_anchors(facts, {})
        if not anchors.get("ok"):
            continue
        category = suggest_category("", state)   # 无类别行 → 状态→类别映射(与生产同口径)
        direction = CATEGORY_DIRECTION.get(category)
        future = closes[i + 1:]
        rec = {
            "date": d, "symbol": symbol, "name": name,
            "state_word": state.get("state_word", ""),
            "scenario": state.get("scenario", ""),
            "category": category, "direction": direction,
            "anchor": anchors.get("anchor_price"),
            "breakout": anchors.get("breakout_add"),
            "zone_up": None, "zone_lo": None,
            "cut": anchors.get("cut_loss"),
            "stop": anchors.get("stop_loss"),
            "no_pullback": bool(anchors.get("no_pullback")),
        }
        zone = anchors.get("pullback_zone")
        if zone:
            rec["zone_up"] = zone.get("upper")
            rec["zone_lo"] = zone.get("lower")
        for n in HORIZONS:
            out, move = _outcome_n(future, rec["anchor"], direction, n)
            rec[f"out{n}"] = out
            rec[f"move{n}"] = move
        # 风控有效性(减仓类)+ 破位次日延续性(日线可得字段,零新数据源)
        rec["m20"] = (facts.get("etf") or {}).get("daily", {}).get("ma", {}).get(20)
        if i + 1 < len(closes):
            if "open" in daily.columns:
                rec["next_open"] = float(daily["open"].iloc[i + 1])
            if "volume" in daily.columns:
                rec["next_vol"] = float(daily["volume"].iloc[i + 1])
                rec["vol_d"] = float(daily["volume"].iloc[i])
            rec["next_close"] = float(daily["close"].iloc[i + 1])
        if len(future) >= 5:
            rec["nday_low5"] = round((min(future[:5]) / rec["anchor"] - 1) * 100, 2)
        if direction == "down":
            cls, gap = _break_class(rec["anchor"], rec["m20"], rec.get("next_open"),
                                    rec.get("next_close"), rec.get("next_vol"),
                                    rec.get("vol_d"))
            rec["break_class"] = cls
            rec["break_gap"] = bool(gap)
            rec["risk"] = _risk_metrics(future, rec["anchor"])
        records.append(rec)
    return records


# --------------------------------------------------------------------------- 报告

def _fmt_pct(x: float | None, digits: int = 1) -> str:
    return f"{x:.{digits}f}%" if x is not None else "-"


def _equity_maxdd(equity: list[float]) -> float:
    """净值序列最大回撤 %(峰值到谷值,不含 t=0)。"""
    peak = equity[0] if equity else 1.0
    worst = 0.0
    for e in equity[1:]:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    return round(worst, 2)


def _risk_metrics(future: list[float], anchor: float, cut_ratio: float = CUT_RATIO,
                  horizons: tuple[int, ...] = RISK_HORIZONS) -> dict:
    """风控有效性(减仓类信号):路径A=执行减仓(留 cut_ratio 仓位) vs 路径B=满仓硬扛。

    每个 N 日窗口输出:
      eff     = (B最大回撤 - A最大回撤)/B最大回撤(>30% 真保险,<10% 无保险,<0 反保险)
      end_ratio= A终值/B终值
      worstA/B = 两路径最低收盘(相对锚定价 %)
      maxddA/B = 两路径最大回撤 %
    """
    if anchor is None or not future:
        return {}
    out: dict[int, dict] = {}
    for n in horizons:
        seg = future[:n]
        if len(seg) < n:
            break
        eB = [1.0] + [float(c) / anchor for c in seg]
        eA = [1.0] + [cut_ratio * (c / anchor) + (1 - cut_ratio) for c in seg]
        ddB, ddA = _equity_maxdd(eB), _equity_maxdd(eA)
        eff = (ddB - ddA) / ddB * 100 if ddB > 0 else None
        out[n] = {
            "eff": round(eff, 1) if eff is not None else None,
            "maxddA": ddA, "maxddB": ddB,
            "end_ratio": round(eA[-1] / eB[-1], 3) if eB[-1] else None,
        }
        # worst 以"最低收盘 vs 锚"计(两路径同底,差异在仓位):A 亏损 = cut_ratio × (最低-锚)/锚
        worst_pct = (min(seg) / anchor - 1) * 100
        out[n]["worstA"] = round(cut_ratio * worst_pct, 2)
        out[n]["worstB"] = round(worst_pct, 2)
    return out


def _break_class(anchor: float, m20: float | None, next_open: float | None,
                 next_close: float | None, next_vol: float | None,
                 vol_d: float | None) -> tuple[str, bool]:
    """破位次日延续性分类(Kimi 方案):A 次日收回 / B 未收回缩量 / C 未收回放量 / D 跳空低开。

    返回 (类别, 是否跳空)。A/B/C 按次日收盘 vs 破位日 M20;跳空 = 次日开盘 < 锚定价×0.99。
    """
    gap = (next_open is not None and anchor is not None
           and next_open < anchor * 0.99)
    if next_close is None or m20 is None:
        return "?", gap
    if next_close > m20:
        cls = "A"
    elif next_vol is not None and vol_d is not None and next_vol >= vol_d:
        cls = "C"
    else:
        cls = "B"
    if gap:
        cls = "D"
    return cls, gap


def _bucket(v: float | None, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    """幅度分桶(传入绝对值):edges 为各桶上限(升序);labels 一一对应(最后桶开口)。"""
    if v is None:
        return "?"
    for e, lab in zip(edges, labels[:-1]):
        if v < e:
            return lab
    return labels[-1]


def _group_stats(rows: list[dict], key_fn, threshold: float = VERIFY_THRESHOLD_PCT) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    out = []
    for k, rs in sorted(groups.items()):
        dec = [r for r in rs if r["out5"] in ("应验", "部分应验")]
        strict = [r for r in rs if r["out5"] == "应验"]
        un = [r for r in rs if r["out5"] == "未应验"]
        # 盈亏比:5 日 move 平均(应验方 vs 未应验方,按方向归一)
        wins = [abs(r["move5"]) for r in rs
                if r["out5"] in ("应验", "部分应验") and r["move5"] is not None]
        loss = [abs(r["move5"]) for r in rs if r["out5"] == "未应验" and r["move5"] is not None]
        pl = (sum(wins) / len(wins) / (sum(loss) / len(loss))
              if wins and loss and sum(loss) > 0 else None)
        has_out = [r for r in rs if r["out5"] is not None]
        out.append({
            "key": k, "n": len(rs), "verdict_n": len(has_out),
            "hit_rate": len(strict) / len(has_out) * 100 if has_out else None,
            "dir_ok_rate": len(dec) / len(has_out) * 100 if has_out else None,
            "pl_ratio": pl,
            "mean_move5": round(sum(r["move5"] for r in rs if r["move5"] is not None) /
                                len([r for r in rs if r["move5"] is not None]), 2)
            if any(r["move5"] is not None for r in rs) else None,
        })
    return out


def build_report(symbol: str, rows: list[dict],
                 start: str = "", end: str = "") -> str:
    """《规则检验报告》markdown:总体 / 状态词 / 档位宽度分组 / 按年 + 稳定性。"""
    lines = [f"# 规则检验报告 · {symbol}({len(rows)} 个可判定信号日)",
             "",
             f"> 回放区间 {start or '历史起点'} ~ {end or '最后交易日'} · 日线粒度(收盘决策) · "
             f"判据:±{VERIFY_THRESHOLD_PCT:.0f}%({HORIZONS} 日) · 30分执行刻度不参与",
             "> 输出:信号日记录 + 统计分组 + 稳定性(walk-forward 窗口 250 交易日/步 42)。",
             ""]
    dec = [r for r in rows if r["direction"]]
    overall = _group_stats(dec, lambda r: "all")[0] if dec else None
    pl_txt = f"{overall['pl_ratio']:.2f}" if overall and overall["pl_ratio"] else "-"
    lines += ["## ① 总体", "", "| 指标 | 值 |", "|---|---|",
              f"| 方向性信号日 | {len(dec)} |",
              f"| 5日应验率(≥{VERIFY_THRESHOLD_PCT:.0f}%) | "
              f"{_fmt_pct(overall['hit_rate'] if overall else None)} |",
              f"| 5日方向对率 | "
              f"{_fmt_pct(overall['dir_ok_rate'] if overall else None)} |",
              f"| 5日盈亏比(应验vs未应验均幅) | {pl_txt} |",
              ""]
    for title, key in (("状态词", lambda r: r["state_word"]),
                       ("建议类别", lambda r: r["category"])):
        gs = [g for g in _group_stats(rows, key)]
        if not gs:
            continue
        lines += [f"## ② 按{title}", "",
                  "| 组 | 信号日 | 5日应验率 | 方向对率 | 盈亏比 | 平均5日move% |",
                  "|---|---|---|---|---|---|"]
        for g in gs:
            pl2 = f"{g['pl_ratio']:.2f}" if g["pl_ratio"] else "-"
            lines.append(f"| {g['key']} | {g['n']} | {_fmt_pct(g['hit_rate'])} | "
                         f"{_fmt_pct(g['dir_ok_rate'])} | {pl2} | {g['mean_move5']} |")
        lines.append("")
    # 档位宽度分组(回测校准的命门:止损/减仓档放多深历史才站得住)
    lines += ["## ③ 档位宽度分组(相对锚定价 %,5 日口径)", ""]
    for title, field, edges, labs in (
            ("止损深度", "stop", (3, 6, 10, 15), ("0~-3", "-3~-6", "-6~-10", "-10~-15", ">-15")),
            ("减仓档距离", "cut", (2, 4, 7), ("0~-2", "-2~-4", "-4~-7", ">-7")),
            ("突破档距离", "breakout", (3, 6, 12), ("+0~3", "+3~6", "+6~12", ">+12"))):
        gs = _group_stats(dec, lambda r, f=field, e=edges, l=labs: _bucket(
            abs(r[f]) if r[f] is not None else None, e, l))
        lines += [f"### {title}", "",
                  "| 分桶 | 信号日 | 5日应验率 | 方向对率 | 平均5日move% |",
                  "|---|---|---|---|---|"]
        for g in gs:
            lines.append(f"| {g['key']} | {g['n']} | {_fmt_pct(g['hit_rate'])} | "
                         f"{_fmt_pct(g['dir_ok_rate'])} | {g['mean_move5']} |")
        lines.append("")
    # 按年 + walk-forward 稳定性
    lines += ["## ④ 稳定性", ""]
    years: dict[str, list[dict]] = {}
    for r in rows:
        years.setdefault(r["date"][:4], []).append(r)
    lines += ["| 年份 | 信号日 | 5日应验率 | 方向对率 |", "|---|---|---|---|"]
    for y, rs in sorted(years.items()):
        g = _group_stats([r for r in rs if r["direction"]], lambda r: "all")[0] if any(
            r["direction"] for r in rs) else None
        lines.append(f"| {y} | {len(rs)} | {_fmt_pct(g['hit_rate'] if g else None)} | "
                     f"{_fmt_pct(g['dir_ok_rate'] if g else None)} |")
    lines.append("")
    # walk-forward 滚动(250/42)
    wf = []
    sorted_dec = sorted(dec, key=lambda r: r["date"])
    i = 0
    while i + 250 <= len(sorted_dec):
        seg = sorted_dec[i:i + 250]
        g = _group_stats(seg, lambda r: "all")[0]
        wf.append({"start": seg[0]["date"], "end": seg[-1]["date"],
                   "n": len(seg), "hit": g["hit_rate"], "dir": g["dir_ok_rate"]})
        i += 42
    lines += ["| 窗口(滚动250) | 信号日 | 5日应验率 | 方向对率 |", "|---|---|---|---|"]
    for w in wf:
        lines.append(f"| {w['start']}~{w['end']} | {w['n']} | {_fmt_pct(w['hit'])} | {_fmt_pct(w['dir'])} |")
    if wf:
        hits = [w["hit"] for w in wf if w["hit"] is not None]
        lines += ["", f"> 稳定性:窗口数 {len(wf)} · 应验率均值 {_fmt_pct(sum(hits) / len(hits) if hits else None)}"
                      f" · 最低 {_fmt_pct(min(hits) if hits else None)}"
                      f" · 最高 {_fmt_pct(max(hits) if hits else None)}"]
    # ⑤ 风控有效性(减仓类):执行减仓(留 CUT_RATIO 仓位) vs 满仓硬扛
    down_r = [r for r in rows if r.get("direction") == "down" and r.get("risk")]
    if down_r:
        lines += ["", "## ⑤ 风控有效性(减仓类 · 执行减仓 vs 满仓硬扛)", "",
                  "| 窗口 | 样本 | 回撤缩小均值 | >30%(真保险) | <10%(无保险) | 负值(反保险) | 终值比A/B |",
                  "|---|---|---|---|---|---|---|"]
        for n in RISK_HORIZONS:
            effs = [r["risk"][n]["eff"] for r in down_r
                    if n in r["risk"] and r["risk"][n]["eff"] is not None]
            endr = [r["risk"][n]["end_ratio"] for r in down_r
                    if n in r["risk"] and r["risk"][n]["end_ratio"] is not None]
            if not effs:
                continue
            n_gt30 = sum(1 for e in effs if e > 30) / len(effs) * 100
            n_lt10 = sum(1 for e in effs if e < 10) / len(effs) * 100
            n_neg = sum(1 for e in effs if e < 0) / len(effs) * 100
            lines.append(f"| {n}日 | {len(effs)} | {_fmt_pct(sum(effs) / len(effs))} | "
                         f"{n_gt30:.0f}% | {n_lt10:.0f}% | {n_neg:.0f}% | "
                         f"{sum(endr) / len(endr):.3f} |")
        lines += ["", "> 解读:回撤缩小>30%=真保险(方向预测错也值得保留);<10%=无保险;负=反保险(越减越亏)。", ""]
    # ⑥ 破位次日延续性(放量破 M20 样本按次日走势分类)
    down_b = [r for r in rows if r.get("break_class") in ("A", "B", "C", "D")]
    if down_b:
        lines += ["## ⑥ 破位次日延续性(放量破 M20 样本)", "",
                  "| 类别 | 定义 | n | 占比 | 后5日平均收益 | 后5日最低均 |",
                  "|---|---|---|---|---|---|"]
        for cls, desc in (("A", "次日收回(收盘>M20)"), ("B", "未收回·缩量"),
                          ("C", "未收回·放量"), ("D", "次日跳空低开(<0.99×锚)")):
            rs = [r for r in down_b if r["break_class"] == cls]
            if not rs:
                continue
            m5 = [r["move5"] for r in rs if r["move5"] is not None]
            lo = [r["nday_low5"] for r in rs if r.get("nday_low5") is not None]
            lines.append(f"| {cls} | {desc} | {len(rs)} | {len(rs) / len(down_b) * 100:.0f}% | "
                         f"{_fmt_pct(sum(m5) / len(m5) if m5 else None)} | "
                         f"{_fmt_pct(sum(lo) / len(lo) if lo else None)} |")
        lines += ["", "> 解读:A 占比高且后5日收益为正 → 当日减仓错误,应改次日未收回/反抽再减;"
                      "C 占比高且跌幅大 → 破位有效,可考虑连续2日收破确认。", ""]
    # ⑦ 分半验证(训练/验证切分预设:前半 vs 后半;改参数后此段才有实操意义)
    dec_all = sorted((r for r in rows if r["direction"]), key=lambda r: r["date"])
    if dec_all:
        mid = dec_all[len(dec_all) // 2]["date"]
        lines += ["## ⑦ 分半验证(前半 vs 后半,预设训练/验证切分)", "",
                  "| 时段 | 方向性样本 | 5日应验率 | 方向对率 |", "|---|---|---|---|"]
        for label, rs in ((f"前半(≤{mid})", [r for r in dec_all if r["date"] <= mid]),
                          (f"后半(>{mid})", [r for r in dec_all if r["date"] > mid])):
            g = _group_stats(rs, lambda r: "all")[0] if rs else None
            lines.append(f"| {label} | {len(rs)} | {_fmt_pct(g['hit_rate'] if g else None)} | "
                         f"{_fmt_pct(g['dir_ok_rate'] if g else None)} |")
        lines.append("")
    return "\n".join(lines)


def run_replay(symbol: str, name: str = "", start: str = "2024-01-01",
               end: str = "") -> tuple[list[dict], str]:
    """一键回放:历史拉取/缓存 → 逐日引擎 → 报告。返回 (records, report_md)。"""
    daily = ensure_daily_history(symbol)
    index_daily = load_index_history()
    rows = replay_symbol(symbol, name or symbol, daily, index_daily, start, end)
    report = build_report(symbol, rows, start, end)
    return rows, report