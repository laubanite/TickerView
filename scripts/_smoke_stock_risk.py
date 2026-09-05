# -*- coding: utf-8 -*-
"""个股模式冒烟(只读+归档一条):真实 qt + 日线缓存 + 状态机 + signal_log。"""
import json
import sys

sys.path.insert(0, r"E:\AgentProjects\AlphaPrism")
from alphaprism.config import Config
from alphaprism.planner.stock_risk import (archive_stock_risk,
                                           build_stock_risk_advice)

cfg = Config()
print("flag stock_mode =", cfg.get("intraday", "stock_mode", default=None))
for code in ("300308", "600519"):  # 深/沪 各一只(沪股验证 sh 前缀)
    adv = build_stock_risk_advice(code, cfg)
    print("=" * 60)
    print(f"[{code}] 级别={adv['risk']['level']} 面板词={adv['panel']['status_word']}"
          f" / 风险={adv['panel']['risk']} 类别={adv['category']}")
    print(adv["one_sentence"])
    if adv["one_sentence_event"]:
        print(adv["one_sentence_event"])
    print("规则:", [(r["id"], r["level"]) for r in adv["risk"]["rules"]])
    print("指标: m20=%s vol_ratio=%s low250=%s atr%%=%s 成本=%s"
          % (adv["facts"].get("m20"), adv["facts"].get("vol_ratio"),
             adv["facts"].get("low250_close"), adv["facts"].get("atr20_pct"),
             adv["facts"].get("cost")))
    archive_stock_risk(code, adv)
print("=" * 60)
print("归档完成(每日一条,signal_log kind=stock_risk)")

# app 可导入性(路由分支语法/导入检查)
from alphaprism.web.app import create_app  # noqa: E402
app = create_app()
client = app.test_client()
for path in ("/api/snapshot/tech/facts?code=600519",
             "/api/snapshot/tech/analysis?code=600519",
             "/api/quote?symbol=600519"):
    r = client.get(path)
    d = r.get_json()
    print(f"GET {path} -> {r.status_code} ok={d.get('ok')}")
    if path.startswith("/api/snapshot/tech/facts"):
        print("  signal:", json.dumps(d.get("signal", {}), ensure_ascii=False)[:200])
        print("  md 头两行:", (d.get("markdown") or "")[:80].splitlines()[:2])
    if path.startswith("/api/quote"):
        print("  snapshot keys:", sorted((d.get("snapshot") or {}).keys()))
