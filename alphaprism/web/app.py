"""Web 行情页后端(Flask,里程碑6)。

复用现有 Python 引擎,通过 REST API 供 Vue 前端调用:
- /api/indices      指数实时(上证/科创/深成/创业板)
- /api/watchlist    自选股:实时行情 + 核对引擎状态灯
- /api/quote        单只实时行情
- /api/kline        K线(日K/30分/分时)
- /api/morning      盘前市场状态灯 + 隔夜消息
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ..config import Config
from ..fetchers import fuyao
from ..fetchers.etf_kline import fetch_30m, fetch_daily, fetch_name
from ..fetchers.stock_qt import fetch_stock_snapshot

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent


def _mask_key(v: str) -> str:
    """API key 脱敏:固定长度格式(前4 + 4星 + 后4),如 sk-****abcd。"""
    s = str(v)
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def _signal_type_from_arch(advice_md: str) -> str | None:
    """从深入分析正文提取结论卡 signal_type(与 planner.advice_verify 同口径)。"""
    m = re.search(r"信号类型[:：]\s*([^\s·|]+)", advice_md)
    return m.group(1).strip() if m else None


def _card_summary(advice_md: str, category: str, state_word: str, anchor) -> str:
    """结论卡段截取为日历摘要;无结论卡时用结构化字段兜底。"""
    if advice_md and advice_md.strip():
        lines = advice_md.splitlines()
        for i, ln in enumerate(lines):
            if "结论卡" in ln and ln.lstrip().startswith("#"):
                body = [l.strip() for l in lines[i + 1:]
                        if l.strip() and not l.lstrip().startswith("#")]
                text = "\n".join(body[:8]).strip()
                if text:
                    return text[:220]
                break
    anchor_txt = f",锚定价 {anchor}" if anchor is not None else ""
    return f"{category or '建议'} · {state_word or ''}{anchor_txt}".strip(" ·,")


def _one_sentence_from_md(advice_md: str) -> str:
    """从归档 md 提取结论卡「核心判断」一句话;无则空串(前端回退摘要)。"""
    m = re.search(r"核心判断[:：]\s*([^\n]+)", advice_md)
    return m.group(1).strip() if m else ""


def _rebuild_sentence_from_snapshot(snapshot_md: str, state_word: str, anchor) -> str:
    """旧归档无结论卡时,从 snapshot_md 的「操作参数(程序锚点)」行按当前模板
    重算一句话(2026-09)。价位全部来自当时程序锚点行(白名单源,可复核);
    解析失败返回空串。兼容旧 v2 格式(突破/回踩/砍仓/止损)与新格式
    (突破加仓位/回踩加仓位 区间/减仓 跌破/清仓止损)。"""
    if not snapshot_md:
        return ""
    line = ""
    for ln in snapshot_md.splitlines():
        if "操作参数" in ln:
            line = ln
            break
    if not line:
        return ""
    m1 = re.search(r"回踩(?:加仓位)?\s*([\d.]+)(?:-([\d.]+))?", line)
    m2 = re.search(r"突破(?:加仓位)?\s*([\d.]+)", line)
    m3 = re.search(r"减仓\s*跌破\s*([\d.]+)", line) or re.search(r"砍仓\s*([\d.]+)", line)
    m4 = re.search(r"清仓止损\s*([\d.]+)", line) or re.search(r"止损\s*([\d.]+)", line)
    acts = []
    if m1:
        lo, up = m1.group(1), m1.group(2)
        acts.append(f"回踩 {lo}-{up} 观察企稳" if up else f"回踩 {lo} 观察企稳")
    if m2:
        acts.append(f"突破 {m2.group(1)} 需放量+日线收盘")
    if m3:
        acts.append(f"跌破 {m3.group(1)} 减仓")
    if m4:
        acts.append(f"破 {m4.group(1)} 止损")
    if not acts:
        return ""
    head = (f"现价 {float(anchor):.3f} · {state_word}" if anchor else state_word)
    return head + "。" + "；".join(acts) + "。"


def _ensure_verify_klines(conn) -> None:
    """按需补齐建议日之后的日 K(腾讯接口 → 本地 upsert)。只拉 advice_archive 涉及的标的;
    网络失败不阻断(验证保持 无法判定,下次打开再试)。零定时任务、零推送。"""
    from datetime import date, timedelta

    syms = [dict(r) for r in conn.execute(
        "SELECT symbol, MIN(trade_date) t0 FROM advice_archive GROUP BY symbol")]
    if not syms:
        return
    from ..db import upsert_rows
    from ..fetchers.etf_kline import DAILY_COLUMNS, daily_to_rows, fetch_daily

    today = date.today().isoformat()
    for s in syms:
        start = (date.fromisoformat(s["t0"]) - timedelta(days=10)).isoformat()
        try:
            df = fetch_daily(s["symbol"], start, today)
            if df is not None and not df.empty:
                rows = daily_to_rows(df, s["symbol"])
                n = upsert_rows(conn, "etf_kline_daily", DAILY_COLUMNS, rows)
                logger.info("[验证] %s 补拉日K %d 根", s["symbol"], n)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 验证补拉日K失败(保持无法判定): %s", s["symbol"], exc)
    conn.commit()


# 盘中快照缓存(2026-09 用户拍板):深入分析只读最近一次快照缓存,不重复拉实时数据;
# 30 秒内视为新鲜,过期由分析入口自动先刷新再分析(对用户透明)。
_SNAP_CACHE: dict[str, tuple[float, dict]] = {}
SNAP_TTL_SEC = 30.0

# 悬浮面板标记缓存(ETF + 个股统一):facts 接口每次刷新即写入(与返回的 panel 同源)。
# 跨窗口同步的真源:网页看板/浮窗各自拉取(BroadcastChannel 无法跨 WebView 边界),
# 浮窗轮询 /api/snapshot/panel 即可拿到网页端刚刷出的标记。
_PANEL_CACHE: dict[str, dict] = {}   # code -> {panel, time, mode}

# 个股名称缓存(验证日历兜底用;进程生命周期内有效,qt 一次拉取)
_stock_name_cache: dict[str, str | None] = {}


def _deterministic_snapshot(code: str, cfg) -> tuple[dict, str, dict]:
    """拉实时 → 生成确定性建议 → 归档(每天每标的最终方向为准)→ 缓存。

    返回 (facts, 快照 markdown, 确定性建议 dict)。零 LLM,秒级。
    """
    from ..db import connect, init_db
    from ..planner.intraday import (_archive_advice, _facts_markdown,
                                    build_deterministic_advice)

    adv = build_deterministic_advice(code, cfg)
    facts, md = adv["facts"], _facts_markdown(adv["facts"])
    _SNAP_CACHE[code] = (time.time(), facts)
    conn = connect()
    init_db(conn)
    try:
        _archive_advice(code, facts, md, adv["advice_md"], adv["state"],
                        adv["anchors"], adv["catalyst"], adv["category"], False)
    finally:
        conn.close()
    return facts, md, adv


def _panel_payload_from(code: str, adv: dict) -> dict:
    """确定性建议 dict → 悬浮面板标记载荷(与快照/归档同源)。"""
    from ..planner.intraday import _panel_payload
    return _panel_payload(code, adv["facts"], adv["state"], adv["anchors"],
                          adv["catalyst"])


def _stock_mode_enabled(cfg) -> bool:
    """盘中个股模式 feature flag(方案 §4:settings.yaml `intraday.stock_mode`,
    缺省开;出问题可整体关闭且不影响 ETF 路径)。"""
    try:
        return bool(cfg.get("intraday", "stock_mode", default=True))
    except Exception:  # noqa: BLE001
        return True


def _snapshot_kind(code: str, cfg) -> str:
    """快照路由类型:watchlist 显式 type 覆盖 > 代码前缀自动(方案 §4)。"""
    from ..fetchers.stock_qt import stock_type
    try:
        for w in (cfg.watchlist or []):
            if str(w.get("symbol")) == code and w.get("type") in ("etf", "stock"):
                return str(w["type"])
    except Exception:  # noqa: BLE001
        pass
    return stock_type(code)


def create_app() -> Flask:
    from ..paths import ensure_user_dirs

    ensure_user_dirs()  # 冻结态首启建 %APPDATA%\TickerView 并播种出厂 watchlist
    app = Flask(__name__, static_folder=str(WEB_DIR / "static"), static_url_path="/static")
    cfg = Config()

    @app.after_request
    def _no_cache_static(resp):
        """静态资源禁缓存:本地应用无 CDN,改版后浏览器/WebView 拿旧 JS 会出现
        "页面行为与源码对不上"的幽灵 bug(用户怎么试都是老问题,硬刷新才好)。"""
        if request.path.startswith("/static") or request.path == "/":
            resp.headers["Cache-Control"] = "no-cache, max-age=0"
        return resp

    # ------------------------------------------------------------ 静态首页
    @app.route("/")
    def index():
        return send_from_directory(WEB_DIR / "static", "index.html")

    # ------------------------------------------------------------ 指数
    @app.route("/api/indices")
    def api_indices():
        # 上证/深成/创业板/科创综指 用同花顺(科创综指 thscode 000680.SH,2026-08-21 实测可取)
        try:
            snaps = fuyao.fetch_index_snapshot(["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"])
            out = []
            for code, label in (("000001.SH", "上证指数"), ("399001.SZ", "深证成指"),
                                ("399006.SZ", "创业板指"), ("000680.SH", "科创综指")):
                s = snaps.get(code) or {}
                out.append({
                    "name": label, "code": code,
                    "price": s.get("last_price"),
                    "change_pct": s.get("price_change_ratio_pct") or s.get("change_pct"),
                })
            return jsonify({"ok": True, "indices": out})
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数快照失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 自选股(§5.6)
    @app.route("/api/watchlist", methods=["GET", "POST", "DELETE"])
    def api_watchlist():
        from ..config import WATCHLIST_FILE
        import yaml

        def _load():
            if WATCHLIST_FILE.exists():
                return list(yaml.safe_load(WATCHLIST_FILE.read_text(encoding="utf-8")) or [])
            return []

        def _save(items):
            WATCHLIST_FILE.write_text(
                yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")

        try:
            if request.method == "POST":
                data = request.get_json(force=True) or {}
                raw = str(data.get("symbol", "")).strip()
                m = re.match(r"(\d{6})", raw)
                sym = m.group(1) if m else raw
                if not sym:
                    return jsonify({"ok": False, "error": "缺少 symbol"}), 400
                items = _load()
                if any(i["symbol"] == sym for i in items):
                    return jsonify({"ok": True, "duplicated": True})
                stock_name = None
                if _snapshot_kind(sym, cfg) == "stock":
                    stock_name = (fetch_stock_snapshot([sym]).get(sym) or {}).get("name")
                name = data.get("name") or stock_name or fetch_name(sym) or sym
                category = data.get("category") or "行业"
                items.append({"symbol": sym, "name": name, "category": category})
                _save(items)
                return jsonify({"ok": True, "item": {"symbol": sym, "name": name, "category": category}})
            if request.method == "DELETE":
                raw = str(request.args.get("symbol", ""))
                m = re.match(r"(\d{6})", raw)
                sym = m.group(1) if m else raw
                # 双向联动保护:持仓标的禁止从自选池删除(交易系统设计 §7 / 架构 §8.3)
                try:
                    from ..db import connect, init_db
                    conn = connect()
                    init_db(conn)
                    held = conn.execute("SELECT 1 FROM holdings WHERE symbol=?", (sym,)).fetchone()
                    conn.close()
                except Exception:  # noqa: BLE001
                    held = None
                if held:
                    return jsonify({"ok": False,
                                    "error": f"{sym} 在持仓卡中——请先在「设置」清空该持仓,再删除自选股"}), 409
                items = [i for i in _load() if i["symbol"] != sym]
                _save(items)
                return jsonify({"ok": True})

            items = []
            # 直接读 watchlist.yaml(而非 cfg.watchlist):后者在 app 启动时缓存,
            # 看不到页面上新增/删除的标的,导致删了还在、加了不出现。
            wl_items = _load()
            # 个股模式路由(方案 §4):个股走 qt 实时行情,基金快照源(fuyao)仅适用 ETF。
            # 个股快照 = 与基金快照同构的最小键(last_price/涨跌/换手),
            # 表格列与悬浮面板(app.js w.snapshot.last_price)零改动可用。
            stock_syms = [str(w["symbol"]) for w in wl_items
                          if _snapshot_kind(str(w["symbol"]), cfg) == "stock"]
            try:
                stock_q = (fetch_stock_snapshot(stock_syms) if stock_syms else {})
            except Exception as exc:  # noqa: BLE001  批量个股快照失败不拖垮整表
                logger.warning("个股批量快照失败,按无数据处理: %s", exc)
                stock_q = {}
            for w in wl_items:
                sym = str(w["symbol"])
                try:
                    if _snapshot_kind(sym, cfg) == "etf":
                        snap = fuyao.fetch_fund_snapshot(sym)
                    else:
                        q = stock_q.get(sym)
                        snap = ({"last_price": q.get("price"),
                                 "price_change_ratio_pct": q.get("pct_chg"),
                                 "turnover_ratio_pct": q.get("turnover_pct")} if q else None)
                except Exception as exc:  # noqa: BLE001  单只快照失败→该只留空,列表照常渲染
                    logger.warning("[%s] 快照失败,按无数据处理: %s", sym, exc)
                    snap = None
                items.append({
                    "symbol": sym,
                    "name": (w.get("name") or fetch_name(sym)
                             or (stock_q.get(sym) or {}).get("name") or sym),
                    "category": w.get("category", ""),
                    "type": _snapshot_kind(sym, cfg),
                    "snapshot": snap,
                })
            return jsonify({"ok": True, "items": items})
        except Exception as exc:  # noqa: BLE001
            logger.warning("自选股操作失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 标的搜索联想(同花顺式关键词添加)
    @app.route("/api/suggest")
    def api_suggest():
        """关键词 → 候选标的(名称/拼音缩写/代码均可,中文/ETF 过滤见 fetchers.suggest)。"""
        q = str(request.args.get("q", "")).strip()
        if not q:
            return jsonify({"ok": True, "items": []})
        try:
            from ..fetchers.suggest import search_suggestions
            return jsonify({"ok": True, "items": search_suggestions(q)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("标的联想失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘中快照(§5.4)
    # ------------------------------------------------------------ 盘中技术快照(2026-08-22)
    # 两张卡完全独立:数据快照(确定性,不经过 LLM) / 深入分析(LLM,失败降级)。
    # LLM 失败/幻觉不影响数据快照卡。
    @app.route("/api/snapshot/panel")
    def api_snapshot_panel():
        """悬浮面板标记批量查询(跨窗口同步真源,2026-09-07)。

        返回本进程内最近一次「刷新快照」生成的所有标记(ETF+个股统一,与 facts 返回的
        panel 字段完全同源)。浮窗轮询时调用——BroadcastChannel 只能覆盖同一浏览器
        上下文(双网页标签页),跨 WebView(浏览器↔pywebview 浮窗)必须走本接口。
        """
        out = {code: {"panel": v["panel"], "time": v["time"], "mode": v["mode"]}
               for code, v in _PANEL_CACHE.items()}
        return jsonify({"ok": True, "markers": out})

    @app.route("/api/snapshot/tech/facts")
    def api_snapshot_tech_facts():
        code = request.args.get("code", "")
        if not code:
            return jsonify({"ok": False, "error": "缺少 code"}), 400
        # ---- 个股模式路由(方案 §4):风险监控管道,ETF 路径不动 ----
        if _stock_mode_enabled(cfg) and _snapshot_kind(code, cfg) == "stock":
            try:
                from ..planner.stock_risk import (archive_stock_risk,
                                                  build_stock_risk_advice)

                adv = build_stock_risk_advice(code, cfg)
                signal = {
                    "one_sentence": adv["one_sentence"],
                    "signal_type": adv["signal_type"],
                    "state_word": adv["state"].get("state_word", ""),
                    "scenario": adv["state"].get("scenario", ""),
                    "risk_level": (adv["catalyst"] or {}).get("risk_level", "正常"),
                    # v5:方向性动作(减仓/清仓)时 panel.anchors 带当日锚定价,与 ETF 同构
                    "anchor_price": (adv.get("panel") or {}).get("anchors", {})
                    .get("anchor_price"),
                    "trigger": {}, "invalidation": {},
                }
                archive_stock_risk(code, adv)
                # 面板标记缓存:与返回给调用方的 panel 同源(跨窗口同步真源)
                _PANEL_CACHE[code] = {
                    "panel": adv.get("panel", {}),
                    "time": adv["facts"].get("now", ""),
                    "mode": "stock",
                }
                return jsonify({"ok": True, "markdown": adv["markdown"],
                                "panel": adv.get("panel", {}), "signal": signal,
                                "data_at": adv["facts"].get("now", ""),
                                "category": adv["category"], "mode": "stock"})
            except Exception as exc:  # noqa: BLE001
                logger.warning("个股风险快照失败 %s: %s", code, exc)
                return jsonify({"ok": False, "error": str(exc)}), 502
        try:
            facts, md, adv = _deterministic_snapshot(code, cfg)
            panel = _panel_payload_from(code, adv)
            # 面板标记缓存:与返回给调用方的 panel 同源(跨窗口同步真源)
            _PANEL_CACHE[code] = {
                "panel": panel,
                "time": facts.get("now", ""),
                "mode": "etf",
            }
            signal = {
                "one_sentence": adv["one_sentence"],
                "signal_type": adv["signal_type"],
                "state_word": adv["state"].get("state_word", ""),
                "scenario": adv["state"].get("scenario", ""),
                "risk_level": (adv["catalyst"] or {}).get("risk_level", "正常"),
                "anchor_price": adv["anchors"].get("anchor_price"),
                "trigger": adv["trigger"],
                "invalidation": adv["invalidation"],
            }
            return jsonify({"ok": True, "markdown": md, "panel": panel,
                            "signal": signal, "data_at": facts.get("now", ""),
                            "category": adv["category"]})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘中数据快照失败 %s: %s", code, exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.route("/api/snapshot/tech/analysis")
    def api_snapshot_tech_analysis():
        code = request.args.get("code", "")
        if not code:
            return jsonify({"ok": False, "error": "缺少 code"}), 400
        # ---- 个股模式:A 档风险解读(用户拍板 2026-09-04;v5 放行动作转译)
        # ——机制复用深入分析 pipeline(LLM 层/校验打回/降级链),骨架换个股风险语义 ----
        if _snapshot_kind(code, cfg) == "stock":
            try:
                from ..planner.stock_risk import build_stock_analysis

                md, degraded, data_at = build_stock_analysis(code, cfg)
                return jsonify({"ok": True, "markdown": md, "degraded": degraded,
                                "data_at": data_at, "mode": "stock"})
            except Exception as exc:  # noqa: BLE001
                logger.warning("个股风险解读失败 %s: %s", code, exc)
                return jsonify({"ok": False, "error": str(exc)}), 502
        try:
            from ..planner.intraday import build_tech_analysis

            hit = _SNAP_CACHE.get(code)
            if hit and (time.time() - hit[0]) <= SNAP_TTL_SEC:
                facts = hit[1]
            else:
                # 无缓存/过期 → 先自动刷新确定性建议并归档(对用户透明)
                facts, _md2, _adv2 = _deterministic_snapshot(code, cfg)
            md, degraded = build_tech_analysis(code, cfg, facts=facts)
            return jsonify({"ok": True, "markdown": md, "degraded": degraded,
                            "data_at": facts.get("now", "")})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘中深入分析失败 %s: %s", code, exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.route("/api/snapshot/tech/counterfactual")
    def api_snapshot_tech_counterfactual():
        """反事实推演(盘后·情景分支):收盘快照 + 迁移表 → LLM 情景推演(手动触发)。"""
        code = request.args.get("code", "")
        if not code:
            return jsonify({"ok": False, "error": "缺少 code"}), 400
        # ---- 个股模式:不支持(迁移表价位源=ETF 锚点白名单;个股结构性无价位档) ----
        if _snapshot_kind(code, cfg) == "stock":
            return jsonify({"ok": False, "mode": "stock",
                            "error": "个股模式不支持收盘反事实推演:迁移表依赖 ETF 锚点白名单价位档,"
                                     "个股模式定位为风险监控,无操作价位"}), 400
        try:
            from ..planner.intraday import build_counterfactual

            scenario = request.args.get("scenario") or None
            md, ok = build_counterfactual(code, cfg, scenario=scenario)
            return jsonify({"ok": True, "markdown": md, "degraded": not ok})
        except Exception as exc:  # noqa: BLE001
            logger.warning("反事实推演失败 %s: %s", code, exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘中快照推送
    @app.route("/api/snapshot/push", methods=["POST"])
    def api_snapshot_push():
        data = request.get_json(force=True) or {}
        md = data.get("markdown", "")
        if not md:
            return jsonify({"ok": False, "error": "缺少内容"}), 400
        try:
            from ..push import send

            r = send("盘中快照", md, level="alert", cfg=cfg)
            return jsonify(r)
        except Exception as exc:  # noqa: BLE001
            logger.warning("快照推送失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 设置:MVP(持仓卡 + 账户 + key 只读)
    @app.route("/api/holdings", methods=["GET", "POST", "DELETE"])
    def api_holdings():
        from ..db import connect, init_db

        try:
            if request.method == "POST":
                data = request.get_json(force=True) or {}
                raw = str(data.get("symbol", "")).strip()
                m = re.match(r"(\d{6})", raw)
                sym = m.group(1) if m else raw
                if not sym:
                    return jsonify({"ok": False, "error": "缺少 symbol"}), 400
                try:
                    cost = float(data.get("cost") or 0)
                    qty = int(data.get("quantity") or 0)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "cost/quantity 需为数字"}), 400
                if qty <= 0 or cost <= 0:
                    return jsonify({"ok": False, "error": "成本与数量需为正数"}), 400
                if qty % 100 != 0:
                    return jsonify({"ok": False, "error":
                                    f"股数需为 100 的整数倍(A股最小交易单位,当前 {qty})"}), 400
                name = str(data.get("name") or fetch_name(sym) or sym)
                status = str(data.get("status") or "持仓")
                conn = connect()
                init_db(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO holdings (symbol, name, cost, quantity, status)"
                    " VALUES (?,?,?,?,?)", (sym, name, cost, qty, status))
                conn.commit()
                conn.close()
                return jsonify({"ok": True, "item": {"symbol": sym, "name": name}})
            if request.method == "DELETE":
                raw = str(request.args.get("symbol", ""))
                m = re.match(r"(\d{6})", raw)
                sym = m.group(1) if m else raw
                conn = connect()
                init_db(conn)
                conn.execute("DELETE FROM holdings WHERE symbol=?", (sym,))
                conn.commit()
                conn.close()
                return jsonify({"ok": True})
            conn = connect()
            init_db(conn)
            rows = [dict(r) for r in conn.execute(
                "SELECT symbol, name, cost, quantity, status FROM holdings ORDER BY symbol")]
            conn.close()
            return jsonify({"ok": True, "holdings": rows})
        except Exception as exc:  # noqa: BLE001
            logger.warning("持仓操作失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.route("/api/account", methods=["GET", "POST"])
    def api_account():
        from ..db import connect, init_db

        try:
            conn = connect()
            init_db(conn)
            if request.method == "POST":
                data = request.get_json(force=True) or {}
                for key in ("total_capital", "cash"):
                    if data.get(key) is not None:
                        conn.execute("INSERT OR REPLACE INTO account_meta (key, value) VALUES (?,?)",
                                     (key, str(data[key])))
                conn.commit()
            meta = {str(r["key"]): r["value"] for r in conn.execute(
                "SELECT key, value FROM account_meta")}
            conn.close()
            return jsonify({"ok": True, "account": {
                "total_capital": meta.get("total_capital", ""),
                "cash": meta.get("cash", ""),
            }})
        except Exception as exc:  # noqa: BLE001
            logger.warning("账户操作失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 设置:刷新间隔(里程碑7 追加)
    @app.route("/api/refresh-config")
    def api_refresh_config():
        from ..webprefs import load_prefs

        return jsonify({"ok": True, **load_prefs()})

    @app.route("/api/refresh-config", methods=["POST"])
    def api_refresh_config_save():
        data = request.get_json(force=True) or {}
        patch: dict = {}
        if "refresh_interval_sec" in data:
            try:
                sec = int(data.get("refresh_interval_sec"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "刷新间隔需为整数秒"}), 400
            if sec < 1 or sec > 3600:
                return jsonify({"ok": False, "error": "刷新间隔需在 1-3600 秒之间"}), 400
            patch["refresh_interval_sec"] = sec
        if "panel_start_hidden" in data:
            patch["panel_start_hidden"] = bool(data.get("panel_start_hidden"))
        from ..webprefs import save_prefs

        cur = save_prefs(patch)
        return jsonify({"ok": True, **cur})

    # ------------------------------------------------------------ 设置:开机自启(托盘宿主,里程碑7 追加)
    @app.route("/api/panel/autostart")
    def api_panel_autostart_get():
        from ..autostart import is_enabled

        return jsonify({"ok": True, "enabled": is_enabled()})

    @app.route("/api/panel/autostart", methods=["POST"])
    def api_panel_autostart_save():
        data = request.get_json(force=True) or {}
        from ..autostart import set_enabled

        on = bool(data.get("enabled"))
        set_enabled(on)
        return jsonify({"ok": True, "enabled": on})

    @app.route("/api/keys")
    def api_keys():
        """API key 只读展示(掩码)。真实值在 config/settings.local.yaml,web 不提供写口。"""
        from ..config import LOCAL_CONFIG
        import yaml

        out = []
        try:
            data = yaml.safe_load(LOCAL_CONFIG.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            data = {}

        def _mask(v) -> str:
            s = str(v)
            return s[:3] + "*" * min(len(s) - 5, 12) + s[-2:] if len(s) > 8 else "***"

        for sec, keys in (("llm", ("deepseek", "qwen", "glm")),
                          ("fuyao", ("api_key",)), ("wind", ("api_key",))):
            node = data.get(sec)
            if isinstance(node, dict):
                for k in keys:
                    if node.get(k):
                        out.append({"section": sec, "key": k, "set": True,
                                    "masked": _mask(node[k])})
        return jsonify({"ok": True, "keys": out,
                        "hint": "API key 修改请直接编辑 config/settings.local.yaml(web 只读)"})

    @app.route("/api/llm")
    def api_llm():
        """LLM 配置读取(设置弹窗「模型」栏):生效 profiles(含 base_url/has_key) + 各服务商 key 掩码。

        profiles 来源 = settings.yaml llm.profiles 被 settings.local.yaml 覆盖后的合并结果,
        即当前实际按顺序尝试的候选列表。web 只读展示,修改走 /api/llm/save。
        """
        from ..llm import _ENDPOINTS

        profiles = list(cfg.get("llm", "profiles", default=[]) or [])
        out_p = []
        providers: list[str] = []
        for i, p in enumerate(profiles):
            if not isinstance(p, dict) or not p.get("provider") or not p.get("model"):
                continue
            provider = str(p["provider"]).strip()
            if provider not in providers:
                providers.append(provider)
            out_p.append({
                "order": i + 1,
                "provider": provider,
                "model": str(p["model"]).strip(),
                "base_url": str(p.get("base_url") or "").strip(),
                "has_key": bool(str(cfg.get("llm", f"{provider}_key", default="") or "").strip()),
            })
        # 已配置 key 的服务商也要展示(含不在 profiles 里的,便于后续添加模型直接复用)
        from ..config import LOCAL_CONFIG
        import yaml as _yaml

        try:
            _local = _yaml.safe_load(LOCAL_CONFIG.read_text(encoding="utf-8")) or {}
            for k in (_local.get("llm") or {}):
                if str(k).endswith("_key") and str(k)[:-4] not in providers:
                    providers.append(str(k)[:-4])
        except Exception:  # noqa: BLE001
            pass
        keys = []
        for provider in providers:
            v = str(cfg.get("llm", f"{provider}_key", default="") or "").strip()
            keys.append({"provider": provider, "set": bool(v),
                         "masked": _mask_key(v) if v else "",
                         "default_url": _ENDPOINTS.get(provider, "")})
        return jsonify({"ok": True, "profiles": out_p, "keys": keys,
                        "default_urls": _ENDPOINTS})

    @app.route("/api/llm/save", methods=["POST"])
    def api_llm_save():
        """保存 LLM 配置(设置弹窗「模型」栏):profiles 全量 + 非空 key 覆盖。

        - profiles: [{provider, model, base_url?}],按数组顺序即尝试顺序;
        - keys: {provider: key},值为空串 = 不修改(保留原 key);
        写入 settings.local.yaml(深合并,不动主配置),并 reload 长驻 cfg 立即生效。
        """
        from ..config import save_local_config

        data = request.get_json(force=True) or {}
        patch: dict = {"llm": {}}
        if "profiles" in data:
            if not isinstance(data["profiles"], list):
                return jsonify({"ok": False, "error": "profiles 需为列表"}), 400
            profs = []
            for p in data["profiles"]:
                try:
                    provider = str(p.get("provider", "")).strip()
                    model = str(p.get("model", "")).strip()
                    base_url = str(p.get("base_url") or "").strip()
                except AttributeError:
                    return jsonify({"ok": False, "error": "profiles 项格式错误"}), 400
                if not provider or not model:
                    return jsonify({"ok": False, "error": "服务商与模型名必填"}), 400
                row = {"provider": provider, "model": model}
                if base_url:
                    row["base_url"] = base_url
                profs.append(row)
            patch["llm"]["profiles"] = profs
        if data.get("keys"):
            if not isinstance(data["keys"], dict):
                return jsonify({"ok": False, "error": "keys 需为对象"}), 400
            for provider, key in data["keys"].items():
                key = str(key or "").strip()
                if not key:
                    continue
                patch["llm"][f"{str(provider).strip()}_key"] = key
        # 删除 key(clear_keys):值为 None → _deep_merge 显式删除该键,settings.local.yaml
        # 里彻底消失(不留 `custom_key: ''` 残行);该 provider 的密钥卡片随之消失。
        if data.get("clear_keys"):
            if not isinstance(data["clear_keys"], list):
                return jsonify({"ok": False, "error": "clear_keys 需为列表"}), 400
            for provider in data["clear_keys"]:
                provider = str(provider or "").strip()
                if provider:
                    patch["llm"][f"{provider}_key"] = None
        if not patch["llm"]:
            return jsonify({"ok": False, "error": "没有可保存的内容"}), 400
        try:
            save_local_config(patch)
            cfg.reload()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 配置保存失败: %s", exc)
            return jsonify({"ok": False, "error": f"保存失败: {exc}"}), 502
        return jsonify({"ok": True, "message": "已保存并生效"})

    # ------------------------------------------------------------ 设置:数据源密钥(同花顺等行情数据源)
    @app.route("/api/datasource", methods=["GET", "POST"])
    def api_datasource():
        """数据源 API Key 配置(设置-模型面板「数据源密钥」小节)。

        GET:返回各数据源 key 状态(脱敏);POST:保存非空 key 到 settings.local.yaml 并立即生效。
        key 留空 = 不修改(保留原值);clear:[source,...] = 彻底删除该数据源的 key。
        """
        from ..config import save_local_config

        sources = ("fuyao",)   # 数据源清单:目前仅同花顺(行情/异动/热榜)
        try:
            if request.method == "POST":
                data = request.get_json(force=True) or {}
                patch: dict = {}
                for src in sources:
                    key = str(data.get(src) or "").strip()
                    if key:
                        patch[src] = {"api_key": key}
                # 删除:patch 里 {"api_key": None} → _deep_merge 的 None 删除语义,彻底移除
                clears = data.get("clear") or []
                if not isinstance(clears, list):
                    return jsonify({"ok": False, "error": "clear 需为列表"}), 400
                for src in clears:
                    src = str(src or "").strip()
                    if src in sources:
                        patch[src] = {"api_key": None}
                if not patch:
                    return jsonify({"ok": False, "error": "没有可保存的内容(留空不修改)"}), 400
                try:
                    save_local_config(patch)
                    cfg.reload()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("数据源密钥保存失败: %s", exc)
                    return jsonify({"ok": False, "error": f"保存失败: {exc}"}), 502
                return jsonify({"ok": True, "message": "已保存并生效"})
            out = []
            for src in sources:
                v = str(cfg.get(src, "api_key", default="") or "").strip()
                out.append({"source": src, "set": bool(v), "masked": _mask_key(v) if v else ""})
            return jsonify({"ok": True, "sources": out})
        except Exception as exc:  # noqa: BLE001
            logger.warning("数据源密钥操作失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.route("/api/datasource/peek", methods=["POST"])
    def api_datasource_peek():
        """查看单个数据源明文 API Key(设置弹窗眼睛图标,前端已弹安全警告后调用)。"""
        data = request.get_json(force=True) or {}
        src = str(data.get("source", "")).strip()
        key = str(cfg.get(src, "api_key", default="") or "").strip()
        if not key:
            return jsonify({"ok": False, "error": f"{src} 未配置 API Key"}), 404
        return jsonify({"ok": True, "source": src, "key": key})

    @app.route("/api/llm/test", methods=["POST"])
    def api_llm_test():
        """测试连接(设置弹窗「模型」栏「测试连接」):用最小请求验证 Base URL + Key。

        表单显式传入的 key 优先;未填则用当前已保存的该服务商 key。
        """
        from ..llm import test_connection

        data = request.get_json(force=True) or {}
        try:
            provider = str(data.get("provider", "")).strip()
            model = str(data.get("model", "")).strip()
            base_url = str(data.get("base_url") or "").strip()
        except AttributeError:
            return jsonify({"ok": False, "error": "参数格式错误"}), 400
        key = str(data.get("key") or "").strip()
        if not key:
            key = str(cfg.get("llm", f"{provider}_key", default="") or "").strip()
        if not model:
            return jsonify({"ok": False, "error": "缺少模型名称"}), 400
        if not key:
            return jsonify({"ok": False, "error": "没有可用的 API Key(请先在表单中输入或保存)"}), 400
        try:
            ok, msg = test_connection(provider, model, base_url, key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 测试连接异常: %s", exc)
            return jsonify({"ok": False, "error": f"测试异常: {exc}"}), 502
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/llm/peek", methods=["POST"])
    def api_llm_peek():
        """查看单个服务商明文 API Key(设置弹窗眼睛图标,前端已弹安全警告后调用)。

        仅返回当前生效的 key,不落日志;前端展示后自行收起。"""
        data = request.get_json(force=True) or {}
        provider = str(data.get("provider", "")).strip()
        key = str(cfg.get("llm", f"{provider}_key", default="") or "").strip()
        if not key:
            return jsonify({"ok": False, "error": f"{provider} 未配置 API Key"}), 404
        return jsonify({"ok": True, "provider": provider, "key": key})

    # ------------------------------------------------------------ 单只行情
    @app.route("/api/quote")
    def api_quote():
        sym = request.args.get("symbol", "")
        if not sym:
            return jsonify({"ok": False, "error": "缺少 symbol"}), 400
        try:
            if _snapshot_kind(sym, cfg) == "stock":
                q = fetch_stock_snapshot([sym]).get(sym)
                snap = ({"last_price": q.get("price"),
                         "open_price": q.get("open"),
                         "high_price": q.get("high"),
                         "low_price": q.get("low"),
                         "prev_close": q.get("prev_close"),
                         "price_change_ratio_pct": q.get("pct_chg"),
                         "volume": (q.get("volume_hand") or 0) * 100,  # 手→股(前端÷100 回手)
                         "turnover_ratio_pct": q.get("turnover_pct"),
                         "limit_up": q.get("limit_up"),
                         "limit_down": q.get("limit_down")} if q else None)
                return jsonify({"ok": True, "symbol": sym, "snapshot": snap})
            return jsonify({"ok": True, "symbol": sym,
                            "snapshot": fuyao.fetch_fund_snapshot(sym)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("行情快照失败 %s: %s", sym, exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ K线
    @app.route("/api/kline")
    def api_kline():
        sym = request.args.get("symbol", "")
        period = request.args.get("period", "day")  # day | m30 | min
        if not sym:
            return jsonify({"ok": False, "error": "缺少 symbol"}), 400
        try:
            if period == "m30":
                df = fetch_30m(sym, count=160)
                rows = [{"t": str(r.ts), "o": r.open, "h": r.high,
                         "l": r.low, "c": r.close, "v": r.volume,
                         "am": getattr(r, "amount", None)}
                        for r in df.itertuples()]
                return jsonify({"ok": True, "period": period, "rows": rows})
            if period == "min":
                from ..fetchers.etf_kline import fetch_minute
                df = fetch_minute(sym)
                rows = [{"t": str(r.ts), "p": r.price, "v": r.vol, "am": r.amount,
                         "avg": r.avg_price, "pc": r.prev_close}
                        for r in df.itertuples()]
                return jsonify({"ok": True, "period": period, "rows": rows})
            # 日K(近 300 根)
            df = fetch_daily(sym, "2024-01-01", "2030-12-31").tail(300)
            rows = [{"t": str(r.trade_date), "o": r.open, "h": r.high,
                     "l": r.low, "c": r.close, "v": r.volume,
                     "am": getattr(r, "amount", None), "tr": getattr(r, "turnover", None)}
                    for r in df.itertuples()]
            return jsonify({"ok": True, "period": "day", "rows": rows})
        except Exception as exc:  # noqa: BLE001
            logger.warning("K线失败 %s/%s: %s", sym, period, exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘前视图(市场状态灯 + 隔夜消息)
    @app.route("/api/morning")
    def api_morning():
        try:
            from ..morning_view import build_morning_view

            view = build_morning_view(cfg)
            return jsonify({"ok": True, **view})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘前视图失败: %s", exc)
            return jsonify({"ok": False, "error": "盘前视图生成失败,请稍后重试"}), 502

    # ------------------------------------------------------------ 信息层评估(方案 M5/M6)
    @app.route("/api/news/stats")
    def api_news_stats():
        from ..db import connect, init_db
        from ..morning_verify import aggregate_stats, keyword_stats, tuning_report

        conn = connect()
        init_db(conn)
        try:
            return jsonify({
                "ok": True,
                "stats20": aggregate_stats(conn, n_day=20),
                "stats3": aggregate_stats(conn, n_day=3),
                "keywords": keyword_stats(conn, n_day=20),
                "report": tuning_report(conn, n_day=20),
            })
        finally:
            conn.close()

    # ------------------------------------------------------------ 盘后建议验证(增量4 MVP:懒验证 + 日历)
    @app.route("/api/verify/calendar")
    def api_verify_calendar():
        """建议验证日历:先懒跑到期未判的验证(幂等、本地、零推送),再返回
        advice_archive × 3/5 日判定,按交易日倒序分天。MVP 只做这一件事。"""
        try:
            from ..db import connect, init_db
            from ..planner.advice_verify import run_advice_verification

            # 1) 按需补拉建议日之后的日 K(断言数据新鲜,网络失败容忍)
            conn = connect()
            init_db(conn)
            try:
                _ensure_verify_klines(conn)
            finally:
                conn.close()

            # 2) 懒验证:只补判到期未判;失败不阻断展示
            done = 0
            try:
                done = run_advice_verification()
            except Exception as exc:  # noqa: BLE001
                logger.warning("建议验证懒跑失败(继续展示已有判定): %s", exc)

            conn = connect()
            init_db(conn)
            try:
                # 旧库一次性收敛:开发期每次分析存一行 → 按(日期,标的)保留当日
                # 最后时刻的一行(新归档语义已改为覆盖写入,此条只处理 2026-08 遗留)
                conn.execute(
                    "DELETE FROM advice_archive WHERE id NOT IN"
                    " (SELECT id FROM advice_archive a WHERE id ="
                    " (SELECT a2.id FROM advice_archive a2"
                    "  WHERE a2.trade_date=a.trade_date AND a2.symbol=a.symbol"
                    "  ORDER BY a2.created_at DESC LIMIT 1))")
                names = {r["symbol"]: r["name"]
                         for r in conn.execute("SELECT symbol, name FROM etf")}
                # 名称兜底:自选池(web 新增标的可能未入 etf 表,如新的科创芯片 588200)
                try:
                    from ..config import WATCHLIST_FILE
                    import yaml as _yaml
                    if WATCHLIST_FILE.exists():
                        for w in (_yaml.safe_load(WATCHLIST_FILE.read_text(encoding="utf-8")) or []):
                            if w.get("symbol") and w.get("name"):
                                names.setdefault(str(w["symbol"]), str(w["name"]))
                except Exception:  # noqa: BLE001
                    pass
                # v5.2 名称兜底终极形态(根因修复):etf 表(仅9行)/watchlist(仅6行)
                # 都不是"被监控标的注册表"——直接在快照输入框监控的标的(如 512480)
                # 两边都查不到,名字解析断链。凡未解析的归档标的,统一 qt 实时取名
                # (fetch_name 对 ETF/个股通用),进程内缓存,网络失败留空不阻断。
                for a0 in [dict(r) for r in conn.execute(
                        "SELECT DISTINCT symbol FROM advice_archive")]:
                    sym0 = str(a0["symbol"])
                    if sym0 and sym0 not in names:
                        try:
                            from ..fetchers.etf_kline import fetch_name
                            nm = _stock_name_cache.get(sym0)
                            if nm is None:
                                nm = fetch_name(sym0)
                                _stock_name_cache[sym0] = nm
                            if nm:
                                names[sym0] = nm
                        except Exception:  # noqa: BLE001
                            pass
                archs = [dict(r) for r in conn.execute(
                    "SELECT id, trade_date, symbol, anchor_price, state_word, category,"
                    " advice_md, snapshot_md, degraded, verdict FROM advice_archive"
                    " WHERE trade_date >= date('now','localtime','-90 day')"
                    " ORDER BY trade_date DESC, symbol")]
                vrows = [dict(r) for r in conn.execute(
                    "SELECT archive_id, n_day, outcome, move_pct FROM advice_verification"
                    " WHERE n_day IN (3, 5)")]
            finally:
                conn.close()

            vmap: dict[int, dict[int, dict]] = {}
            for v in vrows:
                vmap.setdefault(v["archive_id"], {})[v["n_day"]] = v

            by_date: dict[str, list[dict]] = {}
            from ..planner.advice_verify import _direction
            for a in archs:
                # direction:up/down/None;观望等无方向 → 日历显示灰虚线"观望·不验证"(留痕)
                d = _direction({"category": a["category"],
                                "advice_md": a["advice_md"]})
                vs = vmap.get(a["id"], {})
                v3, v5 = vs.get(3), vs.get(5)
                md = str(a["advice_md"] or "")
                item = {
                    "symbol": a["symbol"],
                    "name": names.get(a["symbol"]) or "",
                    "anchor_price": a["anchor_price"],
                    "state_word": a["state_word"] or "",
                    "category": a["category"] or "",
                    "direction": d,            # up / down / None(观望等无方向)
                    "signal_type": _signal_type_from_arch(md),
                    "one_sentence": (_rebuild_sentence_from_snapshot(
                                     str(a.get("snapshot_md") or ""),
                                     a.get("state_word") or "",
                                     a.get("anchor_price"))
                                 or _one_sentence_from_md(md)),
                    "summary": _card_summary(md, a["category"], a["state_word"], a["anchor_price"]),
                    "degraded": bool(a["degraded"]),
                    "out3": v3["outcome"] if v3 else None,
                    "move3": v3["move_pct"] if v3 else None,
                    "out5": v5["outcome"] if v5 else None,
                    "move5": v5["move_pct"] if v5 else None,
                }
                by_date.setdefault(a["trade_date"], []).append(item)
            days = [{"trade_date": d, "items": items}
                    for d, items in by_date.items()]
            return jsonify({"ok": True, "days": days, "verified": done})
        except Exception as exc:  # noqa: BLE001
            logger.warning("验证日历失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    return app


DEFAULT_PORT = 8765   # 冷门端口,避开 5000 等常用默认端口


def main() -> None:
    import threading
    import webbrowser

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    port = int(os.environ.get("ALPHAPRISM_WEB_PORT", DEFAULT_PORT))
    url = f"http://127.0.0.1:{port}"
    print(f"AlphaPrism Web 行情页: {url}")
    print("关闭本窗口即停止服务。")
    # 延迟打开浏览器(等服务真正就绪)
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # threaded=True:盘前视图的 LLM 调用较慢(10-20s),若不开启线程会阻塞其他所有接口
    # (自选股/K线/指数),导致行情图迟迟不渲染。多线程后各请求互不阻塞。
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
