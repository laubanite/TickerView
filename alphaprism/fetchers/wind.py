"""Wind AIFinMarket 数据接入 —— 经 Wind MCP Skill(agent 通道),非管线 REST。

结论(2026-08-11 实测):Wind AIFinMarket 官方接入方式是 MCP(Skills),面向 AI Agent,
不是给 Python 定时脚本的 REST API。已全局安装 wind-mcp-skill / wind-find-finance-skill
(%USERPROFILE%\\.agents\\skills\\),key 已配置在 %USERPROFILE%\\.wind-aifinmarket\\config,
取数验证通过(600519.SH 最新价 1346.50 元)。

使用方式:需要行业信息 / 催化剂 / 宏观数据时,由 LLM 层(agent)按
%USERPROFILE%\\.agents\\skills\\wind-mcp-skill\\SKILL.md 的规则调用 CLI 取数,写入
盘前简报 / 月度定池子。定时管线(scripts/run_daily.py)不调用 Wind,本模块仅作说明。

积分:免费额度 1000 积分/天,低频使用(盘前简报 + 定池子)足够。
"""
from __future__ import annotations

# 本模块为文档占位:Wind 数据经 agent 层 skill 访问,管线侧不提供 Python 客户端。
# 取数入口(见 wind-mcp-skill/SKILL.md):
#   node scripts/cli.mjs call <server_type> <tool_name> '<params_json>'
# 示例:股票最新价 → stock_data / get_stock_price_indicators / {"windcode":"600519.SH","indexes":"最新成交价"}
