"""标的搜索联想:腾讯 smartbox 接口(与 etf_kline.fetch_name 同源,本机直连稳定)。

行为对标同花顺等通用行情软件的搜索框:
- 中文关键词("半导体")、拼音缩写("bdt")、6 位代码("512480")均可命中
- 只返回系统可跟踪的类别:A股个股(GP-A)/ETF/LOF;指数、债券、逆回购、B股、
  港美股等一律过滤——watchlist 快照路由(_snapshot_kind)仅支持 stock/etf,
  放进候选会得到"加了但无行情"的死行
- 统一返回结构:{"symbol","name","market","kind","pinyin"}
"""
from __future__ import annotations

import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

_URL = "https://smartbox.gtimg.cn/s3/"
_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

# 进程内缓存:连续击键会高频触发联想,同一关键词 5 分钟内直接复用
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300.0
_CACHE_MAX = 128


def _decode_escapes(text: str) -> str:
    """响应体里的中文名以 \\uXXXX 转义出现,还原为汉字。"""
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _kind_of(raw_type: str) -> str | None:
    """smartbox 原始 type → 候选类别;None = 系统不可跟踪,过滤。"""
    t = str(raw_type or "").upper()
    if "ETF" in t:
        return "ETF"
    if "LOF" in t:
        return "LOF"
    if t.startswith("GP-A"):  # A股(沪深主板/创业板/科创板/北交所)
        return "股"
    return None  # 指数/债券/回购/B股/港美股等不进候选


def search_suggestions(keyword: str, limit: int = 8) -> list[dict]:
    """关键词 → 候选标的列表。网络/解析失败返回 [](联想是增强功能,不阻断页面)。"""
    q = str(keyword or "").strip()
    if not q or len(q) > 24:
        return []

    now = time.time()
    hit = _CACHE.get(q)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    # 与 fetch_name 同策略:清代理环境变量,直连腾讯国内源
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    try:
        resp = requests.get(_URL, params={"v": 2, "q": q, "t": "all"},
                            headers=HEADERS, timeout=8)
        resp.raise_for_status()
        resp.encoding = "gbk"
    except Exception as exc:  # noqa: BLE001
        logger.warning("标的联想请求失败(%s): %s", q, exc)
        return []

    text = _decode_escapes(resp.text)
    m = re.search(r'v_hint="(.*)"', text)
    items: list[dict] = []
    if m and m.group(1) and m.group(1) != "N":
        for entry in m.group(1).split("^"):
            fields = entry.split("~")
            if len(fields) < 5:
                continue
            market, code, name, pinyin, raw_type = fields[:5]
            kind = _kind_of(raw_type)
            if kind is None or not re.fullmatch(r"\d{6}", code):
                continue
            items.append({
                "symbol": code,
                "name": name,
                "market": market,
                "kind": kind,
                "pinyin": pinyin,
            })
            if len(items) >= limit:
                break

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[q] = (now, items)
    return items
