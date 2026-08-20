"""新闻快讯采集(盘前催化检测用):新浪 7x24 快讯,按板块关键词过滤。

2026-08-12 验证:财联社(签名反爬 10012)、网易、腾讯、雪球均被拦/空;
新浪 zhibo(zhibo_id=152)免费可用,返回 {create_time, rich_text, docurl, tag}。
板块→关键词表在 config/settings.yaml `news.keywords`(盘前过滤,词不够就加)。
"""
from __future__ import annotations

import logging

import requests

from ..net import apply_network_policy

logger = logging.getLogger(__name__)

SINA_FEED_URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}


def fetch_sina_feed(pages: int = 1, page_size: int = 50) -> list[dict]:
    """新浪 7x24 快讯列表。返回 [{time, text, url, tag}],按时间倒序(新→旧)。"""
    apply_network_policy(True)
    items: list[dict] = []
    for page in range(1, pages + 1):
        try:
            resp = requests.get(
                SINA_FEED_URL,
                params={"page": page, "page_size": page_size, "zhibo_id": 152},
                headers=_HEADERS, timeout=15,
            )
            resp.raise_for_status()
            js = resp.json()
            feed = (js.get("result") or {}).get("data") or {}
            lst = (feed.get("feed") or {}).get("list") or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("新浪 7x24 第 %d 页抓取失败: %s", page, exc)
            continue
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
    return items


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
