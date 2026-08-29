# -*- coding: utf-8 -*-
"""批量生成 真实数据快照样例 → tests/fixtures/snapshots/。

拉池内 7 只 ETF 当前行情,跑 internal 取数 + _facts_markdown,把
「数据快照 + 盘面状态」markdown 存成 fixture(结构回归用,不断言数值)。

跑法:python scripts/_gen_fixtures.py
"""
import os
import sys
sys.path.insert(0, "E:/AgentProjects/AlphaPrism")
from datetime import datetime

from alphaprism.planner import intraday_engine as eng
from alphaprism.planner.intraday import _facts_markdown, _load_facts

POOL = ["516020", "159796", "159326", "515790", "515050", "159516", "518850"]

OUT_DIR = os.path.join("E:/AgentProjects/AlphaPrism", "tests", "fixtures", "snapshots")
os.makedirs(OUT_DIR, exist_ok=True)

now = datetime.now()
print(f"now = {now:%Y-%m-%d %H:%M} [{now.strftime('%A')}]")

from alphaprism.config import Config
cfg = Config()

for code in POOL:
    try:
        facts = _load_facts(code, cfg, now)
        md = _facts_markdown(facts)
        # 追加盘面状态段(render_state_md 需要 state/anchors/catalyst/holding)
        etf = facts["etf"]
        state = eng.signal_state(facts)
        anchors = eng.param_anchors(facts)
        try:
            catalyst = eng.catalyst_context(facts, state)
            holding = eng.holding_context()
        except Exception as exc:  # noqa: BLE001
            catalyst, holding = None, None
        md += eng.render_state_md(facts, state, anchors, catalyst, holding)
        # 清理:去掉 Windows 控制台编码噪音无影响;直接写文件
        path = os.path.join(OUT_DIR, f"{code}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        # 只打印关键摘要,避免刷屏
        price = (etf.get("minute") or {}).get("price")
        vr = etf.get("vol_ratio")
        rp = etf.get("range_pos")
        sw = (state or {}).get("state_word")
        sc = (state or {}).get("scenario")
        anchor = anchors.get("anchor_price")
        stop = anchors.get("stop_loss")
        print(f"[{code}] price={price} vr={vr} range_pos={rp} "
              f"state={sc}/{sw} anchor={anchor} stop={stop} rows={len(md.splitlines())} -> {path}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[{code}] FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)

print("done")