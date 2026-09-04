# -*- coding: utf-8 -*-
"""个股 MVP 基准对照:
1. 上证指数(1A0001,缓存 sh000001_daily.csv)窗口收益;
2. 同池等权买入持有:只用 2021-01-04 前已上市(数据起于 ≤2021-01-04)的成员,
   等权简单平均收益(不复利、不再平衡),与策略收益同窗对比。
→ 用途:静态池(2026 筛选)幸存者偏差下,拆分『选池贡献』与『策略择时贡献』。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(r"E:\AgentProjects\AlphaPrism")
CACHE = ROOT / "data" / "kline_cache"
START, END = (sys.argv[1] if len(sys.argv) > 1 else "2021-01-01"), "2026-09-03"

# 1) 上证指数
idx = pd.read_csv(CACHE / "sh000001_daily.csv", dtype={"trade_date": str})
idx = idx[(idx["trade_date"] >= START) & (idx["trade_date"] <= END)]
i0, i1 = float(idx["close"].iloc[0]), float(idx["close"].iloc[-1])
print(f"上证指数 {idx['trade_date'].iloc[0]}({i0:.0f}) → {idx['trade_date'].iloc[-1]}({i1:.0f})"
      f"  {i1 / i0 * 100 - 100:+.2f}%")

# 2) 等权买入持有(前上市成员)
import yaml
pool = yaml.safe_load(open(ROOT / "backtest_engine" / "configs" / "pool_stock_mvp.yaml",
                           encoding="utf-8"))
rets, skipped = [], []
for m in pool["members"]:
    p = CACHE / f"{m['symbol']}_daily.csv"
    df = pd.read_csv(p, dtype={"trade_date": str})
    df = df[(df["trade_date"] >= START) & (df["trade_date"] <= END)]
    if df.empty or str(df["trade_date"].iloc[0]) > "2021-01-04":
        skipped.append(m["symbol"])
        continue
    rets.append(float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1)
bh = sum(rets) / len(rets) * 100
print(f"等权买入持有({len(rets)} 只前上市成员,等权简单平均): {bh:+.2f}%")
print(f"(剔除 {len(skipped)} 只晚上市: {','.join(skipped[:12])}{'...' if len(skipped) > 12 else ''})")
