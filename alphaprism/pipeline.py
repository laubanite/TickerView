"""抓取编排:逐只抓取 → 入库,写 fetch_log,单只失败不中断整体。"""
from __future__ import annotations

import logging

import pandas as pd

from .db import (
    SIGNAL_LOG_COLUMNS,
    log_fetch,
    sync_watchlist,
    upsert_etf,
    upsert_rows,
)
from .fetchers import etf_kline, fuyao, wind_enrich
from .net import apply_network_policy
from .signals.fractal import process as _fractal_process
from .signals.signals import classify_signal, detect_signal

logger = logging.getLogger(__name__)


def _today_start(years: int) -> str:
    start = pd.Timestamp.today().normalize() - pd.DateOffset(years=years, days=5)
    return start.strftime("%Y-%m-%d")


def _today_end() -> str:
    return pd.Timestamp.today().normalize().strftime("%Y-%m-%d")


def _merge_wind_enrich(conn, symbol: str, df_daily, start: str, end: str) -> pd.DataFrame:
    """用 Wind 补全历史(或窗口)的成交额/换手率;失败降级(保持原值),不中断抓取。

    Wind get_fund_kline 一次返回全部区间(实测 3 年 730 行无上限);
    成本=每只每轮 1 次调用,计入 Wind 每日 1000 积分额度。
    """
    if df_daily is None or df_daily.empty:
        return df_daily
    begin = start.replace("-", "")
    end_date = end.replace("-", "")
    try:
        wdf = wind_enrich.fetch_daily_amount_turnover(symbol, begin, end_date)
        if wdf.empty:
            return df_daily
        out = df_daily.copy()
        amap = dict(zip(wdf["trade_date"], wdf["amount"]))
        tmap = dict(zip(wdf["trade_date"], wdf["turnover"]))
        out["amount"] = out["trade_date"].map(amap)
        out["turnover"] = out["trade_date"].map(tmap)
        n_filled = int(out["amount"].notna().sum())
        logger.info("[%s] Wind 补充成交额/换手率 %d 天 (%s ~ %s)", symbol, n_filled,
                    wdf["trade_date"].min(), wdf["trade_date"].max())
        log_fetch(conn, "wind", f"enrich:{symbol}", start, end, n_filled, "ok")
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Wind 补充失败(降级,保持原值): %s", symbol, exc)
        log_fetch(conn, "wind", f"enrich:{symbol}", start, end, None, "error", str(exc))
        return df_daily


def fetch_symbol(conn, cfg, item: dict) -> dict:
    """抓取单只 ETF 全部启用数据源并入库,返回结果摘要。"""
    symbol = str(item["symbol"])
    name = str(item.get("name", ""))
    category = str(item.get("category", ""))
    result = {
        "symbol": symbol,
        "name": name,
        "daily": 0,
        "weekly": 0,
        "m30": 0,
        "errors": [],
    }
    upsert_etf(conn, symbol, name, category)

    start = _today_start(cfg.get("daily_history_years", default=3))
    end = _today_end()
    df_daily = None

    # 日线:腾讯前复权 + 新浪补当日 → Wind 补成交额/换手率(全历史) → 整窗覆盖
    try:
        df_daily = etf_kline.fetch_daily(symbol, start, end)
        if cfg.source_enabled("wind_amount"):
            df_daily = _merge_wind_enrich(conn, symbol, df_daily, start, end)
        rows = etf_kline.daily_to_rows(df_daily, symbol)
        n = upsert_rows(conn, "etf_kline_daily", etf_kline.DAILY_COLUMNS, rows)
        result["daily"] = n
        log_fetch(conn, "tencent", f"daily:{symbol}", start, end, n, "ok")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"daily: {exc}")
        log_fetch(conn, "tencent", f"daily:{symbol}", start, end, None, "error", str(exc))

    # 周线:由本次日线重采样(与日线同源,一致性最好)
    if df_daily is not None and not df_daily.empty:
        try:
            wdf = etf_kline.build_weekly(df_daily)
            wrows = etf_kline.weekly_to_rows(wdf, symbol)
            n = upsert_rows(conn, "etf_kline_weekly", etf_kline.WEEKLY_COLUMNS, wrows)
            result["weekly"] = n
            log_fetch(conn, "tencent", f"weekly:{symbol}", start, end, n, "ok")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"weekly: {exc}")
            log_fetch(conn, "tencent", f"weekly:{symbol}", None, None, None, "error", str(exc))
    else:
        result["errors"].append("weekly: 日线为空,跳过")
        log_fetch(conn, "tencent", f"weekly:{symbol}", None, None, None, "error", "日线为空")

    # 30分钟线:窗口内全量覆盖
    try:
        mdf = etf_kline.fetch_30m(symbol)
        mrows = etf_kline.min_to_rows(mdf, symbol)
        n = upsert_rows(conn, "etf_kline_30m", etf_kline.MIN30_COLUMNS, mrows)
        result["m30"] = n
        log_fetch(conn, "tencent", f"30m:{symbol}", None, None, n, "ok")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"30m: {exc}")
        log_fetch(conn, "tencent", f"30m:{symbol}", None, None, None, "error", str(exc))

    conn.commit()
    return result


def run_pipeline(conn, cfg, symbols: list[str] | None = None) -> list[dict]:
    """抓取全部(或指定)跟踪池,返回逐只结果。单只失败不中断整体。"""
    apply_network_policy(bool(cfg.get("network", "no_proxy", default=True)))
    items = [w for w in cfg.watchlist if not symbols or str(w["symbol"]) in symbols]
    results = []
    for item in items:
        res = fetch_symbol(conn, cfg, item)
        results.append(res)
        if res["errors"]:
            logger.warning("[%s] 部分失败: %s", res["symbol"], " | ".join(res["errors"]))
    sync_watchlist(conn, [str(w["symbol"]) for w in cfg.watchlist])

    # 板块成交占比 + 催化剂数据(同花顺):失败降级不中断
    if cfg.source_enabled("fuyao"):
        refresh_sector_crowding(conn, cfg, symbols)
        refresh_catalyst(conn, cfg, symbols)
    conn.commit()
    return results


def refresh_sector_crowding(conn, cfg, symbols: list[str] | None = None) -> None:
    """抓取板块成交占比并入库(最新交易日)。失败降级不中断,供 daily 与 sector 命令共用。"""
    try:
        crowd = fuyao.sector_crowding(cfg)
        if symbols:
            crowd = [c for c in crowd if c["symbol"] in symbols]
        if not crowd:
            return
        trade_date = conn.execute("SELECT MAX(trade_date) FROM etf_kline_daily").fetchone()[0]
        rows = [
            (c["symbol"], trade_date, c["sector"], c["sector_amount"], c["market_amount"],
             c["ratio_pct"], c["level"])
            for c in crowd
        ]
        n = upsert_rows(conn, "sector_turnover", fuyao.SECTOR_COLUMNS, rows)
        logger.info("同花顺板块成交占比 %d 只(%s)", n, trade_date)
        log_fetch(conn, "fuyao", "sector-crowding", None, None, n, "ok")
    except Exception as exc:  # noqa: BLE001
        logger.warning("同花顺板块成交占比失败(降级,不中断): %s", exc)
        log_fetch(conn, "fuyao", "sector-crowding", None, None, None, "error", str(exc))


def refresh_catalyst(conn, cfg, symbols: list[str] | None = None) -> dict | None:
    """催化剂数据(同花顺异动+热榜)抓取入库。失败返回 None,不中断。

    全市场异动/热榜按板块成分股归属到各 ETF(symbol 列),日报消息面区按 symbol 读取。
    """
    try:
        ctx = fuyao.catalyst_context(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("同花顺催化剂数据失败(降级,不中断): %s", exc)
        log_fetch(conn, "fuyao", "catalyst", None, None, None, "error", str(exc))
        return None
    trade_date = conn.execute("SELECT MAX(trade_date) FROM etf_kline_daily").fetchone()[0]
    matched = ctx["matched"]

    a_rows = [
        (trade_date, a.get("thscode"), a.get("stock_name"), a.get("tag_name"),
         "/".join(a.get("keyword_list") or []), a.get("analysis_content"),
         matched.get(a.get("thscode")))
        for a in ctx["anomalies"]
    ]
    h_rows = [
        (trade_date, h.get("thscode"), h.get("name"), h.get("rank"), h.get("heat"),
         h.get("rank_change"), matched.get(h.get("thscode")))
        for h in ctx["hot"]
    ]
    n1 = upsert_rows(conn, "catalyst_anomaly", fuyao.CATALYST_ANOMALY_COLUMNS, a_rows)
    n2 = upsert_rows(conn, "catalyst_hot", fuyao.CATALYST_HOT_COLUMNS, h_rows)
    logger.info("同花顺催化剂:异动 %d 条 / 热榜 %d 条(%s)", n1, n2, trade_date)
    log_fetch(conn, "fuyao", "catalyst", None, None, n1 + n2, "ok")
    return ctx


def refresh_signal_log(conn, cfg, symbols: list[str] | None = None) -> list[dict]:
    """信号日志(§6.4):逐只 ETF 从上次记录处增量检测 出现/激活/失效,写入 signal_log。

    首次运行自动回填全历史(从第一根 K 线开始)。返回本次新增的事件(供推送强提醒)。
    状态判定(分层关键位 2026-08-17):setup 由近端位触发(回踩支撑/触及压力),价格贴近即记录;
    跌破近端/结构支撑或突破近端/结构压力记失效。
    """
    events: list[dict] = []
    for item in cfg.watchlist:
        sym = str(item["symbol"])
        if symbols and sym not in symbols:
            continue
        df = pd.read_sql_query(
            "SELECT trade_date, open, high, low, close, volume, turnover FROM etf_kline_daily "
            "WHERE symbol=? ORDER BY trade_date", conn, params=(sym,))
        if len(df) < 30:
            continue

        # 上次记录到哪天 → 从那天的信号上下文继续(增量)
        last_row = conn.execute(
            "SELECT trade_date, kind, signal, state FROM signal_log "
            "WHERE symbol=? ORDER BY id DESC LIMIT 1", (sym,),
        ).fetchone()
        start_idx = 0
        seed = None
        if last_row is not None:
            pos = df.index[df["trade_date"] == last_row["trade_date"]]
            if len(pos):
                start_idx = int(pos[0])
                if last_row["kind"] != "fail":
                    seed = (last_row["signal"], last_row["state"])
        if start_idx >= len(df) - 1:
            continue

        # 已存在的 (date, kind) 去重
        existing = {
            f"{r['trade_date']}|{r['kind']}"
            for r in conn.execute("SELECT trade_date, kind FROM signal_log WHERE symbol=?", (sym,)).fetchall()
        }

        def emit(row: tuple) -> None:
            nonlocal existing
            key = f"{row[0]}|{row[1]}"
            if key in existing:
                return
            existing.add(key)
            full = (row[0], sym, row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8])
            upsert_rows(conn, "signal_log", SIGNAL_LOG_COLUMNS, [full])
            events.append(_ev(sym, row))

        proc = _fractal_process(df)
        strokes, fractals = proc["strokes"], proc["fractals"]
        active = seed
        for i in range(start_idx, len(df)):
            sig = detect_signal(df, strokes, i, fractals)
            cs, st = classify_signal(sig)
            date = str(df["trade_date"].iloc[i])
            close = float(df["close"].iloc[i])
            sup = sig.get("support")
            res = sig.get("resistance")
            note = sig.get("note") or ""

            if cs == "观望":
                if active is not None:
                    emit((date, "fail", active[0], "失效", close, sup, res,
                          f"{active[0]}信号失效:价已远离关键位/结构变化", None))
                    active = None
            elif st == "破位":
                if active is not None and active[0] == cs:
                    reason = "跌破支撑" if cs == "买点" else "站上压力"
                    emit((date, "fail", cs, "失效", close, sup, res, reason, None))
                    active = None
                # 无活动信号时的破位=普通观望,不记录
            else:  # 观察 / 激活
                if active is None:
                    emit((date, "appear", cs, st, close, sup, res, note, None))
                elif active[0] == cs and active[1] != st:
                    emit((date, "activate", cs, st, close, sup, res, note, None))
                elif active[0] != cs:
                    emit((date, "fail", active[0], "失效", close, sup, res,
                          f"{active[0]}信号结束(结构变为{cs})", None))
                    emit((date, "appear", cs, st, close, sup, res, note, None))
                active = (cs, st)
    conn.commit()
    if events:
        kinds = ",".join({e["kind"] for e in events})
        logger.info("信号日志新增 %d 条(%s)", len(events), kinds)
    return events


def _ev(sym: str, row: tuple) -> dict:
    """日志行 → 事件 dict(推送用)。row: (date, kind, signal, state, price, support, resistance, note, result)"""
    return {
        "symbol": sym,
        "trade_date": row[0],
        "kind": row[1],
        "signal": row[2],
        "state": row[3],
        "price": row[4],
        "support": row[5],
        "resistance": row[6],
        "note": row[7],
    }
