"""配置加载:settings.yaml(主配置)+ settings.local.yaml(本地私有,覆盖主配置)。

本地私有配置不入版本控制,用于存放 API key 等敏感项。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "settings.yaml"
LOCAL_CONFIG = CONFIG_DIR / "settings.local.yaml"
WATCHLIST_FILE = CONFIG_DIR / "watchlist.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并:override 的值覆盖 base;dict 值逐层合并。"""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    def __init__(self) -> None:
        self._data = self._load()

    @staticmethod
    def _load() -> dict:
        if not DEFAULT_CONFIG.exists():
            raise FileNotFoundError(f"主配置不存在: {DEFAULT_CONFIG}")
        with open(DEFAULT_CONFIG, encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        local: dict = {}
        if LOCAL_CONFIG.exists():
            with open(LOCAL_CONFIG, encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
        merged = _deep_merge(base, local)
        # 跟踪池独立文件(CLI: alphaprism pool 管理)
        if WATCHLIST_FILE.exists():
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                merged["watchlist"] = yaml.safe_load(f) or []
        return merged

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def watchlist(self) -> list[dict]:
        return list(self.get("watchlist", default=[]))

    @property
    def adjust(self) -> str:
        return str(self.get("adjust", default="qfq"))

    def source_enabled(self, name: str) -> bool:
        return bool(self.get("sources", name, default=False))

    def wind_api_key(self) -> str:
        """Wind key 优先读环境变量(便于计划任务),其次本地配置。"""
        key = os.environ.get("WIND_AIFM_API_KEY", "").strip()
        if not key:
            key = str(self.get("wind", "api_key", default="") or "").strip()
        return key
