# -*- coding: utf-8 -*-
"""批量拉取 100 只个股日K(后台作业脚本)。

- 全量走 ensure_stock_cache:完整缓存秒回,残缺缓存(首日晚于 2019-09-29)自动重拉补全;
- 输出每只 rows + 首末日,末尾汇总 fail 清单。
"""
import sys
import time

sys.path.insert(0, r"E:\AgentProjects\AlphaPrism")
import yaml
from backtest_engine.stock_fetch import ensure_stock_cache

pool = yaml.safe_load(open(r"E:\AgentProjects\AlphaPrism\backtest_engine\configs\pool_stock_mvp.yaml", encoding="utf-8"))
done, fail, refetched = 0, [], 0
for m in pool["members"]:
    sym = m["symbol"]
    try:
        df = ensure_stock_cache(sym)
        n0, n1 = str(df["trade_date"].iloc[0])[:10], str(df["trade_date"].iloc[-1])[:10]
        if n0 <= "2019-09-29":
            done += 1
        else:
            refetched += 1
        print(f"OK {sym} {m['name']} rows={len(df)} {n0}..{n1}", flush=True)
        time.sleep(0.05)
    except Exception as e:  # noqa: BLE001
        fail.append((sym, str(e)[:80]))
        print(f"FAIL {sym}: {e}", flush=True)
print(f"\ncomplete={done} refetched={refetched} fail={len(fail)}", flush=True)
if fail:
    print("failures:", fail, flush=True)
