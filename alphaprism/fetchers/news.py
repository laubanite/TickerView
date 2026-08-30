"""新闻快讯采集(盘前催化检测用):新浪 7x24 + 东方财富快讯,按板块关键词过滤。

2026-08-12 验证:财联社(签名反爬 10012)、网易、腾讯、雪球均被拦/空;
新浪 zhibo(zhibo_id=152)免费可用,返回 {create_time, rich_text, docurl, tag}。
2026-08-21 补:东方财富 np-listapi 快讯(web_724)可直连,返回 {title, summary, showTime}——
作为第二消息面来源扩覆盖(尤其隔夜宏观/大盘消息,新浪 7x24 常被个股琐事刷屏)。
2026-08-21 升级(方案 M1):fetch 结构化(FeedResult)+ 瞬态重试 + 时效校验(stale)。
失败不再静默:每次抓取的健康状态由调用方(morning._build_context)写 fetch_log 审计。

板块→关键词表在 config/settings.yaml `news.keywords`(盘前过滤,词不够就加)。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from ..net import apply_network_policy

logger = logging.getLogger(__name__)

SINA_FEED_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
EM_FEED_URL = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}


@dataclass
class FeedResult:
    """单源抓取结果。ok=False 表示该源本次未抓到(原因见 error),调用方不得静默忽略。"""
    source: str            # sina | em
    ok: bool               # 源是否可达(至少一页请求成功)
    items: list[dict]      # [{time, text, url, tag}]
    fetched_at: str        # ISO 时间戳(抓取完成时刻)
    rows: int              # 实际条数
    error: str | None = None


def _get_with_retry(url: str, params: dict, headers: dict,
                    timeout: int = 15, retries: int = 2) -> requests.Response:
    """GET + 瞬态错误指数退避重试(0.5s → 1.5s)。HTTP 4xx 不重试;5xx/网络错误重试。"""
    delay = 0.5
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(delay)
                delay *= 3
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError:
            raise  # 4xx 不重试;5xx 重试耗尽后也在此抛出
        except requests.exceptions.RequestException:
            if attempt >= retries:
                raise
            time.sleep(delay)
            delay *= 3
    raise RuntimeError("unreachable")


def _parse_ts(s: str) -> datetime | None:
    """解析快讯时间字段(新浪/东财格式不一),解析失败返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _prev_trading_day(now: datetime | None = None) -> datetime:
    """最近一个**已收盘**交易日(15:00 后),用于隔夜窗口起点,兼容周末/长假。"""
    from . import fuyao  # 延迟导入,避免与 fuyao 的模块级依赖纠缠
    now = now or datetime.now()
    for back in range(0, 10):
        d = now - timedelta(days=back)
        if not fuyao.is_trading_day(d.strftime("%Y%m%d")):
            continue
        if d.date() == now.date() and now.hour < 15:
            continue  # 今天尚未收盘,隔夜窗口起点应取上一交易日
        return d
    return now - timedelta(days=1)  # 日历异常时退化为昨天


def _is_stale(items: list[dict], now: datetime | None = None) -> tuple[bool, str]:
    """最新消息是否陈旧:期望 >= 最近已收盘交易日 15:00,否则视为陈旧缓存。

    周末/长假时最新消息停在最近交易日属正常,不误报。返回 (stale, latest_ts)。
    """
    if not items:
        return False, ""
    latest = max((it.get("time") or "") for it in items)
    latest_dt = _parse_ts(latest)
    if latest_dt is None:
        return False, latest
    cutoff = _prev_trading_day(now).replace(hour=15, minute=0, second=0, microsecond=0)
    return latest_dt < cutoff, latest


def fetch_sina_feed(pages: int = 1, page_size: int = 50, retries: int = 2) -> FeedResult:
    """新浪 7x24 快讯列表(按时间倒序,新→旧)。失败不静默:返回 FeedResult(ok=False)。"""
    apply_network_policy(True)
    items: list[dict] = []
    errors: list[str] = []
    pages_ok = 0
    for page in range(1, pages + 1):
        try:
            resp = _get_with_retry(
                SINA_FEED_URL,
                params={"page": page, "page_size": page_size, "zhibo_id": 152},
                headers=_HEADERS, retries=retries,
            )
            js = resp.json()
            feed = (js.get("result") or {}).get("data") or {}
            lst = (feed.get("feed") or {}).get("list") or []
        except Exception as exc:  # noqa: BLE001
            errors.append(f"page{page}: {exc}")
            logger.warning("新浪 7x24 第 %d 页抓取失败: %s", page, exc)
            continue
        pages_ok += 1
        for it in lst:
            tag = ""
            if isinstance(it.get("tag"), list) and it["tag"]:
                tag = (it["tag"][0].get("name") or "") if isinstance(it["tag"][0], dict) else ""
            items.append({
                "time": it.get("create_time") or "",
                "text": (it.get("rich_text") or it.get("title") or "").strip(),
                "url": it.get("docurl") or "",
                "tag": tag,
            })
    return FeedResult(source="sina", ok=pages_ok > 0, items=items,
                      fetched_at=datetime.now().isoformat(timespec="seconds"),
                      rows=len(items), error="; ".join(errors) or None)


def fetch_em_feed(page_size: int = 50, retries: int = 2) -> FeedResult:
    """东方财富 7x24 快讯列表(按时间倒序)。偏宏观/大盘/政策,与新浪互补。"""
    apply_network_policy(True)
    try:
        resp = _get_with_retry(
            EM_FEED_URL,
            params={"client": "web", "biz": "web_724", "fastColumn": "102",
                    "sortEnd": "", "pageSize": str(page_size), "req_trace": "1"},
            headers=_HEADERS, retries=retries,
        )
        js = resp.json()
        data = js.get("data") or {}
        lst = data.get("fastNewsList") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("东财快讯抓取失败: %s", exc)
        return FeedResult(source="em", ok=False, items=[],
                          fetched_at=datetime.now().isoformat(timespec="seconds"),
                          rows=0, error=str(exc))
    out: list[dict] = []
    for it in lst:
        text = (it.get("summary") or it.get("title") or "").strip()
        if not text:
            continue
        out.append({
            "time": it.get("showTime") or "",
            "text": text,
            "url": "",   # 东财快讯无单条 docurl
            "tag": "",
        })
    return FeedResult(source="em", ok=True, items=out,
                      fetched_at=datetime.now().isoformat(timespec="seconds"),
                      rows=len(out))


def fetch_feeds(pages: int = 1, page_size: int = 50,
                em_page_size: int | None = None, retries: int = 2) -> dict:
    """双源抓取 + 健康状态。返回完整结构(盘前视图/简报取 health 与 items):

    {sources: [FeedResult], items: 合并去重(时间倒序),
     fetched_at, any_ok, all_failed, stale, latest_ts}
    """
    sina = fetch_sina_feed(pages=pages, page_size=page_size, retries=retries)
    em = fetch_em_feed(page_size=em_page_size or page_size, retries=retries)
    merged = sina.items + em.items
    # 去重 + 跨源计数:同一(分钟级时间, 文本前 30 字)在两源都出现 → cross=2(方案 M2)
    first: dict[tuple, dict] = {}
    cross_keys: set[tuple] = set()
    for it in merged:
        key = (it["time"][:16], it["text"][:30])
        if key in first:
            cross_keys.add(key)
            continue
        first[key] = it
    out = list(first.values())
    for it in out:
        it["cross"] = 2 if (it["time"][:16], it["text"][:30]) in cross_keys else 1
    out.sort(key=lambda x: x["time"], reverse=True)  # 新→旧
    stale, latest_ts = _is_stale(out)
    return {
        "sources": [sina, em],
        "items": out,
        "fetched_at": max(sina.fetched_at, em.fetched_at),
        "any_ok": sina.ok or em.ok,
        "all_failed": not (sina.ok or em.ok),
        "stale": stale,
        "latest_ts": latest_ts,
    }


def fetch_all_feeds(pages: int = 1, page_size: int = 50,
                    em_page_size: int | None = None) -> list[dict]:
    """兼容旧接口:仅返回合并去重后的 items(健康状态走 fetch_feeds)。"""
    return fetch_feeds(pages=pages, page_size=page_size, em_page_size=em_page_size)["items"]


def filter_by_keywords(items: list[dict], keyword_map: dict[str, list[str]]) -> dict[str, list[dict]]:
    """按 板块→关键词 过滤,返回 {板块: [命中的快讯]}。"""
    out: dict[str, list[dict]] = {}
    for sector, kws in keyword_map.items():
        hits = [it for it in items if any(kw and kw in it["text"] for kw in kws)]
        if hits:
            out[sector] = hits
    return out


def news_digest(hits: dict[str, list[dict]], limit: int = 5) -> str:
    """过滤结果 → 盘前简报用文本(每板块取最近 limit 条)。"""
    lines = []
    for sector, items in hits.items():
        lines.append(f"**{sector}**:{len(items)} 条")
        for it in items[:limit]:
            lines.append(f"- {it['time'][5:16]} {it['text'][:80]}")
    return "\n".join(lines) if lines else "今日无匹配板块新闻"