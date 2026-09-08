"""盘前视图(盘前方案 v3.3):市场状态灯 + 隔夜重要消息。

不依赖外部计划文档:
- 板块上下文来自自选池 + 同花顺催化剂(缺 key / 网络失败时降级为空,见 morning._build_context);
- T1 定级走 planner.prestate.compose_state(纯消息,LLM 不参与判定);
- 剧本 / 纪律 / 大盘门控已随旧版计划层下线,本模块不再产出。

供 web /api/morning 调用。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from .config import WATCHLIST_FILE, Config
from .db import connect, init_db
from .fetchers import news as news_mod
from .morning import (_build_context, _llm_important_news, _rule_important_news,
                      _sector_perf)
from .morning_verify import prior_prompt
from .planner.prestate import compose_state

logger = logging.getLogger(__name__)


def _load_watchlist() -> list[dict]:
    """自选池(状态灯标的行 + 证据 sector 归属的唯一数据源,不依赖持仓卡)。"""
    try:
        if WATCHLIST_FILE.exists():
            import yaml

            return list(yaml.safe_load(WATCHLIST_FILE.read_text(encoding="utf-8")) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("自选池读取失败(状态灯标的行降级为空): %s", exc)
    return []


def build_morning_view(cfg: Config | None = None) -> dict:
    """盘前视图:①市场状态灯(T1) + ②过滤后隔夜重要消息(证据链)。"""
    cfg = cfg or Config()
    conn = connect()
    try:
        init_db(conn)   # 确保 important_news / pre_state / catalyst_verification 等表存在(幂等)
        ctx = _build_context(cfg, conn)
        sectors = ctx["sectors"]
        news_health = ctx.get("news_health")
        feed = ctx.get("feed") or []
        kws = cfg.get("news", "keywords", default={}) or {}
        sector_names = sorted({s["sector"] for s in sectors if s.get("sector")})
        sector2sym = {s["sector"]: s["symbol"] for s in sectors if s.get("sector")}

        # ① 隔夜重要消息:坏数据(全失败/陈旧)不进列表,前端显示诚实空态
        if news_health and (news_health.get("all_failed") or news_health.get("stale")):
            important = {"summary": "", "items": []}
        else:
            sector_hits = news_mod.filter_by_keywords(feed, kws)
            important = _llm_important_news(
                feed, sector_hits, sector_names, cfg, prior=prior_prompt(conn),
                sector_perf=_sector_perf(conn, sector2sym))
            if not important.get("items"):
                # AI 筛选失败(超时/限流/输出不合法)→ 降级两档:关键词命中 → 原始快讯
                important = _rule_important_news(sector_hits, sector_names, feed=feed)

        watchlist = _load_watchlist()
        _persist_important_news(conn, important, sector2sym)
        _run_verification(conn)

        # ② 市场状态灯 T1(纯消息定级 + 组合/标的小灯)
        state = compose_state(important.get("items") or [], watchlist)
        for it, tag in zip(important.get("items") or [], state.get("tags") or []):
            it["pm_tag"] = tag          # 每条消息的词库命中标签(证据列表标注)
        state.pop("tags", None)
        _persist_pre_state(conn, state)

        return {"date": date.today().isoformat(),
                "summary": important.get("summary", ""),
                "news": important, "news_health": news_health, "state": state}
    finally:
        conn.close()


# ---------------------------------------------------------------- 持久化(自 playbook 迁入,零 model 依赖)

def _persist_important_news(conn, important: dict, sector2sym: dict[str, str]) -> None:
    """当日重要消息写入 important_news(验证输入)。按 (trade_date, text) upsert。"""
    try:
        cfg = Config()
        kws_map = cfg.get("news", "keywords", default={}) or {}
        all_kws = sorted({k for ks in kws_map.values() for k in ks})
        today = date.today().isoformat()
        now = datetime.now().isoformat(timespec="seconds")
        for it in (important.get("items") or []):
            text = (it.get("text") or "").strip()
            if not text:
                continue
            hit_kws = [k for k in all_kws if k in text]
            conn.execute(
                "INSERT INTO important_news (trade_date, sector, symbol, text, impact, confidence, "
                "type, source_grade, cross, source, keywords, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(trade_date, text) DO UPDATE SET impact=excluded.impact, "
                "confidence=excluded.confidence, type=excluded.type, source_grade=excluded.source_grade, "
                "cross=excluded.cross, keywords=excluded.keywords",
                (today, it.get("sector") or "其他", sector2sym.get(it.get("sector")),
                 text, it.get("impact", "中性"), it.get("confidence"), it.get("type"),
                 it.get("source_grade"), it.get("cross", 1), it.get("source"),
                 json.dumps(hit_kws, ensure_ascii=False), now))
        conn.commit()
    except Exception as exc:  # noqa: BLE001  持久化失败不影响盘前视图
        logger.warning("important_news 写入失败: %s", exc)


def _persist_pre_state(conn, state: dict) -> None:
    """T1 定级写库(每日一份;盘中 C15 读取作当日下限,接线后置)。失败容忍。"""
    try:
        conn.execute(
            "INSERT INTO pre_state (trade_date, level, name, hint, evidence,"
            " per_symbol, hits, bonus, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))"
            " ON CONFLICT(trade_date) DO UPDATE SET level=excluded.level, name=excluded.name,"
            " hint=excluded.hint, evidence=excluded.evidence, per_symbol=excluded.per_symbol,"
            " hits=excluded.hits, bonus=excluded.bonus, updated_at=excluded.updated_at",
            (date.today().isoformat(), state["level"], state["name"], state["hint"],
             json.dumps(state.get("evidence") or [], ensure_ascii=False),
             json.dumps(state.get("per_symbol") or [], ensure_ascii=False),
             json.dumps(state.get("hits") or [], ensure_ascii=False),
             json.dumps(state.get("bonus") or {}, ensure_ascii=False)))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pre_state 写入失败: %s", exc)


def _run_verification(conn) -> None:
    """到期催化判定跑 N 日验证(幂等)。失败不阻断盘前视图。"""
    try:
        from .morning_verify import verify_pending

        res = verify_pending(conn)
        if res.get("verified"):
            logger.info("催化验证新增 %d 条", res["verified"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("催化验证失败(不阻断视图): %s", exc)
