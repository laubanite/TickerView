"""LLM 调用(盘前催化状态判定):多 provider 免费模型自动切换。

settings.yaml `llm.profiles` 为尝试顺序(配额用尽/失败自动换下一个):
  - {provider, model}   provider: openrouter / siliconflow
key 在 settings.local.yaml `llm.<provider>_key`。全部失败返回 None(调用方降级规则判定)。
"""
from __future__ import annotations

import logging

import requests

from .config import Config
from .net import apply_network_policy

logger = logging.getLogger(__name__)

_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "siliconflow": "https://api.siliconflow.cn/v1/chat/completions",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
}


def _profiles(cfg: Config) -> list[dict]:
    return list(cfg.get("llm", "profiles", default=[]) or [])


def chat(messages: list[dict], cfg: Config | None = None,
         temperature: float = 0.2, max_tokens: int = 1200,
         timeout: int = 90) -> str | None:
    """按 profiles 顺序尝试,返回首个成功的文本;全部失败返回 None。"""
    cfg = cfg or Config()
    for prof in _profiles(cfg):
        provider = str(prof.get("provider", "")).strip()
        model = str(prof.get("model", "")).strip()
        key = str(cfg.get("llm", f"{provider}_key", default="") or "").strip()
        url = _ENDPOINTS.get(provider)
        if not url or not model or not key:
            continue
        try:
            text = _call(url, key, model, messages, temperature, max_tokens, timeout)
            if text:
                return text
            logger.warning("LLM[%s/%s] 返回空,切换下一个", provider, model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM[%s/%s] 失败(%s),切换下一个", provider, model, str(exc)[:80])
    logger.info("LLM 全部可用模型失败,返回 None(调用方降级规则)")
    return None


def _call(url: str, key: str, model: str, messages: list[dict],
          temperature: float, max_tokens: int, timeout: int) -> str:
    apply_network_policy(True)
    resp = requests.post(
        url,
        json={"model": model, "messages": messages,
              "temperature": temperature, "max_tokens": max_tokens},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    js = resp.json()
    return (js.get("choices") or [{}])[0].get("message", {}).get("content") or ""
