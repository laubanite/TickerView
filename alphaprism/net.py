"""网络策略:按配置强制直连(清除代理环境变量)。

本机曾配置 ALL_PROXY 指向本地代理端口(如 127.0.0.1:7890),代理未运行时
requests 走死代理导致 ProxyError。国内数据源(东方财富等)应直连。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def apply_network_policy(no_proxy: bool) -> None:
    """no_proxy=True 时清除代理环境变量,强制直连。"""
    if no_proxy:
        for key in _PROXY_ENV_KEYS:
            if key in os.environ:
                logger.info("清除代理环境变量 %s (强制直连)", key)
                os.environ.pop(key, None)
