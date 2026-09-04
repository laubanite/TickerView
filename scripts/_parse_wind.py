# -*- coding: utf-8 -*-
"""解析 Wind 搜索结果 → 池清单(临时)。"""
import json
import re

for f in (r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_zz500.json",
          r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_zz1000.json",
          None):
    pass

files = [r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_zz500.json",
         r"E:\AgentProjects\AlphaPrism\data\wind_req\resp_zz1000.json"]
out = {}
for f in files:
    raw = open(f, encoding="utf-8-sig").read()
    txt = json.loads(json.loads(raw)["content"][0]["text"])
    tbl = txt["data"]["data"][0]
    cols = [c["name"] for c in tbl["columns"]]
    rows = tbl["rows"]
    print(f.split("\\")[-1], "rows:", len(rows))
    for r in rows:
        code, name = r[0], r[1]
        out.setdefault(code, name)
        print(f"  {code} {name}")
    print("---")

with open(r"E:\AgentProjects\AlphaPrism\data\wind_req\pool_zz500_zz1000.json", "w", encoding="utf-8") as w:
    json.dump(out, w, ensure_ascii=False, indent=1)
print("saved", len(out))