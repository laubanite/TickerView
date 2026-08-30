"""生产 verifier 路径冒烟(2026-08-27):build_tech_analysis(卡先行+候选选优+复核+卡)。"""
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from alphaprism.config import Config
from alphaprism.planner.intraday import build_tech_analysis

t0 = time.time()
md, deg = build_tech_analysis("515790", Config())
dt = time.time() - t0
print(f"degraded={deg} | 耗时 {dt:.1f}s | len={len(md)}")
for line in md.splitlines():
    if any(line.startswith(p) for p in ("## 一", "## 八", "## 建议类别", "- 核心判断",
                                        "- 信号类型", "- 触发", "- 失效")) \
            or "结论卡" in line or "风控复核" in line:
        print("  " + line[:120])
print("raw json leaked:", '{"conclusion"' in md)
print("卡重复次数:", md.count("📋 结论卡"))
print("包含运行:(按日志 verifier 生产 行确认候选选优)")