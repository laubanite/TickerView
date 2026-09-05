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
    """代码 → 腾讯符号:5/6/9 开头(沪:ETF 与沪股),1/3/0 开头(深),4/8 开头(北交所)。"""
    if symbol.startswith(("5", "6", "9")):
        prefix = "sh"
    elif symbol.startswith(("4", "8")):
        prefix = "bj"
    else:
        prefix = "sz"
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

def _fetch_tsym_daily_bars(tsym: str, start_date: str, end_date: str) -> list[list]:
    """按腾讯符号拉日线(指数/ETF 通用;指数无前复权,取 day 原始序列)。"""
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


def _fetch_tencent_daily_bars(symbol: str, start_date: str, end_date: str) -> list[list]:
    return _fetch_tsym_daily_bars(_tx_symbol(symbol), start_date, end_date)


def fetch_index_daily(ths_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """指数日线(腾讯)。ths_code 形如 000001.SH → 腾讯 sh000001。

    用于催化验证的超额基准(方案 M4)。指数无前复权,取 day 原始序列。
    """
    code, mkt = ths_code.split(".")
    tsym = ("sh" if mkt.upper() == "SH" else "sz") + code
    bars = _fetch_tsym_daily_bars(tsym, start_date, end_date)
    return _tencent_bars_to_df(bars, tsym)


def _tencent_bars_to_df(bars: list[list], symbol: str) -> pd.DataFrame:
    """腾讯日线 bar [date, open, close, high, low, volume, ...] → 内部字段 DataFrame。

    个股 bar 比 ETF 多出成交额等尾随字段(2026-09-04 实测 7 列),取前 6 列并
    对不足行补齐 —— 否则 pd.DataFrame 按 6 列名建 7 列数据直接抛
    "6 columns passed, passed data had 7 columns"(个股日K 报错的根因)。
    """
    if not bars:
        return pd.DataFrame()
    rows = []
    for b in bars:
        r = list(b[:6])
        if len(r) < 6:
            r += [None] * (6 - len(r))
        rows.append(r)
    df = pd.DataFrame(rows, columns=["trade_date", "open", "close", "high", "low", "volume"])
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

def fetch_30m(symbol: str, count: int = 320, tsym: str | None = None) -> pd.DataFrame:
    """腾讯 30 分钟线(ifzq.gtimg.cn 直连,避开 web3 重定向)。

    tsym 覆盖:指数等非 ETF 标的直接传腾讯符号(如 sh000001),不走 _tx_symbol(会把
    000001 误判成 sz000001=平安银行)。
    """
    tsym = tsym or _tx_symbol(symbol)
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

def fetch_minute(symbol: str, tsym: str | None = None) -> pd.DataFrame:
    """腾讯当日分时(web.ifzq.gtimg.cn /appstock/app/minute/query)。

    每点格式:"0930 0.869 979 85075.00" = 时间 价格 累计量(手) 累计额(元)。
    均价 = 累计额 / 累计量(产品方案 §六,2026-08-19 实测 ETF+指数均可用)。
    返回字段:ts(09:30:00) price vol(单分钟量=累计量差分) amount(累计额)
             avg_price(均价) prev_close(昨收,来自 qt[4])。
    tsym 覆盖:指数等非 ETF 标的直接传腾讯符号(如 sh000001)。
    """
    tsym = tsym or _tx_symbol(symbol)
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


# --------------------------------------------------------------------------- ETF 规模/份额(天天基金)

def fetch_etf_fund_flow(symbol: str, nav: float | None = None) -> dict:
    """ETF 份额(天天基金 gmbd 接口):期末总份额(亿份) + 份额变化 + 估算规模。

    东财 push2* 行情接口被 WAF 拦,但 fundf10.eastmoney.com 基金数据接口可用
    (2026-08-23 实测,需带 Referer)。份额为基金公司披露(季报 + 份额变动公告,
    如 2026-07-09 期末 517.70亿份),比等季报规模更及时。
    估算规模 = 最新份额 × 最新净值(nav 传入,默认用盘口 IOPV)。
    ETF 资金信号以份额变化 + 折溢价为主,内外盘/大单净流入参考性弱(套利机制)。
    失败返回 {}。
    """
    try:
        url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
               f"?type=gmbd&mode=0&code={symbol}&rt=0.123")
        headers = {**HEADERS, "Referer": f"http://fundf10.eastmoney.com/gmbd_{symbol}.html"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "utf-8"
        # 响应为 JSONP:content:"<table>...</table>",直接抽 td 单元格(每行 6 格:
        # 日期/期间申购/期间赎回/期末总份额/期末净资产/净资产变动率)
        tds = re.findall(r"<td[^>]*>([^<]*)</td>", resp.text)
        rows = [tds[i:i + 6] for i in range(0, len(tds), 6)]
        data = []
        for r in rows:
            if len(r) >= 4 and re.match(r"^\d{4}-\d{2}-\d{2}$", r[0].strip()):
                share = r[3].strip()
                if share and share != "---":
                    data.append({"date": r[0].strip(), "share": float(share)})
        if not data:
            return {}
        latest, prev = data[0], (data[1] if len(data) > 1 else None)
        out = {"share": latest["share"], "date": latest["date"]}
        if prev and prev["share"]:
            chg = latest["share"] - prev["share"]
            out["share_chg"] = round(chg, 2)
            out["share_chg_pct"] = round(chg / prev["share"] * 100, 2)
        if nav:
            out["scale_est"] = round(latest["share"] * nav, 2)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] ETF 份额获取失败: %s", symbol, exc)
        return {}


# --------------------------------------------------------------------------- 实时盘口(腾讯 qt)

def fetch_orderbook(symbol: str, tsym: str | None = None) -> dict:
    """腾讯实时盘口(场内基金):五档买卖 / IOPV / 溢价率 / 量比 / 换手 / 外内盘。

    qt.gtimg.cn 88 字段布局(2026-08-22 实测 sz159516):
      9-18 买1-5(价,量)  19-28 卖1-5(价,量)  7/8 外盘/内盘(手)
      33/34 交易所日内最高/最低  38 换手率%  49 量比  50 委差
      77 溢价率%  78 IOPV
    指数(sh000001)无五档字段 → 返回空 buy/sell,量比/换手仍可用。失败返回 {}。
    """
    tsym = tsym or _tx_symbol(symbol)
    try:
        resp = requests.get(f"https://qt.gtimg.cn/q={tsym}", headers=HEADERS, timeout=10)
        resp.encoding = "gbk"
        line = resp.text.strip().split(";")[0]
        if "=" not in line:
            return {}
        f = line.split("=")[1].strip('"').split("~")
        if len(f) < 30:
            return {}

        def _f(i):
            try:
                return float(f[i]) if f[i] else None
            except (ValueError, TypeError):
                return None

        def _level(price_i, vol_i):
            p = _f(price_i)
            # 指数无五档:字段为 0 → 视为空,避免渲染 0.000/0手 伪档位
            return {"price": p if p else None, "vol": _f(vol_i)}

        buy = [_level(9 + i * 2, 10 + i * 2) for i in range(5)]
        sell = [_level(19 + i * 2, 20 + i * 2) for i in range(5)]
        return {
            "buy": buy, "sell": sell,
            "high": _f(33), "low": _f(34),   # 交易所日内最高/最低(分时采样会漏极值点)
            "iopv": _f(78), "premium_pct": _f(77),
            "vol_ratio": _f(49), "turnover_pct": _f(38),
            "weicha": _f(50), "waipan": _f(7), "neipan": _f(8),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] 盘口获取失败: %s", symbol, exc)
        return {}


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
