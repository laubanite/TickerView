# -*- coding: utf-8 -*-
"""A 档风险解读 + 前端双模式冒烟(2026-09-04)。"""
import json
import sys

sys.path.insert(0, r"E:\AgentProjects\AlphaPrism")
from alphaprism.web.app import create_app

app = create_app()
client = app.test_client()
CODE = "600519"

print("== 1. facts(个股模式标记) ==")
r = client.get(f"/api/snapshot/tech/facts?code={CODE}").get_json()
print("ok=", r.get("ok"), "mode=", r.get("mode"), "panel词=", (r.get("panel") or {}).get("status_word"))

print("== 2. analysis(A 档 LLM 风险解读) ==")
r = client.get(f"/api/snapshot/tech/analysis?code={CODE}").get_json()
print("ok=", r.get("ok"), "degraded=", r.get("degraded"), "mode=", r.get("mode"))
md = r.get("markdown") or ""
print(md[:600])
print("...")
low = md.lower()
bad = [w for w in ("加仓", "买入", "卖出", "减仓", "清仓", "目标价") if w in md]
print(">> 建议词检查:", "命中" if bad else "无", bad or "")

print("== 3. counterfactual(个股守卫) ==")
r = client.get(f"/api/snapshot/tech/counterfactual?code={CODE}")
d = r.get_json()
print("status=", r.status_code, "mode=", d.get("mode"), "error=", (d.get("error") or "")[:40])

print("== 4. ETF 模式回归(反事实端点不受影响) ==")
r = client.get("/api/snapshot/tech/counterfactual?code=512480")
print("ETF 反事实 status=", r.status_code, "(预期 404/502:未找到作战地图或无快照,但不是 400 个股守卫)")
r2 = client.get("/api/watchlist")
types = {i["symbol"]: i.get("type") for i in r2.get_json().get("items", [])}
print("watchlist types:", types)
