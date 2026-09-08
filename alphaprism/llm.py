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
         timeout: int = 150) -> str | None:
    """按 profiles 顺序尝试,返回首个成功的文本;全部失败返回 None。

    限流智能切换(2026-08-29):429/5xx = provider 级限流,同 provider 换模型无效,
    立即跳过该 provider 的所有 profile(避免智谱 429 后还撞 4.7-flash 又 429)。
    """
    cfg = cfg or Config()
    tried_providers: set[str] = set()
    for prof in _profiles(cfg):
        provider = str(prof.get("provider", "")).strip()
        model = str(prof.get("model", "")).strip()
        key = str(cfg.get("llm", f"{provider}_key", default="") or "").strip()
        url = _chat_url(str(prof.get("base_url") or ""), provider)
        if not url or not model or not key:
            continue
        if provider in tried_providers:
            continue     # 该 provider 已限流/失败,整组跳过
        try:
            text = _call(url, key, model, messages, temperature, max_tokens, timeout)
            if text:
                return text
            logger.warning("LLM[%s/%s] 返回空,切换下一个", provider, model)
        except requests.exceptions.HTTPError as exc:
            # 429/5xx = provider 级限流:同 provider 其他模型大概率同样失败
            code = exc.response.status_code if exc.response is not None else 0
            if code in (429, 500, 502, 503):
                tried_providers.add(provider)
                logger.warning("LLM[%s/%s] 限流(HTTP %d),整组跳过 provider",
                               provider, model, code)
            else:
                logger.warning("LLM[%s/%s] 失败(%s),切换下一个",
                               provider, model, str(exc)[:80])
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM[%s/%s] 失败(%s),切换下一个",
                           provider, model, str(exc)[:80])
    logger.info("LLM 全部可用模型失败,返回 None(调用方降级规则)")
    return None


def _chat_url(base_url: str, provider: str) -> str:
    """规范化聊天端点:接受完整端点或常规 Base URL(自动补 /chat/completions)。

    行业惯例(OpenAI SDK 等):Base URL 指向 API 根(如 https://api.deepseek.com/v1),
    客户端负责拼接 /chat/completions。内置服务商的 _ENDPOINTS 已是完整端点,幂等通过。
    """
    url = str(base_url or "").strip() or _ENDPOINTS.get(provider)
    if not url:
        return ""
    url = url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def test_connection(provider: str, model: str,
                    base_url: str = "", key: str = "",
                    timeout: int = 20) -> tuple[bool, str]:
    """测试单个 LLM 连接(Web 设置面板「测试连接」)。

    用最小请求(1 token)验证 Base URL + API Key 是否有效;
    返回 (ok, message),不写任何配置、不落日志明文 key。
    """
    url = _chat_url(base_url, provider)
    if not url:
        return False, f"未知服务商「{provider}」且未填 Base URL"
    if not model:
        return False, "缺少模型名称"
    if not key:
        return False, "没有可用的 API Key(请在表单中输入或先保存)"
    try:
        apply_network_policy(True)
        resp = requests.post(
            url,
            json={"model": model,
                  "messages": [{"role": "user", "content": "ping"}],
                  "max_tokens": 1},
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            detail = ""
            try:
                js = resp.json()
                err = js.get("error") if isinstance(js, dict) else js
                detail = str(err)[:160]
            except Exception:  # noqa: BLE001
                detail = resp.text[:160]
            return False, f"HTTP {resp.status_code}: {detail or '请求被拒绝'}"
        # HTTP 200 但 content 为空:reasoning 模型(如 glm-4.6/qwq)只输出思考,max_tokens=1 时
        # content 必为 null——连接是通的,但「测试成功」会误导用户以为可用,实际一用就截断。
        try:
            msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
            if not msg.get("content") and (msg.get("reasoning_content") or msg.get("reasoning")):
                return True, "连接成功(注意:该模型是思考型,会先花 token 思考再作答,任务类调用建议调大 token 上限)"
        except Exception:  # noqa: BLE001
            pass
        return True, f"连接成功({model})"
    except Exception as exc:  # noqa: BLE001
        return False, f"请求失败: {str(exc)[:160]}"


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
    choice = (js.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content")
    if content:
        return str(content)
    # reasoning 模型(如 z-ai/glm-5.3-free)可能把 token 预算全花在思考上,content 为 null:
    # finish=length = 思考未完成即截断,输出必然残缺 → 返回空让上层换下一个模型;
    # 不返回 reasoning_content(那是思考过程,不是答案)。
    finish = str(choice.get("finish_reason") or "")
    if msg.get("reasoning_content") or msg.get("reasoning"):
        logger.warning("LLM[%s] reasoning 模型 content 为空(finish=%s,max_tokens=%d)——"
                       "token 预算被思考耗尽,视为失败换下一个", model, finish, max_tokens)
    return ""
