# -*- coding: utf-8 -*-
"""合并三层 Wind 池 → 清单 + pool_stock_mvp.yaml(临时脚本)。"""
import sys
import json

sys.path.insert(0, r"E:\AgentProjects\AlphaPrism")

FILES = {
    "ls300": (r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_ls300.json", "宽基蓝筹", 30),
    "zz500": (r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_zz500.json", "中盘成长", 50),
    "zz1000": (r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_zz1000.json", "小盘弹性", 20),
}
pool = []
for layer, (f, label, _n) in FILES.items():
    raw = open(f, encoding="utf-8-sig").read()
    txt = json.loads(json.loads(raw)["content"][0]["text"])
    tbl = txt["data"]["data"][0]
    cols = [c["name"] for c in tbl["columns"]]
    i_code, i_name, i_ipo = cols.index("Wind代码"), cols.index("证券简称"), cols.index("首发上市日期")
    for r in tbl["rows"]:
        pool.append({"code": r[i_code], "name": r[i_name], "layer": label,
                     "ipo": r[i_ipo] if r[i_ipo] else ""})
print("total:", len(pool))
dup = {}
for p in pool:
    dup[p["code"]] = dup.get(p["code"], 0) + 1
print("重复:", {k: v for k, v in dup.items() if v > 1} or "无")

with open(r"E:\AgentProjects\AlphaPrism\data\wind_req\pool_merged.json", "w", encoding="utf-8") as w:
    json.dump(pool, w, ensure_ascii=False, indent=1)

# 生成 pool YAML(声明式分批起点 = max(2021-01-01, IPO+2年))
import datetime
lines = ["# 个股 MVP 候选池(100 只,三层,2026-09-03 Wind 筛选,静态池+前视声明见报告)",
         "# 分层:沪深300流动性前30 / 中证500前50 / 中证1000前20(日均>2亿/市值>50亿)",
         "name: pool_stock_mvp", "", "members:"]
for p in pool:
    ipo = p["ipo"][:10]
    since = "2021-01-01"
    if ipo:
        d = datetime.date.fromisoformat(ipo) + datetime.timedelta(days=730)
        if d > datetime.date(2021, 1, 1):
            since = d.isoformat()
    sym = p["code"].lower().replace(".sh", "").replace(".sz", "")
    lines.append(f'  - {{symbol: "{sym}", since: "{since}", name: "{p["name"]}", layer: "{p["layer"]}"}}')
open(r"E:\AgentProjects\AlphaPrism\backtest_engine\configs\pool_stock_mvp.yaml", "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("saved pool_stock_mvp.yaml, members:", len(pool))
for p in pool[:6]:
    print(" ", p)