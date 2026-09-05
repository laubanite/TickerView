# -*- coding: utf-8 -*-
"""个股实时快照(腾讯 qt 接口)· 盘中个股模式 P1(2026-09-04)。

方案依据:docs/盘中个股模式方案-2026-09.md §2/§8。
字段布局 2026-09-04 探针核实(scripts/_probe_qt.py,88 字段):
  [1]名称 [3]现价 [4]昨收 [5]今开 [6]累计量(手) [30]时戳 [32]涨跌% [33]最高
  [34]最低 [38]换手% [43]振幅% [44]流通市值(亿) [45]总市值(亿) [47]涨停价 [48]跌停价
注意:[49] 是 qt 自带"量比"(分时口径),与已验证的"累计量/20日均量"不同定义,弃用;
量比一律由日线缓存自算(stock_risk 内)。
停牌无显式标志 → volume==0 且 现价==昨收 时判"疑似停牌"(文案已声明行情延迟免责)。
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_QT_URL = "https://qt.gtimg.cn/q="
_TIMEOUT = 8


def _market_prefix(sym: str) -> str:
    if sym.startswith(("6", "9", "5")):
        return "sh"
    if sym.startswith(("4", "8")):
        return "bj"  # 北交所(实验性)
    return "sz"


def _parse_qt_reply(text: str, wanted: set[str]) -> dict[str, dict | None]:
    """qt 应答文本 → {symbol: snapshot dict}(纯函数,离线可测)。"""
    out: dict[str, dict | None] = {s: None for s in wanted}
    for line in text.strip().splitlines():
        f = line.split("~")
        if len(f) < 49:
            continue
        sym = f[2].strip()
        if sym not in out:
            continue

        def _f(i: str, d=None) -> float | None:
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return d

        price = _f(3)
        prev = _f(4)
        vol_hand = _f(6)
        lim_up, lim_dn = _f(47), _f(48)
        out[sym] = {
            "symbol": sym,
            "name": f[1].strip(),
            "price": price,
            "prev_close": prev,
            "open": _f(5),
            "volume_hand": vol_hand,
            "pct_chg": _f(32),
            "high": _f(33),
            "low": _f(34),
            "amplitude_pct": _f(43),
            "turnover_pct": _f(38),
            "float_mv_yi": _f(44),
            "total_mv_yi": _f(45),
            "limit_up": lim_up,
            "limit_down": lim_dn,
            "time": f[30].strip() if len(f) > 30 else "",
            # 派生结构状态(方案 R9;涨停/跌停近似阈值 0.1%)
            "suspended": bool(price and prev and vol_hand == 0 and price == prev),
            "at_limit_up": bool(price and lim_up and price >= lim_up * 0.999),
            "at_limit_down": bool(price and lim_dn and price <= lim_dn * 1.001),
        }
    return out


def fetch_stock_snapshot(symbols: list[str]) -> dict[str, dict | None]:
    """批量拉 qt 实时快照。返回 {symbol: dict|None}(单只失败不影响其余)。"""
    out: dict[str, dict | None] = {s: None for s in symbols}
    if not symbols:
        return out
    q = ",".join(f"{_market_prefix(s)}{s}" for s in symbols)
    try:
        r = requests.get(_QT_URL + q, timeout=_TIMEOUT)
        r.encoding = "gbk"
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("qt 实时快照拉取失败: %s", exc)
        return out
    out = _parse_qt_reply(r.text, set(symbols))
    for s in symbols:  # 服务端可能对未知代码返回空行 → 保持 None 占位
        out.setdefault(s, None)
    return out


def stock_type(sym: str) -> str:
    """标的类型判定(方案 §4:5/1 开头 = ETF,其余 = 个股;北交所 4/8 归个股)。"""
    return "etf" if sym.startswith(("5", "1")) else "stock"
