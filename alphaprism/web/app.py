"""Web 行情页后端(Flask,里程碑6)。

复用现有 Python 引擎,通过 REST API 供 Vue 前端调用:
- /api/indices      指数实时(上证/科创/深成/创业板)
- /api/watchlist    自选股:实时行情 + 核对引擎状态灯
- /api/quote        单只实时行情
- /api/kline        K线(日K/30分/分时)
- /api/battlemap    作战地图 RuleModel
- /api/backtest     作战地图回测
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from ..config import Config
from ..planner.parser import parse_file as parse_battlemap
from ..backtest_map import run_map_backtest
from ..fetchers import fuyao
from ..fetchers.etf_kline import fetch_30m, fetch_daily, fetch_name

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(WEB_DIR / "static"), static_url_path="/static")
    cfg = Config()

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
                name = data.get("name") or fetch_name(sym) or f"ETF{sym}"
                category = data.get("category") or "行业"
                items.append({"symbol": sym, "name": name, "category": category})
                _save(items)
                return jsonify({"ok": True, "item": {"symbol": sym, "name": name, "category": category}})
            if request.method == "DELETE":
                raw = str(request.args.get("symbol", ""))
                m = re.match(r"(\d{6})", raw)
                sym = m.group(1) if m else raw
                items = [i for i in _load() if i["symbol"] != sym]
                _save(items)
                return jsonify({"ok": True})

            items = []
            for w in cfg.watchlist:
                sym = str(w["symbol"])
                snap = fuyao.fetch_fund_snapshot(sym)
                items.append({
                    "symbol": sym,
                    "name": w.get("name") or fetch_name(sym) or sym,
                    "category": w.get("category", ""),
                    "snapshot": snap,
                })
            return jsonify({"ok": True, "items": items})
        except Exception as exc:  # noqa: BLE001
            logger.warning("自选股操作失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘中快照(§5.4)
    @app.route("/api/snapshot")
    def api_snapshot():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        try:
            from ..planner.live import check_live
            from ..planner.snapshot import build_snapshot

            model = parse_battlemap(path)
            r = check_live(model)
            md = build_snapshot(model, r, cfg)
            return jsonify({"ok": True, "markdown": md,
                            "gate": r.get("gate"), "verdicts": r.get("verdicts")})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘中快照失败: %s", exc)
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

    # ------------------------------------------------------------ 单只行情
    @app.route("/api/quote")
    def api_quote():
        sym = request.args.get("symbol", "")
        if not sym:
            return jsonify({"ok": False, "error": "缺少 symbol"}), 400
        try:
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

    # ------------------------------------------------------------ 作战地图
    @app.route("/api/battlemap")
    def api_battlemap():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        try:
            model = parse_battlemap(path)
            return jsonify({"ok": True, "model": model.to_dict(),
                            "summary": model.summary()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("作战地图解析失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘前视图(实时新闻+LLM,三块联动)
    @app.route("/api/morning")
    def api_morning():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        try:
            from ..planner.playbook import build_morning_view

            model = parse_battlemap(path)
            view = build_morning_view(model, cfg)
            return jsonify({"ok": True, **view})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘前视图失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘前生成(里程碑2)
    @app.route("/api/playbook", methods=["GET", "POST"])
    def api_playbook():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        try:
            from ..planner.playbook import append_to_journal as pb_append
            from ..planner.playbook import build_draft, format_draft

            model = parse_battlemap(path)
            if request.method == "POST":
                data = request.get_json(force=True) or {}
                draft = {"date": "", "summary": data.get("summary", ""),
                         "rows": data.get("rows", [])}
                md = format_draft(draft, model)
                pb_append(md, path)
                return jsonify({"ok": True, "markdown": md})
            draft = build_draft(model)
            md = format_draft(draft, model)
            return jsonify({"ok": True, "markdown": md,
                            "summary": draft.get("summary", ""),
                            "rows": draft.get("rows", [])})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘前生成失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘中核对(里程碑3)
    @app.route("/api/check")
    def api_check():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        try:
            from ..planner.live import check_live

            model = parse_battlemap(path)
            r = check_live(model)
            return jsonify({"ok": True, **r})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘中核对失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 盘后生成(里程碑4)
    @app.route("/api/close")
    def api_close():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        try:
            from ..planner.close import build_close_review

            model = parse_battlemap(path)
            md = build_close_review(model)
            return jsonify({"ok": True, "markdown": md})
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘后生成失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------ 回测
    @app.route("/api/backtest")
    def api_backtest():
        path = _battlemap_path(cfg)
        if not path:
            return jsonify({"ok": False, "error": "未找到作战地图"}), 404
        start = request.args.get("start", "2025-08-01")
        end = request.args.get("end", "2026-08-01")
        try:
            capital = float(request.args.get("capital", 17300))
        except ValueError:
            capital = 17300.0
        try:
            model = parse_battlemap(path)
            r = run_map_backtest(model, start, end, capital)
            if "error" in r:
                return jsonify({"ok": False, "error": r["error"]}), 400
            return jsonify({"ok": True, "result": r})
        except Exception as exc:  # noqa: BLE001
            logger.warning("回测失败: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 502

    return app


def _battlemap_path(cfg: Config) -> str:
    """作战地图路径:config 配置或 AITrader 最新一份。"""
    p = cfg.get("battlemap", "path", default="")
    if p:
        return p
    import glob

    cands = sorted(glob.glob(r"E:\AITrader\七只ETF作战地图_*.md"), reverse=True)
    return cands[0] if cands else ""


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
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
