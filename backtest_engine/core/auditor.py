# -*- coding: utf-8 -*-
"""审计日志:每根 bar 的输入上下文快照哈希 + 交易级审计哈希。

- audit_bar:对 [date, open, high, low, close, volume, 指标列] 做规范化序列化 → sha256
  (引擎层全量输入指纹,防"信号无法回溯");
- 交易审计行:date/side/price/shares + 触发理由 + bar 哈希,写入交易记录。
"""
from __future__ import annotations

import hashlib
import json


def bar_fingerprint(date: str, row: dict) -> str:
    """单日 bar 规范化指纹(数值截断到 9 位小数,键序稳定)。"""
    payload = {"date": date}
    for k in ("open", "high", "low", "close", "volume"):
        v = row.get(k)
        payload[k] = round(float(v), 9) if v is not None else None
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def trade_fingerprint(date: str, side: str, price: float, shares: int,
                      reason: str, bar_hash: str) -> str:
    payload = {"date": date, "side": side, "price": price, "shares": shares,
               "reason": reason, "bar": bar_hash}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]