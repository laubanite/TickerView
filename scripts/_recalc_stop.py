# -*- coding: utf-8 -*-
"""离线止损档重算:读 fixture 里的现价/均线/位源,重跑 param_anchors 新旧对比。

腾讯 K 线 501 时段不可用,但止损规则变化不需要重新拉行情——
从 fixture 解析出 facts 结构,直接喂 param_anchors 看新规则效果。
"""
import glob
import os
import re
import sys
sys.path.insert(0, "E:/AgentProjects/AlphaPrism")
from alphaprism.planner.intraday_engine import _classify_sources, param_anchors

FIX = os.path.join("E:/AgentProjects/AlphaPrism", "tests", "fixtures", "snapshots")


def parse_facts(md: str) -> dict:
    """从快照 markdown 反解最小 facts(够 param_anchors 用);只取标的表(## 大盘 之前)。"""
    etf_part = md.split("## 大盘")[0]
    rows = {}
    for line in etf_part.splitlines():
        m = re.match(r"\| (分时|日线|30分|盘口|资金) \| ([^|]+) \| ([^|]+) \|", line)
        if m:
            rows[(m.group(1), m.group(2).strip())] = m.group(3)
    daily_ma = {}
    m30_ma = {}
    for (g, k), v in rows.items():
        if g == "日线" and k == "均线":
            for p, val in re.findall(r"M(\d+)=([\d.]+)", v):
                daily_ma[int(p)] = float(val)
        elif g == "30分" and k == "均线":
            for p, val in re.findall(r"M(\d+)=([\d.]+)", v):
                m30_ma[int(p)] = float(val)
    price = float(rows.get(("分时", "现价"), "0").split()[0])
    recent = re.search(r"近20日区间 ([\d.]+)-([\d.]+)", rows.get(("日线", "位置"), ""))
    swing = re.search(r"距阶段高点\(近250日\) ([\d.]+)", rows.get(("日线", "位置"), ""))
    swing_low = None
    m = re.search(r"清仓止损 ([\d.]+)\((\S+?)\)", md)
    if m:
        swing_low = float(m.group(1))     # 旧止损 = 阶段低(250日)
    m30_range = re.search(r"近60根区间 ([\d.]+)-([\d.]+)", rows.get(("30分", "区间"), ""))
    low = None
    hl = rows.get(("分时", "日内高低"), "")
    pm = re.search(r"/ ([\d.]+)", hl)
    if pm:
        low = float(pm.group(1))
    return {
        "etf": {
            "code": "?", "name": "?",
            "daily": {
                "ma": daily_ma,
                "recent_low": float(recent.group(1)) if recent else None,
                "recent_high": float(recent.group(2)) if recent else None,
                "swing_low": swing_low,
                "atr20": None,
            },
            "m30": {
                "ma": m30_ma,
                "range_low": float(m30_range.group(1)) if m30_range else None,
                "range_high": float(m30_range.group(2)) if m30_range else None,
            },
            "minute": {"price": price, "low": low},
        }
    }


for path in sorted(glob.glob(os.path.join(FIX, "*.md"))):
    code = os.path.basename(path)[:-3]
    with open(path, encoding="utf-8") as f:
        md = f.read()
    facts = parse_facts(md)
    etf = facts["etf"]
    d, m, mn = etf["daily"], etf["m30"], etf["minute"]
    price = mn["price"]
    support, _, _, stop_d, labels = _classify_sources(d, m, mn, price)
    a = param_anchors(facts)
    old = None
    m_old = re.search(r"清仓止损 ([\d.]+)\((\S+?)\)", md)
    if m_old:
        old = f"{m_old.group(1)}({m_old.group(2)})"
    new = f"{a.get('stop_loss')}({a.get('stop_src') or '?'})" if a.get("stop_loss") else "?"
    ratio = ""
    if a.get("stop_loss") and price:
        ratio = f" 距现价 {(price - a['stop_loss']) / price * 100:.0f}%"
    print(f"{code}: 现价 {price} | 旧止损 {old} → 新止损 {new}{ratio} | ok={a['ok']}")