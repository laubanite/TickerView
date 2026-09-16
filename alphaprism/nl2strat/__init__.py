# -*- coding: utf-8 -*-
"""nl2strat:自然语言策略 → 可回测 YAML(见 docs/nl2strat-agent-plan.md v6)。

混合架构:外层固定编排(loop.py)+ 修复节点自主子 agent(repair.py)。
模块布局与契约冻结项以方案 §3/§8 为准;契约唯一出口 = contracts.py。
"""
