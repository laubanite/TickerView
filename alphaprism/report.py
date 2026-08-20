"""盘后日报(阶段1数据版):跟踪池状态 + 数据质量,Markdown 输出。

阶段2起:结构 / 分型 / 笔 / 信号占位区将替换为真实计算。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import PROJECT_ROOT

_HOLIDAY_GAP_THRESHOLD_DAYS = 15  # 超过即判定数据缺口(中国长假间隔最多 ~12 天)
_ANOMALY_TOL_PCT = 1.0  # 涨跌幅校验容忍偏差(百分点)


def _fmt(x, digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    return f"{x:.{digits}f}"


def _fmt_vol(x) -> str:
    """成交额 / 规模大数格式化:万 / 亿。"""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    x = float(x)
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}亿"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.1f}万"
    return f"{x:.0f}"


def _fmt_volume(x) -> str:
    """成交量格式化(单位为手):万手 / 亿手。"""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    x = float(x)
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f}亿手"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.1f}万手"
    return f"{x:.0f}手"


def _structure_block(conn: sqlite3.Connection, cfg) -> str:
    """结构 / 信号块(阶段6 分层):结构状态 + 近端位(支撑/压力)+ 当前信号。"""
    from .signals.analyze import analyze

    lines = ["| 代码 | 结构 | 近支撑 | 近压力 | 当前信号 | 说明 |", "|---|---|---|---|---|---|"]
    for item in cfg.watchlist:
        symbol = str(item["symbol"])
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, volume, turnover FROM etf_kline_daily "
            "WHERE symbol=? ORDER BY trade_date",
            conn, params=(symbol,),
        )
        if df.empty:
            lines.append(f"| {symbol} | 无数据 | - | - | - | 请先 daily |")
            continue
        res = analyze(df)
        st = res["structure"]["state"]["state"]
        sup = _fmt(res.get("support")) if res.get("support") else "-"
        resv = _fmt(res.get("resistance")) if res.get("resistance") else "-"
        note = (res.get("note") or "")[:50]
        if len(note) == 50:
            note = note.rstrip("| ") + "…"
        disp = _display_signal_short(res)
        lines.append(f"| {symbol} | {st} | {sup} | {resv} | {disp} | {note} |")
    return "\n".join(lines)


def _display_signal_short(res: dict) -> str:
    """日报用信号显示:价在关键位显示潜在买/卖点;否则显示观望(带结构线索)。"""
    from .signals.signals import display_signal

    disp = display_signal(res)
    return disp[:28]


_VP_FLAG_LABELS = (
    ("volume_stall", "量增价不涨"), ("high_turnover", "高换手"),
    ("macd_top_div", "MACD顶背离"), ("m_head_break", "M头破位"),
    ("top_divergence", "缩量新高"), ("bottom_divergence", "底背离"),
    ("wave_decline", "逐波递减"), ("double_test", "二踩确认"),
    ("round_bottom", "圆弧底"), ("round_top", "圆弧顶"),
    ("sky_volume", "天量"), ("floor_volume", "地量"),
    ("is_vol_up", "放量"), ("is_vol_down", "缩量"),
    ("range_disorder", "量价震荡"), ("irregular_volume", "不规则放量"),
)


def _vp_state(vp: dict) -> str:
    """量价状态分档:禁确认(量价信号不可信)> 预警(卖向)> 观察(买向)> 中性。"""
    if vp.get("range_disorder") or vp.get("irregular_volume"):
        return "⚠️ 禁确认"
    if any(vp.get(k) for k in (
        "volume_stall", "high_turnover", "macd_top_div", "m_head_break",
        "round_top", "wave_decline")):
        return "预警"
    if any(vp.get(k) for k in ("double_test", "round_bottom", "coil", "bottom_divergence")):
        return "观察"
    return "中性"


def _sector_latest(conn: sqlite3.Connection, symbol: str) -> dict | None:
    """最新一条板块成交占比(同花顺);无数据返回 None。"""
    row = conn.execute(
        """
        SELECT sector, sector_amount, market_amount, ratio_pct, level
        FROM sector_turnover WHERE symbol = ?
        ORDER BY trade_date DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def _vp_block(conn: sqlite3.Connection, cfg) -> str:
    """量价状态块(阶段3):每只 ETF 的量比 / 换手 / 量价状态 / 板块占比 / 关键旗标。"""
    from .signals.analyze import analyze

    lines = [
        "| 代码 | 名称 | 量比 | 换手 | 量价状态 | 板块占比 | 关键旗标 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in cfg.watchlist:
        symbol = str(item["symbol"])
        df = _daily_series(conn, symbol)
        if df.empty:
            lines.append(f"| {symbol} | {item.get('name', '')} | - | - | 无数据 | - | 请先 daily |")
            continue
        vp = analyze(df).get("vp", {})
        flags = [label for name, label in _VP_FLAG_LABELS if vp.get(name)]
        if vp.get("coil"):
            flags.append("上沿带量" if vp.get("coil_probe") else "收敛预备")
        vr = vp.get("vol_ratio")
        tier = vp.get("turnover_tier", "-")
        sc = _sector_latest(conn, symbol)
        if sc and sc.get("ratio_pct") is not None:
            mark = "⚠️" if sc["level"] in ("警戒", "强警示") else ""
            sector_col = f"{_fmt(sc['ratio_pct'], 1)}%{mark}({sc.get('sector', '')})"
        else:
            sector_col = "-"
        lines.append(
            "| {sym} | {name} | {vr} | {tier} | {state} | {sector} | {flags} |".format(
                sym=symbol, name=item.get("name", ""),
                vr=_fmt(vr, 2) if vr is not None else "-",
                tier=tier, state=_vp_state(vp),
                sector=sector_col,
                flags="/".join(flags) if flags else "-",
            )
        )
    return "\n".join(lines)


def _news_block(conn: sqlite3.Connection, cfg) -> str:
    """消息面块(阶段4):当日全市场异动/热榜 + 本池板块归属摘要。"""
    latest = conn.execute("SELECT MAX(trade_date) FROM catalyst_anomaly").fetchone()[0]
    if not latest:
        return "_消息面数据未入库(运行 `alphaprism catalyst` 或 `alphaprism daily`)。_"
    total = conn.execute(
        "SELECT count(*) n FROM catalyst_anomaly WHERE trade_date = ?", (latest,)
    ).fetchone()["n"]
    top_hot = conn.execute(
        "SELECT name, rank FROM catalyst_hot WHERE trade_date = ? ORDER BY rank LIMIT 3",
        (latest,),
    ).fetchall()
    hot_s = ", ".join(f"{r['name']}(#{r['rank']})" for r in top_hot) if top_hot else "-"

    lines = [
        f"全市场当日异动 **{total}** 条;热榜 Top3:{hot_s}",
        "",
        "| 代码 | 名称 | 板块异动(成分股) | 板块热榜 |",
        "|---|---|---|---|",
    ]
    any_sector = False
    for item in cfg.watchlist:
        symbol = str(item["symbol"])
        arows = conn.execute(
            "SELECT stock_name, tag_name FROM catalyst_anomaly "
            "WHERE trade_date = ? AND symbol = ? ORDER BY tag_name", (latest, symbol),
        ).fetchall()
        hrows = conn.execute(
            "SELECT name, rank FROM catalyst_hot "
            "WHERE trade_date = ? AND symbol = ? ORDER BY rank", (latest, symbol),
        ).fetchall()
        tags: dict[str, int] = {}
        for r in arows:
            tags[r["tag_name"]] = tags.get(r["tag_name"], 0) + 1
        a_s = "/".join(f"{k}×{v}" for k, v in sorted(tags.items())) if tags else "无"
        h_s = "/".join(f"{r['name']}#{r['rank']}" for r in hrows) if hrows else "无"
        if arows or hrows:
            any_sector = True
        lines.append(f"| {symbol} | {item.get('name', '')} | {a_s} | {h_s} |")
    if not any_sector:
        lines.append("| — | 今日本池板块无成分股异动/上榜 | - | - |")
    return "\n".join(lines)


def _latest_daily(conn: sqlite3.Connection, symbol: str) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM etf_kline_daily
        WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def _daily_series(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM etf_kline_daily WHERE symbol = ? ORDER BY trade_date",
        conn,
        params=(symbol,),
    )


def _volume_ratio(series: pd.DataFrame) -> float | None:
    """量比:最新量 / 前 20 日均量(排除最新)。"""
    if len(series) < 21:
        return None
    latest = series["volume"].iloc[-1]
    base = series["volume"].iloc[-21:-1].mean()
    if not base or pd.isna(base) or base <= 0:
        return None
    return float(latest / base)


def _position_in_60d(series: pd.DataFrame) -> tuple[float | None, float | None]:
    """距 60 日区间高低的位置(0~1 区间内归一化,越接近 1 越近区间顶)。"""
    if len(series) < 2:
        return None, None
    win = series.tail(60)
    hi = win["high"].max()
    lo = win["low"].min()
    close = series["close"].iloc[-1]
    if hi == lo or pd.isna(hi) or pd.isna(lo) or pd.isna(close):
        return None, None
    return float((close - lo) / (hi - lo)), None


def _max_gap_days(series: pd.DataFrame, window: int = 60) -> int:
    """最近 window 根内的最大日历间隔天数(交易日间)。"""
    dates = pd.to_datetime(series["trade_date"].tail(window))
    if len(dates) < 2:
        return 0
    gaps = dates.diff().dt.days.dropna()
    return int(gaps.max()) if len(gaps) else 0


def _count_anomalies(series: pd.DataFrame) -> dict:
    """数据异常计数:NaN、高低收错位、非正成交量、涨跌幅与收盘不符。"""
    s = series.copy()
    s["trade_date"] = pd.to_datetime(s["trade_date"])
    n = len(s)
    out = {"nan": 0, "hl_invert": 0, "hl_contain": 0, "vol_le0": 0, "pct_mismatch": 0}
    if n == 0:
        return out
    for _, r in s.iterrows():
        o, h, l, c, v, pct = r["open"], r["high"], r["low"], r["close"], r["volume"], r["pct_chg"]
        if any(pd.isna(x) for x in (o, h, l, c, v)):
            out["nan"] += 1
            continue
        if h < l:
            out["hl_invert"] += 1
        if h < max(o, c) or l > min(o, c):
            out["hl_contain"] += 1
        if v <= 0:
            out["vol_le0"] += 1
    # 涨跌幅校验(容忍偏差)
    prev_close = s["close"].shift(1)
    expect = (s["close"] / prev_close - 1) * 100
    diff = (expect - s["pct_chg"]).abs()
    out["pct_mismatch"] = int((diff > _ANOMALY_TOL_PCT).sum())
    return out


def _status_table(conn: sqlite3.Connection, cfg) -> str:
    lines = [
        "| 代码 | 名称 | 收盘 | 涨跌幅 | 成交量 | 成交额 | 换手率 | 量比 | 60日位置 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in cfg.watchlist:
        symbol = str(item["symbol"])
        series = _daily_series(conn, symbol)
        latest = _latest_daily(conn, symbol)
        if latest is None:
            lines.append(f"| {symbol} | {item.get('name', '')} | 无数据 | - | - | - | - | - | - |")
            continue
        pos, _ = _position_in_60d(series)
        lines.append(
            "| {sym} | {name} | {close} | {pct} | {vol} | {amount} | {turn} | {vr} | {pos} |".format(
                sym=symbol,
                name=item.get("name", ""),
                close=_fmt(latest["close"], 3),
                pct=_fmt(latest["pct_chg"], 2) + "%",
                vol=_fmt_volume(latest["volume"]),
                amount=_fmt_vol(latest["amount"]),
                turn=(_fmt(latest["turnover"], 2) + "%") if latest["turnover"] is not None else "-",
                vr=_fmt(_volume_ratio(series), 2),
                pos=_fmt(pos, 2) if pos is not None else "-",
            )
        )
    return "\n".join(lines)


def _quality_block(conn: sqlite3.Connection, cfg) -> str:
    lines = [
        "| 代码 | 行数 | 最新交易日 | 最大间隔(天) | NaN | 高低错位 | 高低包含 | 量≤0 | 涨跌幅偏差 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in cfg.watchlist:
        symbol = str(item["symbol"])
        series = _daily_series(conn, symbol)
        latest = series["trade_date"].iloc[-1] if len(series) else "-"
        if len(series) == 0:
            lines.append(f"| {symbol} | 0 | 无数据 | - | - | - | - | - | - |")
            continue
        anom = _count_anomalies(series)
        gap = _max_gap_days(series)
        flag = " ⚠️" if gap > _HOLIDAY_GAP_THRESHOLD_DAYS else ""
        lines.append(
            "| {sym} | {n} | {last} | {gap}{flag} | {nan} | {hi} | {hc} | {v} | {pm} |".format(
                sym=symbol,
                n=len(series),
                last=latest,
                gap=gap,
                flag=flag,
                nan=anom["nan"],
                hi=anom["hl_invert"],
                hc=anom["hl_contain"],
                v=anom["vol_le0"],
                pm=anom["pct_mismatch"],
            )
        )
    return "\n".join(lines)


def build_daily_report(conn: sqlite3.Connection, cfg, summary: list[dict] | None = None) -> str:
    """生成盘后日报 Markdown 文本。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    latest = conn.execute("SELECT MAX(trade_date) FROM etf_kline_daily").fetchone()[0]
    summary = summary or []

    parts = [
        "# AlphaPrism 盘后简报(数据版)",
        "",
        f"> 生成:{now}  |  数据最新交易日:{latest}",
        "",
        "## 一、跟踪池状态",
        "",
        _status_table(conn, cfg),
        "",
        "> 注:成交额 / 换手率来自 Wind 补充(全历史,每只每轮 1 次调用,计入每日积分)。",
        "",
        "## 二、数据质量",
        "",
        _quality_block(conn, cfg),
        "",
        "## 三、抓取状态",
        "",
    ]
    if summary:
        lines = ["| 代码 | 名称 | 日线 | 周线 | 30分 | 状态 |", "|---|---|---|---|---|---|"]
        for r in summary:
            status = "⚠️ " + "; ".join(r["errors"]) if r["errors"] else "ok"
            lines.append(
                "| {sym} | {name} | {d} | {w} | {m} | {st} |".format(
                    sym=r["symbol"], name=r["name"],
                    d=r["daily"], w=r["weekly"], m=r["m30"], st=status,
                )
            )
        parts.extend(lines)
    else:
        parts.append("_本次未抓取(仅读库)。_")
    parts.extend(
        [
            "",
            "## 四、结构 / 信号",
            "",
            _structure_block(conn, cfg),
            "",
            "> 注:支撑/压力为近端位(近60日前高前低);信号为「潜在买点/卖点」时需「量价确认」"
            "(放量反包 / 放量滞涨)后才操作;跌破近端支撑或结构支撑则失效。",
            "",
            "## 五、量价状态",
            "",
            _vp_block(conn, cfg),
            "",
            "> 量价为共振/确认层,不独立开仓。「禁确认」= 区间量价无序或存在不规则放量,"
            "此期间量价信号不可信,只信纯结构信号(如黄金 ETF 长期区间盘整即属此类)。"
            "「预警」含量增价不涨/高换手/MACD顶背离/M头破位/圆弧顶/逐波递减;"
            "「观察」含二踩确认/圆弧底/收敛预备/底背离。"
            "「板块占比」= 板块成交额/全市场成交额(同花顺行业指数),≥15% 警戒、≥20% 强警示(板块拥挤)。",
            "",
            "## 六、消息面(当日异动/热榜)",
            "",
            _news_block(conn, cfg),
            "",
            "> 异动/热榜来源:同花顺。催化剂状态(增强/减弱/新增/未变)由 LLM 解读,"
            "见盘前简报(流程见 docs/盘前简报流程.md)。",
            "",
        ]
    )
    return "\n".join(parts)


def write_report(report_dir: str, text: str, trade_date: str) -> Path:
    """写 reports/YYYY-MM-DD.md,返回文件路径。"""
    if not Path(report_dir).is_absolute():
        out = PROJECT_ROOT / report_dir
    else:
        out = Path(report_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{trade_date}.md"
    path.write_text(text, encoding="utf-8")
    return path
