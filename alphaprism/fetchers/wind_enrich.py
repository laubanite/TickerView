"""Wind 成交额/换手率补充抓取(经 wind-mcp-skill CLI)。

背景:腾讯日线不含成交额/换手率。Wind fund_data.get_fund_kline 提供每日
TURNOVER(成交额,元)/ CHANGEHANDRATE(换手率,%)/ VOLUME(成交量,股),作为
管线补充源(经 agent 侧 skill,subprocess 调用)。已验证与腾讯价格一致。

代价:免费额度 1000 积分/天。默认只拉近期窗口(wind_amount_days)控制消耗;
全区间历史回补留待阶段3(量价规则进引擎)按需做。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

WIND_SKILL_DIR = (
    Path(os.environ.get("USERPROFILE", Path.home())) / ".agents" / "skills" / "wind-mcp-skill"
)
_WIND_CLI = WIND_SKILL_DIR / "scripts" / "cli.mjs"


def to_windcode(symbol: str) -> str:
    """交易代码 → Wind 代码:5 开头(沪)加 .SH,1 开头(深)加 .SZ。"""
    suffix = ".SH" if symbol.startswith("5") else ".SZ"
    return f"{symbol}{suffix}"


def _call_wind(server: str, tool: str, params: dict) -> dict:
    cmd = ["node", "scripts/cli.mjs", "call", server, tool, json.dumps(params, ensure_ascii=False)]
    proc = subprocess.run(
        cmd, cwd=str(WIND_SKILL_DIR), capture_output=True, text=True,
        encoding="utf-8", timeout=120,  # 显式 UTF-8:text=True 默认走 GBK 会破坏 JSON
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Wind CLI 非零退出: {proc.stdout[:400]} {proc.stderr[:200]}")
    outer = json.loads(proc.stdout)
    if outer.get("isError"):
        raise RuntimeError(f"Wind 返回错误: {outer.get('error', proc.stdout[:400])}")
    return json.loads(outer["content"][0]["text"])


def fetch_daily_amount_turnover(symbol: str, begin_date: str, end_date: str) -> pd.DataFrame:
    """取 Wind 日线成交额/换手率,返回 trade_date/amount/turnover。

    begin_date / end_date 格式 yyyyMMdd。skill 缺失时返回空表(调用方降级)。
    """
    if not _WIND_CLI.exists():
        logger.warning("wind-mcp-skill 未安装(%s),成交额/换手率跳过", WIND_SKILL_DIR)
        return pd.DataFrame()
    payload = _call_wind(
        "fund_data",
        "get_fund_kline",
        {"windcode": to_windcode(symbol), "begin_date": begin_date, "end_date": end_date},
    )
    data = payload.get("data") or {}
    cols = [c["name"] for c in data.get("columns", [])]
    rows = data.get("rows", [])
    if not rows or "TIME" not in cols:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=cols)
    out = pd.DataFrame({"trade_date": df["TIME"].str[:10]})
    if "TURNOVER" in df.columns:
        out["amount"] = pd.to_numeric(df["TURNOVER"], errors="coerce")
    else:
        out["amount"] = None
    if "CHANGEHANDRATE" in df.columns:
        out["turnover"] = pd.to_numeric(df["CHANGEHANDRATE"], errors="coerce")
    else:
        out["turnover"] = None
    return out
