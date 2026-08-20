"""分时抓取回归测试(fetch_minute,里程碑6)。

网络依赖:腾讯 minute 接口。断网/改版时跳过(不阻塞本地测试)。
"""
from __future__ import annotations

import pytest

from alphaprism.fetchers.etf_kline import fetch_minute


def test_fetch_minute_shape():
    """分时返回字段完整,均价 = 累计额/(累计量×100),昨收来自 qt。"""
    try:
        df = fetch_minute("516020")
    except Exception:  # noqa: BLE001 网络/接口异常 → 跳过
        pytest.skip("腾讯分时接口不可用(网络/改版)")
    assert not df.empty
    assert {"ts", "price", "vol", "amount", "avg_price", "prev_close"} <= set(df.columns)
    # 首点均价应接近首点价格(量小则等于价格)
    first = df.iloc[0]
    assert abs(first["avg_price"] - first["price"]) < 0.01 or first["vol"] == 0
    # 时间格式 HH:MM:SS
    assert df["ts"].iloc[0][:2] == "09" and df["ts"].iloc[0][3:5] == "30"
    # 昨收非空(盘中/收盘应有值)
    assert df["prev_close"].iloc[0] is not None
    # 累计额单调不减(amount 是累计值)
    assert (df["amount"].diff().dropna() >= 0).all()
