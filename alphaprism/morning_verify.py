"""催化判定 N 日验证(方案 M4/M5):纯确定性规则。

判定(important_news 带 impact/confidence)→ N 个交易日后,用板块日线 + 上证指数 + 关键位
判 应验/部分应验/未应验/无法判定,写 catalyst_verification。
聚合 verification_stats 供先验注入(M5)与信息层评估(M6)。

验证规则(方案 3.3.2):
  方向信号   N 日后收盘 vs 判定日收盘(利好期望涨 / 利空期望跌)
  超额信号   板块N日涨幅 − 上证N日涨幅(利好期望 > +1% / 利空 < −1%)
  关键位     利好期望上穿突破点 / 利空期望下穿减仓红线或生命线
  应验       方向对 且(关键位成立 或 超额成立)
  部分应验   方向对但幅度弱 | 未应验 方向错 | 无法判定 中性/无数据/未到期
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .config import Config
from .db import connect
from .fetchers.etf_kline import fetch_index_daily

logger = logging.getLogger(__name__)

EXCESS_THRESHOLD = 0.01          # 超额 ±1%(可配)
N_DAYS = (3, 20)
INDEX_BENCH = "000001.SH"        # 上证指数作大盘基准


def _daily_closes(conn, symbol: str) -> dict[str, float]:
    """板块日线收盘价 {trade_date: close}。"""
    if not symbol:
        return {}
    rows = conn.execute(
        "SELECT trade_date, close FROM etf_kline_daily WHERE symbol=? ORDER BY trade_date",
        (symbol,)).fetchall()
    return {r["trade_date"]: r["close"] for r in rows}


def _next_nth_close(closes: dict[str, float], base_date: str, n: int) -> tuple[str | None, float | None]:
    """base_date 之后第 n 个交易日的 (date, close);样本不足返回 (None, None)。"""
    dates = sorted(d for d in closes if d > base_date)
    if len(dates) < n:
        return None, None
    d = dates[n - 1]
    return d, closes[d]


def _load_model(cfg: Config):
    """解析作战地图(取关键位)。失败返回 None(关键位信号降级为无)。"""
    from .planner.parser import parse_file as parse_battlemap

    path = cfg.get("battlemap", "path", default="")
    if not path:
        import glob
        cands = sorted(glob.glob(r"E:\AITrader\七只ETF作战地图_*.md"), reverse=True)
        path = cands[0] if cands else ""
    if not path:
        return None
    try:
        return parse_battlemap(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("作战地图解析失败(关键位信号降级为无): %s", exc)
        return None


def _level_prices(model, symbol: str) -> dict[str, float]:
    """作战地图关键位:突破点(上)/减仓红线、生命线(下)。"""
    if model is None:
        return {}
    for inst in model.instruments:
        if inst.code != symbol:
            continue
        out: dict[str, float] = {}
        for l in inst.levels:
            if l.name in ("突破点", "减仓红线", "生命线"):
                out.setdefault(l.name, l.price)
        return out
    return {}


def _bench_closes(conn, cfg: Config, news_rows: list, n_days) -> dict[str, float]:
    """上证指数日线收盘(窗口:最早判定日 −10 天 → 今天)。失败返回空(超额降级)。"""
    if not news_rows:
        return {}
    dates = [r["trade_date"] for r in news_rows]
    start = (datetime.strptime(min(dates), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    try:
        df = fetch_index_daily(INDEX_BENCH, start, end)
        if df.empty:
            return {}
        return {r.trade_date: r.close for r in df.itertuples()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("上证指数日线获取失败(超额信号降级为无): %s", exc)
        return {}


def _classify(impact: str, p0: float, pn: float, excess: float | None,
              level_prices: dict[str, float], win_closes: list[float]) -> tuple[str, str]:
    """综合 方向/超额/关键位 三信号 → (outcome, evidence)。win_closes:窗口内各交易日收盘。"""
    if impact == "中性":
        return "无法判定", "中性消息无预期方向"
    up = impact == "利好"
    dir_ok = (pn > p0) if up else (pn < p0)
    # 关键位信号:利好期望上穿突破点;利空期望下穿减仓红线/生命线
    key_hit = False
    if up and level_prices.get("突破点"):
        key_hit = any(c > level_prices["突破点"] for c in win_closes)
    elif not up and (level_prices.get("减仓红线") or level_prices.get("生命线")):
        lvl = level_prices.get("减仓红线") or level_prices["生命线"]
        key_hit = any(c < lvl for c in win_closes)
    # 超额信号
    if excess is not None:
        exc_ok = (excess > EXCESS_THRESHOLD) if up else (excess < -EXCESS_THRESHOLD)
    else:
        exc_ok = False
    if not dir_ok:
        return "未应验", f"方向错:判定日 {p0:.3f} → N日 {pn:.3f}"
    if key_hit or exc_ok:
        parts = [f"{p0:.3f}→{pn:.3f}"]
        if key_hit:
            parts.append("关键位突破")
        if exc_ok:
            parts.append(f"超额{excess * 100:+.1f}%")
        return "应验", "、".join(parts)
    return "部分应验", f"方向对但幅度弱({p0:.3f}→{pn:.3f})"


def verify_pending(conn, cfg: Config | None = None, model=None, n_days=N_DAYS) -> dict:
    """对所有到期未验证的 important_news 跑 N 日验证,写 catalyst_verification。

    返回 {"verified": 本次新增验证条数, "n_news": 消息总数}。幂等:已验证的跳过。
    """
    cfg = cfg or Config()
    if model is None:
        model = _load_model(cfg)
    news_rows = conn.execute(
        "SELECT id, trade_date, sector, symbol, impact, confidence, type "
        "FROM important_news ORDER BY trade_date").fetchall()
    if not news_rows:
        return {"verified": 0, "n_news": 0}
    done = {(r["news_id"], r["n_day"])
            for r in conn.execute("SELECT news_id, n_day FROM catalyst_verification").fetchall()}
    closes_by_sym = {nr["symbol"]: _daily_closes(conn, nr["symbol"])
                     for nr in news_rows if nr["symbol"]}
    bench = _bench_closes(conn, cfg, news_rows, n_days)

    verified = 0
    for nr in news_rows:
        closes = closes_by_sym.get(nr["symbol"]) or {}
        p0 = closes.get(nr["trade_date"])
        if p0 is None:
            continue  # 判定日无K线(大盘/其他板块),无法验证
        lv = _level_prices(model, nr["symbol"])
        for n in n_days:
            if (nr["id"], n) in done:
                continue
            end_date, pn = _next_nth_close(closes, nr["trade_date"], n)
            if end_date is None:
                continue  # 未到期
            win = [c for d, c in sorted(closes.items()) if nr["trade_date"] < d <= end_date]
            excess = None
            b0, bn = bench.get(nr["trade_date"]), bench.get(end_date)
            if b0 and bn:
                excess = (pn / p0 - 1) - (bn / b0 - 1)
            outcome, evidence = _classify(nr["impact"], p0, pn, excess, lv, win)
            conn.execute(
                "INSERT OR REPLACE INTO catalyst_verification "
                "(news_id, n_day, outcome, evidence, excess, verified_at) "
                "VALUES (?,?,?,?,?, datetime('now','localtime'))",
                (nr["id"], n, outcome, evidence, excess))
            verified += 1
    conn.commit()
    return {"verified": verified, "n_news": len(news_rows)}


def aggregate_stats(conn, n_day: int = 20) -> dict:
    """verification_stats(方案 M5/M6):按 type/confidence 分层 + 信息层 alpha 曲线。

    返回 {"by_type": [...], "by_confidence": [...], "alpha_curve": [[date, cum]], "total": {...}}
    """
    rows = conn.execute(
        "SELECT v.outcome, v.excess, v.n_day, i.id, i.sector, i.impact, i.confidence, i.type, i.trade_date "
        "FROM catalyst_verification v JOIN important_news i ON i.id = v.news_id "
        "WHERE v.n_day=? AND i.impact != '中性' ORDER BY i.trade_date, i.id", (n_day,)).fetchall()

    def bucket(agg, r, keyfn):
        k = keyfn(r) or "其他"
        d = agg.setdefault(k, {"n": 0, "hit": 0, "partial": 0, "miss": 0, "na": 0})
        d["n"] += 1
        d[{"应验": "hit", "部分应验": "partial", "未应验": "miss"}.get(r["outcome"], "na")] += 1
        return d

    by_type: dict = {}
    by_conf: dict = {}
    cum, alpha_curve = 0.0, []
    for r in rows:
        bucket(by_type, r, lambda x: x["type"])
        bucket(by_conf, r, lambda x: x["confidence"])
        if r["excess"] is not None:
            cum += r["excess"] if r["impact"] == "利好" else -r["excess"]
            alpha_curve.append([r["trade_date"], round(cum, 4)])

    def to_list(buckets):
        out = []
        for k, d in buckets.items():
            rate = d["hit"] / d["n"] if d["n"] else 0
            out.append({"key": k, "n": d["n"], "hit": d["hit"], "partial": d["partial"],
                        "miss": d["miss"], "na": d["na"], "hit_rate": round(rate, 3)})
        return sorted(out, key=lambda x: -x["n"])

    total = {"n": len(rows), "hit": sum(d["hit"] for d in by_type.values()),
             "hit_rate": round(sum(d["hit"] for d in by_type.values()) / len(rows), 3) if rows else 0}
    return {"by_type": to_list(by_type), "by_confidence": to_list(by_conf),
            "alpha_curve": alpha_curve, "total": total}


def prior_prompt(conn, n_day: int = 20, limit: int = 6) -> str:
    """历史应验率先验(方案 M5):按 type 分层,取样本最多的 limit 条,注入 LLM 挑选 prompt。"""
    stats = aggregate_stats(conn, n_day=n_day)
    parts = []
    for t in stats["by_type"][:limit]:
        if t["n"] < 3:
            continue
        parts.append(f"{t['key']}类 {t['n']}样本 应验率{int(t['hit_rate'] * 100)}%")
    return "；".join(parts)


def tuning_report(conn, n_day: int = 20) -> str:
    """关键词调优建议报告(方案 M5,每 2 周,人工闸门)。markdown,机器提议、人工确认后改配置。"""
    rows = keyword_stats(conn, n_day=n_day)
    lines = [f"# 关键词调优建议(近 {n_day} 日,样本下限 10)", "",
             "| 关键词 | 样本 | 应验率 | 应验 | 未应验 | 建议 |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['keyword']} | {r['n']} | {int(r['hit_rate'] * 100)}% | "
                     f"{r['hit']} | {r['miss']} | {r['action']} |")
    if not rows:
        lines.append("| — | 0 | — | — | — | 尚无验证数据 |")
    else:
        lines.append("")
        lines.append("> 建议规则:应验率≥60% 保持/扩宽;25%~60% 收窄;<25% 建议移除;样本<10 暂时不动。")
    lines += ["",
              "> **人工闸门**:以上为机器**建议**,确认后手动修改 `config/settings.yaml news.keywords`;"
              "机器不自动改共享配置(防过拟合、可复核)。"]
    return "\n".join(lines)


def keyword_stats(conn, n_day: int = 20) -> list[dict]:
    """关键词调优建议(方案 M5):每个关键词的 样本/应验率/建议动作 + 依据。

    依据:important_news.keywords(JSON 数组)关联 verification 结果。
    """
    rows = conn.execute(
        "SELECT i.keywords, v.outcome FROM catalyst_verification v "
        "JOIN important_news i ON i.id = v.news_id WHERE v.n_day=?", (n_day,)).fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        for kw in json.loads(r["keywords"] or "[]"):
            d = agg.setdefault(kw, {"n": 0, "hit": 0, "partial": 0, "miss": 0, "na": 0})
            d["n"] += 1
            d[{"应验": "hit", "部分应验": "partial", "未应验": "miss"}.get(r["outcome"], "na")] += 1
    out = []
    for kw, d in agg.items():
        if d["n"] < 10:
            action = "样本不足(暂不调)"
        else:
            rate = d["hit"] / d["n"]
            if rate >= 0.6:
                action = "保持/扩宽"
            elif rate >= 0.25:
                action = "收窄"
            else:
                action = "建议移除"
        out.append({"keyword": kw, "n": d["n"], "hit_rate": round(d["hit"] / d["n"], 3)
                    if d["n"] else 0, "hit": d["hit"], "miss": d["miss"], "action": action})
    return sorted(out, key=lambda x: -x["n"])