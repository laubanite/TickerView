"""实盘冒烟:build_tech_analysis 结论卡接入(2026-08-27)。"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from alphaprism.config import Config
from alphaprism.planner.intraday import build_tech_analysis

md, deg = build_tech_analysis("515790", Config())
print("degraded:", deg)
print("=== 结论卡区域 ===")
for line in md.splitlines():
    if ("结论卡" in line or line.startswith("- 核心判断") or line.startswith("- 信号类型")
            or line.startswith("- 多周期优先级") or line.startswith("- 触发")
            or line.startswith("- 失效")):
        print(line)
print("raw json leaked:", '{"conclusion"' in md)
print("len(md):", len(md))