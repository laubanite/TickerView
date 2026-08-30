"""探测当前 llm.profiles 各 provider 是否支持 logprobs(llm-as-a-verifier 打分底层用)。

用法(仓库根目录): python scripts/probe_logprobs.py
输出: 每个配置的 provider/model 是否返回 logprobs;支持 → verifier 打分可用
"logprob 期望"法;不支持 → 退化 JSON 数值打分/多次采样均值。
"""
from __future__ import annotations

import sys

import requests

sys.path.insert(0, ".")
from alphaprism.config import Config  # noqa: E402
from alphaprism.llm import _ENDPOINTS  # noqa: E402
from alphaprism.net import apply_network_policy  # noqa: E402


def _probe(provider: str, url: str, key: str, model: str) -> str:
    apply_network_policy(True)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只输出数字 5"}],
        "max_tokens": 8,
        "temperature": 0.0,
        "logprobs": True,
        "top_logprobs": 8,
    }
    try:
        r = requests.post(url, json=payload,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          timeout=40)
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:200]}"
        js = r.json()
        choice = (js.get("choices") or [{}])[0]
        lp = choice.get("logprobs")
        has = lp is not None and (lp.get("content") is not None
                                  or lp.get("token_logprobs") is not None)
        content = ((choice.get("message") or {}).get("content") or "")[:40]
        lp_keys = list(lp.keys()) if isinstance(lp, dict) else lp
        return f"logprobs={'YES' if has else 'NO'} | content='{content}' | lp_fields={lp_keys}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {str(exc)[:160]}"


def main() -> int:
    cfg = Config()
    profiles = list(cfg.get("llm", "profiles", default=[]) or [])
    if not profiles:
        print("未配置 llm.profiles(settings.yaml llm.profiles) — 无法探测")
        return 0
    for prof in profiles:
        provider = str(prof.get("provider", "")).strip()
        model = str(prof.get("model", "")).strip()
        key = str(cfg.get("llm", f"{provider}_key", default="") or "").strip()
        url = _ENDPOINTS.get(provider)
        if not url or not model or not key:
            print(f"[{provider}/{model}] 缺 url/key,跳过")
            continue
        print(f"[{provider}/{model}]\n  {_probe(provider, url, key, model)}")
    print("\n结论: logprobs=YES → verifier 用 logprob 期望打分;NO → 退化 JSON 数值打分(多次采样均值)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())