"""同花顺金融数据 API 直连(fuyao.aicubes.cn,REST)。

背景(2026-08-12):东财 push2* 被 WAF 拦、baostock ETF 仅近 7 个月,行业/板块数据缺口
用同花顺补。当前只用于「板块成交占比」(量价规则表留待项):
- 行业指数快照取成交额(thscode 形如 881109.TI,2026-08-12 从 ths-index-list 核实)
- 全市场成交额 = 上证综指(000001.SH)+ 深证综指(399106.SZ),不含北交所

鉴权:X-api-key 头;key 在 config/settings.local.yaml fuyao.api_key(已 gitignore)。
信封:{code, message, request_id, data:{item}};错误码 2001=未认证 / 2003=权限不足 / 4001=限频。
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from ..config import Config
from ..net import apply_network_policy

logger = logging.getLogger(__name__)

BASE = "https://fuyao.aicubes.cn"
MARKET_INDEXES = ["000001.SH", "399106.SZ"]  # 上证综指 + 深证综指 = 全市场成交额
MARKET_BENCH = "000300.SH"                   # 估值基准:沪深300 成分股中位数
SECTOR_COLUMNS = ["symbol", "trade_date", "sector", "sector_amount", "market_amount",
                  "ratio_pct", "level"]
VALUATION_COLUMNS = ["trade_date", "symbol", "sector", "n_stocks", "n_pe", "n_pb",
                     "med_pe_ttm", "med_pb_mrq", "mkt_med_pe", "mkt_med_pb", "rel_pb", "reading"]
CATALYST_ANOMALY_COLUMNS = ["trade_date", "thscode", "stock_name", "tag_name",
                            "keywords", "analysis", "symbol"]
CATALYST_HOT_COLUMNS = ["trade_date", "thscode", "name", "rank", "heat", "rank_change", "symbol"]

CROWD_WARN_PCT = 15.0    # 板块成交占比 ≥15% 警戒
CROWD_DANGER_PCT = 20.0  # ≥20% 强警示(书中"强制退出";系统不自动交易,故为强提示)

VALUATION_BATCH = 100    # 估值快照单次最多 100 个 token(服务端上限)
CHEAP_RATIO = 0.85       # 板块PB/市场PB < 0.85 → 相对便宜
RICH_RATIO = 1.2         # > 1.2 → 相对偏贵


class FuyaoError(RuntimeError):
    pass


def _get(url: str, params: dict) -> dict:
    apply_network_policy(True)  # 强制直连(本机有指向死代理的环境变量)
    key = str(Config().get("fuyao", "api_key", default="") or "").strip()
    if not key:
        raise FuyaoError("缺少同花顺 API key(config/settings.local.yaml fuyao.api_key)")
    resp = requests.get(url, params=params,
                        headers={"X-api-key": key, "User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    js = resp.json()
    if js.get("code") != 0:
        raise FuyaoError(f"{js.get('code')}: {js.get('message')}")
    return js


def fetch_index_snapshot(thscodes: list[str]) -> dict[str, dict]:
    """批量取指数行情快照,返回 {thscode: {turnover, last_price, ...}}。"""
    if not thscodes:
        return {}
    js = _get(f"{BASE}/api/a-share-index/prices/snapshot", {"thscodes": ",".join(thscodes)})
    return {it["thscode"]: it for it in (js.get("data") or {}).get("item", [])}


_TRADING_DAYS_CACHE: set[str] | None = None


def is_trading_day(date_str: str | None = None) -> bool:
    """指定日(yyyyMMdd,默认今天)是否 A股交易日(同花顺日历,近一年窗口)。

    进程内缓存当日历集合(盘中 15 分钟任务每轮调,避免重复请求触发限频)。
    """
    global _TRADING_DAYS_CACHE
    target = date_str or datetime.now().strftime("%Y%m%d")
    if _TRADING_DAYS_CACHE is None:
        try:
            js = _get(f"{BASE}/api/a-share/calendar/trading-days", {})
            _TRADING_DAYS_CACHE = {it["date"] for it in (js.get("data") or {}).get("item", [])}
        except Exception as exc:  # noqa: BLE001
            logger.warning("交易日历获取失败(退化为周末判断): %s", exc)
            return datetime.now().weekday() < 5
    return target in _TRADING_DAYS_CACHE


def etf_thscode(symbol: str) -> str:
    """ETF 代码 → 同花顺 thscode(5 开头沪市,其余深市)。"""
    return f"{symbol}.SH" if symbol.startswith("5") else f"{symbol}.SZ"


def fetch_fund_snapshot(symbol: str) -> dict | None:
    """场内 ETF 实时快照(同花顺):{last_price, volume, turnover, turnover_ratio_pct, ...}。"""
    js = _get(f"{BASE}/api/fund/market/snapshot", {"thscode": etf_thscode(symbol)})
    items = (js.get("data") or {}).get("item", [])
    return items[0] if items else None


def sector_crowding(cfg) -> list[dict]:
    """每只 ETF 的板块成交占比(板块成交额 / 全市场成交额 ×100)。

    板块→行业指数映射在 config/settings.yaml fuyao.sector_map(未配置的标的跳过)。
    两次快照调用:一次取全部板块指数、一次取市场指数。
    """
    smap = cfg.get("fuyao", "sector_map", default={}) or {}
    if not smap:
        return []
    all_idx = sorted({t.strip().upper() for entry in smap.values()
                      for t in entry.get("thscodes", []) if t.strip()})
    snaps = fetch_index_snapshot(all_idx) if all_idx else {}
    market = fetch_index_snapshot(MARKET_INDEXES)
    market_amount = sum((market.get(t) or {}).get("turnover") or 0 for t in MARKET_INDEXES)

    out = []
    for item in cfg.watchlist:
        sym = str(item["symbol"])
        entry = smap.get(sym)
        if not entry:
            continue
        idxs = [t.strip().upper() for t in entry.get("thscodes", [])]
        sector_amount = sum((snaps.get(t) or {}).get("turnover") or 0 for t in idxs)
        ratio = sector_amount / market_amount * 100 if market_amount else None
        level = "正常"
        if ratio is not None and ratio >= CROWD_DANGER_PCT:
            level = "强警示"
        elif ratio is not None and ratio >= CROWD_WARN_PCT:
            level = "警戒"
        out.append({
            "symbol": sym,
            "name": str(item.get("name", "")),
            "sector": str(entry.get("name", "")),
            "sector_amount": sector_amount,
            "market_amount": market_amount,
            "ratio_pct": round(ratio, 2) if ratio is not None else None,
            "level": level,
        })
    return out


# --------------------------------------------------------------------------- 估值(板块估值体检)

def fetch_constituents(thscode: str) -> list[dict]:
    """同花顺指数成分股(thscode → [{thscode, ticker, name}])。"""
    js = _get(f"{BASE}/api/a-share-index/constituents/ths-stock-list", {"thscode": thscode})
    return list((js.get("data") or {}).get("item", []))


def fetch_valuations(thscodes: list[str]) -> dict[str, dict]:
    """批量取 A 股估值快照,返回 {thscode: {pe_ttm, pe_mrq, pb_mrq, ps_ttm, pcf_ttm}}。

    单次最多 100 个,超出分批;上游无该股数据时该股不返回。
    """
    out: dict[str, dict] = {}
    codes = [c.strip().upper() for c in thscodes if c.strip()]
    for i in range(0, len(codes), VALUATION_BATCH):
        chunk = codes[i:i + VALUATION_BATCH]
        js = _get(f"{BASE}/api/a-share/valuations/snapshot", {"thscodes": ",".join(chunk)})
        for it in (js.get("data") or {}).get("item", []):
            out[it["thscode"]] = it
    return out


def _median_pos(values: list[float | None]) -> tuple[float | None, int]:
    """正数集合的中位数 + 有效数(排除 None 与 <=0)。"""
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return None, 0
    n = len(vals)
    mid = n // 2
    return (float(vals[mid]) if n % 2 else float((vals[mid - 1] + vals[mid]) / 2)), n


def sector_valuation(cfg) -> list[dict]:
    """板块估值体检:行业指数成分股的中位数 PE/PB vs 沪深300 基准。

    - 板块 = 各 ETF 的 sector_map 行业指数成分股并集
    - 中位数:PE_TTM 与 PB_MRQ,排除负值/空值
    - 相对读数按 PB:板块PB/市场PB <0.85 便宜 / >1.2 偏贵 / 否则中性
    - 每日快照入库(sector_valuation),持续累计后可用作估值分位
    """
    import statistics

    smap = cfg.get("fuyao", "sector_map", default={}) or {}
    if not smap:
        return []
    # 市场基准(沪深300)一次
    bench_stocks = [c["thscode"] for c in fetch_constituents(MARKET_BENCH)]
    bench_val = fetch_valuations(bench_stocks)
    mkt_pe, _ = _median_pos([v.get("pe_ttm") for v in bench_val.values()])
    mkt_pb, _ = _median_pos([v.get("pb_mrq") for v in bench_val.values()])

    out = []
    for item in cfg.watchlist:
        sym = str(item["symbol"])
        entry = smap.get(sym)
        if not entry:
            continue
        stocks: list[str] = []
        for t in entry.get("thscodes", []):
            try:
                stocks.extend(c["thscode"] for c in fetch_constituents(t.strip().upper()))
            except Exception:  # noqa: BLE001
                logger.warning("[%s] 成分股获取失败: %s", sym, t)
        stocks = list(dict.fromkeys(stocks))  # 去重保序
        vals = fetch_valuations(stocks)
        med_pe, n_pe = _median_pos([v.get("pe_ttm") for v in vals.values()])
        med_pb, n_pb = _median_pos([v.get("pb_mrq") for v in vals.values()])

        rel_pb = round(med_pb / mkt_pb, 3) if (med_pb and mkt_pb) else None
        reading = "NA"
        if rel_pb is not None and n_pb >= 10:
            if rel_pb < CHEAP_RATIO:
                reading = "便宜"
            elif rel_pb > RICH_RATIO:
                reading = "偏贵"
            else:
                reading = "中性"
        out.append({
            "symbol": sym,
            "sector": str(entry.get("name", "")),
            "n_stocks": len(stocks),
            "n_pe": n_pe,
            "n_pb": n_pb,
            "med_pe_ttm": round(med_pe, 1) if med_pe else None,
            "med_pb_mrq": round(med_pb, 2) if med_pb else None,
            "mkt_med_pe": round(mkt_pe, 1) if mkt_pe else None,
            "mkt_med_pb": round(mkt_pb, 2) if mkt_pb else None,
            "rel_pb": rel_pb,
            "reading": reading,
        })
    return out


# --------------------------------------------------------------------------- 催化剂(异动+热榜)

def fetch_anomalies(tag_codes: list[str] | None = None) -> list[dict]:
    """当日全市场个股异动原因列表(可选按标签过滤)。

    返回 [{thscode, stock_name, tag_name, keyword_list, analysis_content}]。
    """
    params = {}
    if tag_codes:
        params["tag_codes"] = ",".join(tag_codes)
    js = _get(f"{BASE}/api/a-share/special-data/anomaly-analysis-list", params)
    return list((js.get("data") or {}).get("item", []))


def fetch_hot_list(period: str = "day") -> list[dict]:
    """A股热股榜单 Top30(period: day=24小时 / hour=小时级)。"""
    js = _get(f"{BASE}/api/a-share/special-data/hot-stock-list", {"period": period})
    return list((js.get("data") or {}).get("item", []))


def catalyst_context(cfg) -> dict:
    """催化剂上下文(确定性):当日全市场异动 + 热榜,按板块成分股归属到各 ETF。

    返回 {"anomalies": [全市场异动], "hot": [Top30], "matched": {thscode: symbol},
          "sectors": [{symbol, name, sector, anomalies:[...], hot:[...]}]}。
    异动/热榜中属于本池板块的股票标记 symbol;其余 symbol=None(报告里可忽略)。
    """
    smap = cfg.get("fuyao", "sector_map", default={}) or {}
    anomalies = fetch_anomalies()
    hot = fetch_hot_list()

    # 各板块成分股 → 归属映射(thscode → symbol)
    matched: dict[str, str] = {}
    for item in cfg.watchlist:
        sym = str(item["symbol"])
        entry = smap.get(sym)
        if not entry:
            continue
        for t in entry.get("thscodes", []):
            try:
                for c in fetch_constituents(t.strip().upper()):
                    matched[c["thscode"]] = sym
            except Exception:  # noqa: BLE001
                logger.warning("[%s] 成分股获取失败(催化剂): %s", sym, t)

    sectors = []
    for item in cfg.watchlist:
        sym = str(item["symbol"])
        entry = smap.get(sym)
        if not entry:
            continue
        sa = [a for a in anomalies if matched.get(a.get("thscode")) == sym]
        sh = [h for h in hot if matched.get(h.get("thscode")) == sym]
        sectors.append({
            "symbol": sym,
            "name": str(item.get("name", "")),
            "sector": str(entry.get("name", "")),
            "anomalies": sa,
            "hot": sh,
        })
    return {"anomalies": anomalies, "hot": hot, "matched": matched, "sectors": sectors}
