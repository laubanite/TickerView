"""ETF 份额 / 规模抓取 —— 暂缓(2026-08-11)。

背景:东方财富 api.fund.eastmoney.com 可直连,但 akshare 1.17.62 的
fund_etf_fund_info_em 有列数解析 bug(14 vs 13);且该接口返回历史净值而非份额。
份额 / 规模需走基金概况页 / 定期报告数据,属 §2.1 月度景气度流程,阶段1 不需要。
待阶段2 结束后按需实现,并确认"最新份额"字段的可靠来源。
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

SCALE_COLUMNS = ["symbol", "trade_date", "shares", "scale"]


def fetch_scale(ak=None, symbol: str = "") -> pd.DataFrame:
    """暂缓实现:返回空表并提示。"""
    logger.warning("份额 / 规模抓取暂缓实现(阶段1 不启用),请见模块文档。")
    return pd.DataFrame(columns=SCALE_COLUMNS)
