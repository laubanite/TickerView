# -*- coding: utf-8 -*-
"""个股日K离线拉取(腾讯 fqkline,前复权)——回测基建,与线上 fetchers 解耦。

- 行序与 ETF 接口不同:[date, open, close, high, low, volume] → 统一为
  trade_date/open/high/low/close/volume(load_daily_csv 兼容);
- 符号:纯数字 code → sh/sz 前缀(6/688→sh,其余→sz 的 Wind 代码→code 映射由调用方给);
- 停牌日:腾讯日K不含停牌 bar(连续交易日序列),相邻 bar 即上一交易日,prev_close 直接取前一根;
- 分页(2026-09-04):接口单次上限 800 根,从最新往回按 end=最早日期-1 翻页,
  直到覆盖 fetch_start(默认 2019-07-01,保证 2021 年起 since 成员的 250 根 warmup);
- 完整性检查:缓存首日晚于 fetch_start+90 自然日 → 判为残缺(如只有末段 800 根)自动重拉覆盖;
- 重试:SSL/连接瞬断重试 3 次(间隔 1/2/4s),仍失败抛给调用方统计。
"""
from __future__ import annotations

import datetime as _dt
import time as _time

import pandas as pd
import requests

_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_CACHE = __import__("pathlib").Path(__file__).resolve().parent.parent / "data" / "kline_cache"
_PAGE = 800          # 接口单次上限
_FETCH_START = "2019-07-01"   # 默认回补起点(2021-01 since + 250根 warmup 余量)


def sec_id(code: str) -> str:
    """Wind 代码(300308.SZ / 603986.SH)或纯数字 → 腾讯 secid(小写)。"""
    c = code.strip().lower()
    if "." in c:
        base, mkt = c.split(".")
        return f"{'sh' if mkt == 'sh' else 'sz'}{base}"
    if c.startswith(("6", "5", "9")):
        return f"sh{c}"
    return f"sz{c}"


def _get_rows(sec: str, start: str, end: str) -> list:
    """单页请求,SSL/连接瞬断重试 3 次。"""
    last = None
    for i in range(3):
        try:
            r = requests.get(_URL,
                             params={"param": f"{sec},day,{start},{end},{_PAGE},qfq"},
                             timeout=20)
            r.raise_for_status()
            d = r.json()
            data = (d.get("data") or {}).get(sec) or {}
            rows = data.get("qfqday") or data.get("day") or []
            return rows
        except Exception as e:  # noqa: BLE001
            last = e
            _time.sleep(1 * (2 ** i))
    raise RuntimeError(f"{sec} 请求失败(重试3次): {last}")


def _parse_rows(rows: list) -> list[dict]:
    out = []
    for row in rows:
        # [date, open, close, high, low, volume(手)]
        out.append({"trade_date": str(row[0])[:10], "open": float(row[1]),
                    "high": float(row[3]), "low": float(row[4]),
                    "close": float(row[2]), "volume": float(row[5])})
    return out


def fetch_stock_daily(code: str, start: str = _FETCH_START,
                      end: str = "2030-12-31") -> pd.DataFrame:
    """分页拉取 [start, end] 全量日K(从最新往回翻页合并)。"""
    sec = sec_id(code)
    chunks: dict[str, dict] = {}
    page_end = end
    for _ in range(8):                      # 8页=6400根,足够 2019→2026
        rows = _get_rows(sec, start, page_end)
        if not rows:
            break
        parsed = _parse_rows(rows)
        for r in parsed:
            chunks[r["trade_date"]] = r
        earliest = min(r["trade_date"] for r in parsed)
        if len(rows) < _PAGE or earliest <= start:
            break
        # 下一页:end = 最早日期的前一天(覆盖重叠,按日期去重)
        d = _dt.date.fromisoformat(earliest) - _dt.timedelta(days=1)
        page_end = d.isoformat()
        _time.sleep(0.15)
    if not chunks:
        raise RuntimeError(f"{code} 日K为空")
    df = pd.DataFrame(sorted(chunks.values(), key=lambda r: r["trade_date"]))
    return df.reset_index(drop=True)


def ensure_stock_cache(code: str, start: str = _FETCH_START) -> pd.DataFrame:
    """缓存优先;残缺(首日晚于 start+90 自然日)自动重拉;缺则拉取并落
    data/kline_cache/{code}_daily.csv。"""
    p = _CACHE / f"{code}_daily.csv"
    if p.exists():
        df = pd.read_csv(p, dtype={"trade_date": str})
        if len(df) > 60:
            first = str(df["trade_date"].iloc[0])[:10]
            stale_after = (_dt.date.fromisoformat(start)
                           + _dt.timedelta(days=90)).isoformat()
            if first <= stale_after:
                return df                       # 完整,直接用
            # 残缺(只有末段)→ 落穿重拉覆盖
    df = fetch_stock_daily(code, start)
    _CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8")
    return df
