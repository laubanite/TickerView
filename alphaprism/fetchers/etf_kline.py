"""ETF K线抓取:腾讯源直连(日线 qfq / 30分钟),新浪源补当日 bar。

背景(2026-08-11 实测):东方财富 push2* 接口对 requests 存在 TLS/WAF 指纹拦截
(RemoteDisconnected),本机不可用,故 K线改用腾讯行情接口(稳定直连):
- 日线:   web.ifzq.gtimg.cn /appstock/app/fqkline/get  前复权(qfq)
- 30分钟: ifzq.gtimg.cn  /appstock/app/kline/mkline    30分K
- 当日bar: quotes.sina.cn getKLineData(腾讯日线滞后当日,收盘后由新浪补当日)

数据要点:
- 腾讯 fqkline 单次最多约 640 根,超长区间按天分片拉取后拼接去重
- 新浪日线量单位为"股",腾讯为"手",补当日时 ÷100 统一
- 新浪日线(scale=240)无成交额字段,日线 amount 暂为空(阶段3 量价模块以量为主)
- 涨跌幅由收盘价序列计算后入库
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Sequence

import pandas as pd
import requests

from ..db import KLINE30M_COLUMNS, KLINE_COLUMNS

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
}

# 每片最大天数(日线约 220 交易日/年,640 根上限留余量)
_CHUNK_DAYS = 300


def _tx_symbol(symbol: str) -> str:
    """ETF 代码 → 腾讯符号:5 开头(沪)加 sh,1 开头(深)加 sz。"""
    prefix = "sh" if symbol.startswith("5") else "sz"
    return f"{prefix}{symbol}"


def fetch_name(symbol: str) -> str | None:
    """腾讯实时接口取 ETF 名称(池管理用)。无效代码返回 None。

    自清理代理环境变量(本机 ALL_PROXY 指向未运行代理,直连国内源需清除)。
    """
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    tsym = _tx_symbol(symbol)
    try:
        resp = requests.get(f"https://qt.gtimg.cn/q={tsym}", headers=HEADERS, timeout=10)
        resp.encoding = "gbk"
        line = resp.text.strip().split(";")[0]
        if "=" not in line:
            return None
        fields = line.split("=")[1].strip('"').split("~")
        return fields[1] if len(fields) > 2 and fields[1] else None
    except Exception:  # noqa: BLE001
        return None


def _date_chunks(start: str, end: str, max_days: int = _CHUNK_DAYS) -> list[tuple[str, str]]:
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    chunks: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        nxt = min(e, cur + pd.DateOffset(days=max_days))
        chunks.append((cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
        cur = nxt + pd.Timedelta(days=1)
    return chunks


def _get_json(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- 日线(腾讯前复权)

def _fetch_tencent_daily_bars(symbol: str, start_date: str, end_date: str) -> list[list]:
    tsym = _tx_symbol(symbol)
    bars: list[list] = []
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    for beg, end in _date_chunks(start_date, end_date):
        params = {"param": f"{tsym},day,{beg},{end},640,qfq"}
        js = _get_json(url, params)
        data = (js.get("data") or {}).get(tsym) or {}
        chunk = data.get("qfqday") or data.get("day") or []
        bars.extend(chunk)
    # 去重(分片边界可能重叠,保留后者)
    seen: set[str] = set()
    dedup: list[list] = []
    for bar in bars:
        if bar and bar[0] not in seen:
            seen.add(bar[0])
            dedup.append(bar)
    return dedup


def _tencent_bars_to_df(bars: list[list], symbol: str) -> pd.DataFrame:
    """腾讯日线 bar [date, open, close, high, low, volume] → 内部字段 DataFrame。"""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars, columns=["trade_date", "open", "close", "high", "low", "volume"])
    df["symbol"] = symbol
    df["amount"] = None
    df["turnover"] = None
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["pct_chg"] = (df["close"].pct_change() * 100).round(4)
    return df[["symbol", "trade_date", "open", "high", "low", "close",
               "volume", "amount", "pct_chg", "turnover"]]


# --------------------------------------------------------------------------- 当日 bar 补充(新浪)

def _parse_jsonp(text: str) -> list[dict]:
    """新浪 jsonp(形如 var _data=([...]);) → 数据数组。"""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(f"新浪响应无 JSON 数组: {text[:100]}")
    return json.loads(m.group(0))


def _fetch_sina_daily_rows(symbol: str, datalen: int = 800) -> list[dict]:
    tsym = _tx_symbol(symbol)
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketDataService."
        f"getKLineData?symbol={tsym}&scale=240&ma=no&datalen={datalen}"
    )
    text = requests.get(url, headers=HEADERS, timeout=15).text
    return _parse_jsonp(text)


def _append_today_if_needed(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """腾讯日线可能滞后当日(收盘后仍显示昨日);用新浪当日 bar 补最新一天。

    新浪量为"股",腾讯为"手",补当日时 ÷100 统一;新浪日线无成交额,amount 留空。
    最新一日前复权价 = 原始价,补入的原始 OHLC 与 qfq 序列一致。
    """
    if df.empty:
        return df
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    if today <= df["trade_date"].max():
        return df
    try:
        rows = _fetch_sina_daily_rows(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] 新浪当日 bar 补充失败(维持腾讯最新一日): %s", symbol, exc)
        return df
    latest = next((r for r in rows if r["day"][:10] == today), None)
    if latest is None:
        return df
    vol = _to_float(latest.get("volume"))
    new_row = {
        "symbol": symbol,
        "trade_date": today,
        "open": _to_float(latest.get("open")),
        "high": _to_float(latest.get("high")),
        "low": _to_float(latest.get("low")),
        "close": _to_float(latest.get("close")),
        "volume": None if vol is None else vol / 100,
        "amount": None,
        "pct_chg": float("nan"),
        "turnover": float("nan"),
    }
    new_df = pd.DataFrame([new_row]).astype({"pct_chg": "float64", "turnover": "float64"})
    df = pd.concat([df, new_df], ignore_index=True)
    df["pct_chg"] = (df["close"].pct_change() * 100).round(4)
    return df.sort_values("trade_date").reset_index(drop=True)


# --------------------------------------------------------------------------- 30分钟(腾讯)

def fetch_30m(symbol: str, count: int = 320) -> pd.DataFrame:
    """腾讯 30 分钟线(ifzq.gtimg.cn 直连,避开 web3 重定向)。"""
    tsym = _tx_symbol(symbol)
    url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    params = {"param": f"{tsym},m30,,{count}"}
    js = _get_json(url, params)
    data = (js.get("data") or {}).get(tsym) or {}
    bars = data.get("m30") or []
    if not bars:
        return pd.DataFrame()

    def parse_ts(v: str) -> str:
        return f"{v[:4]}-{v[4:6]}-{v[6:8]} {v[8:10]}:{v[10:12]}:00"

    df = pd.DataFrame([b[:6] for b in bars],
                      columns=["ts", "open", "close", "high", "low", "volume"])
    df["symbol"] = symbol
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = df["ts"].map(parse_ts)
    df["amount"] = None
    return df[["symbol", "ts", "open", "high", "low", "close", "volume", "amount"]]


# --------------------------------------------------------------------------- 分时(腾讯,1分钟)

def fetch_minute(symbol: str) -> pd.DataFrame:
    """腾讯当日分时(web.ifzq.gtimg.cn /appstock/app/minute/query)。

    每点格式:"0930 0.869 979 85075.00" = 时间 价格 累计量(手) 累计额(元)。
    均价 = 累计额 / 累计量(产品方案 §六,2026-08-19 实测 ETF+指数均可用)。
    返回字段:ts(09:30:00) price vol(单分钟量=累计量差分) amount(累计额)
             avg_price(均价) prev_close(昨收,来自 qt[4])。
    """
    tsym = _tx_symbol(symbol)
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    js = _get_json(url, {"code": tsym})
    data = (js.get("data") or {}).get(tsym) or {}
    points = (data.get("data") or {}).get("data") or []
    if not points:
        return pd.DataFrame()

    rows = []
    prev = 0.0
    for p in points:
        parts = p.split()
        if len(parts) < 4:
            continue
        ts_raw, price, cum_vol, cum_amt = parts[0], parts[1], parts[2], parts[3]
        try:
            price_f = float(price)
            cum_vol_f = float(cum_vol)
            cum_amt_f = float(cum_amt)
        except ValueError:
            continue
        # 时间 0930 → 09:30:00
        tt = f"{ts_raw[:2]}:{ts_raw[2:4]}:00"
        vol = max(cum_vol_f - prev, 0.0)
        prev = cum_vol_f
        # 累计量为"手"(×100 股),均价 = 累计额 / (累计量×100)
        avg = cum_amt_f / (cum_vol_f * 100) if cum_vol_f > 0 else price_f
        rows.append({"ts": tt, "price": price_f, "vol": vol,
                     "amount": cum_amt_f, "avg_price": round(avg, 4)})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # 昨收:qt 列表 index 4(实测:['1','名称','code',今收,昨收,今开,...])。
    # 注意 index 3 是今收,不是昨收——取错会导致涨跌恒为 0。
    prev_close = None
    qt = data.get("qt")
    if isinstance(qt, dict):
        for v in qt.values():
            if isinstance(v, list) and len(v) > 4:
                try:
                    prev_close = float(v[4])
                    break
                except (ValueError, TypeError):
                    continue
    df["prev_close"] = prev_close
    df["symbol"] = symbol
    return df[["symbol", "ts", "price", "vol", "amount", "avg_price", "prev_close"]]


# --------------------------------------------------------------------------- 对外接口

def fetch_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """腾讯前复权日线 + 新浪补当日 bar,返回内部字段 DataFrame。"""
    bars = _fetch_tencent_daily_bars(symbol, start_date, end_date)
    df = _tencent_bars_to_df(bars, symbol)
    df = _append_today_if_needed(df, symbol)
    logger.info("[%s] 日线 %d 根 (%s ~ %s)", symbol, len(df),
                df["trade_date"].min() if len(df) else "-",
                df["trade_date"].max() if len(df) else "-")
    return df


def build_weekly(df_daily: pd.DataFrame) -> pd.DataFrame:
    """从日线重采样周线。周标签取该周最后一个实际交易日(避免未来日期)。"""
    if df_daily.empty:
        return pd.DataFrame()
    d = df_daily.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    d = d.sort_values("trade_date")
    g = d.groupby(d["trade_date"].dt.to_period("W-FRI"))
    weekly = g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
        turnover=("turnover", "sum"),
        _end=("trade_date", "max"),
    )
    weekly["trade_date"] = weekly["_end"].dt.strftime("%Y-%m-%d")
    weekly["pct_chg"] = weekly["close"].pct_change() * 100
    weekly = weekly.drop(columns=["_end"])
    weekly = weekly.reset_index(drop=True)
    weekly["symbol"] = df_daily["symbol"].iloc[0]
    return weekly[["symbol", "trade_date", "open", "high", "low", "close",
                   "volume", "amount", "pct_chg", "turnover"]]


def _nan_to_none(row: tuple) -> tuple:
    return tuple(None if (isinstance(x, float) and pd.isna(x)) else x for x in row)


def daily_to_rows(df: pd.DataFrame, symbol: str) -> list[tuple]:
    return [_nan_to_none(r) for r in df[KLINE_COLUMNS].itertuples(index=False, name=None)]


def weekly_to_rows(df: pd.DataFrame, symbol: str) -> list[tuple]:
    return [_nan_to_none(r) for r in df[KLINE_COLUMNS].itertuples(index=False, name=None)]


def min_to_rows(df: pd.DataFrame, symbol: str) -> list[tuple]:
    return [_nan_to_none(r) for r in df[KLINE30M_COLUMNS].itertuples(index=False, name=None)]


def _to_float(value) -> float | None:
    try:
        if value is None or pd.isna(value) or value == "":
            return None
        return float(value)
    except (ValueError, TypeError):
        return None


DAILY_COLUMNS = KLINE_COLUMNS
WEEKLY_COLUMNS = KLINE_COLUMNS
MIN30_COLUMNS = KLINE30M_COLUMNS
