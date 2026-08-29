# -*- coding: utf-8 -*-
"""离线刷新 fixture 的操作参数行(腾讯 501/不可达时可用)。

fixture 行情数据不动(拉取时点一致),只把「操作参数(程序锚点)」/「回踩带」
等由规则计算的文本按当前 param_anchors 重算替换——保证 fixture 语义
与当前规则一致,不阻塞等网络。腾讯恢复后跑 _gen_fixtures.py 全量重拉。

用法:python scripts/_refresh_fixtures.py
"""
import glob
import os
import re
import sys
sys.path.insert(0, "E:/AgentProjects/AlphaPrism")
from alphaprism.planner.intraday import _asset_rows, _facts_markdown
from alphaprism.planner.intraday_engine import (param_anchors, render_state_md,
                                                signal_state)

FIX = os.path.join("E:/AgentProjects/AlphaPrism", "tests", "fixtures", "snapshots")


def parse_facts(md: str) -> dict:
    etf_part = md.split("## 大盘")[0]
    rows = {}
    for line in etf_part.splitlines():
        m = re.match(r"\| (分时|日线|30分|盘口|资金) \| ([^|]+) \| ([^|]+) \|", line)
        if m:
            rows[(m.group(1), m.group(2).strip())] = m.group(3)
    daily_ma, m30_ma = {}, {}
    for (g, k), v in rows.items():
        if g == "日线" and k == "均线":
            for p, val in re.findall(r"M(\d+)=([\d.]+)", v):
                daily_ma[int(p)] = float(val)
        elif g == "30分" and k == "均线":
            for p, val in re.findall(r"M(\d+)=([\d.]+)", v):
                m30_ma[int(p)] = float(val)
    price = float(rows.get(("分时", "现价"), "0").split()[0])
    recent = re.search(r"近20日区间 ([\d.]+)-([\d.]+)", rows.get(("日线", "位置"), ""))
    m30_range = re.search(r"近60根区间 ([\d.]+)-([\d.]+)", rows.get(("30分", "区间"), ""))
    low = None
    pm = re.search(r"/ ([\d.]+)", rows.get(("分时", "日内高低"), ""))
    if pm:
        low = float(pm.group(1))
    return {"etf": {
        "code": "?", "name": "?",
        "daily": {"ma": daily_ma,
                  "recent_low": float(recent.group(1)) if recent else None,
                  "recent_high": float(recent.group(2)) if recent else None,
                  "swing_low": None,  # 阶段低无法从快照反解 → 跳过(止损保持旧文本引擎重算)
                  "atr20": None},
        "m30": {"ma": m30_ma,
                "range_low": float(m30_range.group(1)) if m30_range else None,
                "range_high": float(m30_range.group(2)) if m30_range else None},
        "minute": {"price": price, "low": low},
    }}


def main() -> None:
    for path in sorted(glob.glob(os.path.join(FIX, "*.md"))):
        code = os.path.basename(path)[:-3]
        with open(path, encoding="utf-8") as f:
            md = f.read()
        facts = parse_facts(md)
        state = signal_state(facts)
        anchors = param_anchors(facts)
        # 只渲染「盘面状态」段,替换旧段(数据快照表主体不动)
        new_state_md = render_state_md(facts, state, anchors, None, None)
        if "## 盘面状态" in md:
            head = md.split("## 盘面状态")[0].rstrip()
            md = head + "\n" + new_state_md.strip() + "\n"
        else:
            md = md.rstrip() + "\n" + new_state_md + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[{code}] 盘面状态段已刷新 (stop={anchors.get('stop_loss')}({anchors.get('stop_src')}))")


if __name__ == "__main__":
    main()