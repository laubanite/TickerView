# -*- coding: utf-8 -*-
"""synthesize:交付报告(LLM 1 次调用;LLM 不可用降级为确定性骨架,不吞交付物)。"""
from __future__ import annotations

from .contracts import IntentSpec
from .prompts import synthesize_prompt


def deterministic_report(spec: IntentSpec, draft_yaml: str, metrics: dict,
                         notes: list[str]) -> str:
    """LLM 失败时的保底交付(数字零虚构,结构同 SYNTH 口径)。"""
    frags = "\n".join(f"- {f.fragment_id}({f.origin}):{f.text}" for f in spec.fragments)
    m = metrics or {}
    return (f"# 策略交付:{spec.symbol} {spec.start}~{spec.end}\n"
            f"## 我理解到的规则\n{frags}\n"
            f"## 回测结论\n指标:{m}\n(含佣金滑点、样本内统计;不保证未来表现)\n"
            f"## 不确定点\n" + ("\n".join(notes) or "无") + "\n\n## YAML\n" + draft_yaml)


class SynthesizeService:
    def __init__(self, chat, cfg=None) -> None:
        self.chat = chat
        self.cfg = cfg

    def run(self, spec: IntentSpec, draft_yaml: str, metrics: dict,
            notes: list[str]) -> tuple[str, bool]:
        """返回 (报告, 是否 LLM 成文)。"""
        prompt = synthesize_prompt(spec, draft_yaml, metrics, notes)
        text = self._call(prompt)
        if text and text.strip():
            return text.strip(), True
        return deterministic_report(spec, draft_yaml, metrics, notes), False

    def _call(self, prompt: str) -> str | None:
        import inspect
        try:
            params = inspect.signature(self.chat).parameters
        except (TypeError, ValueError):
            params = {}
        if "cfg" in params:
            return self.chat([{"role": "user", "content": prompt}], cfg=self.cfg,
                             temperature=0.3, max_tokens=1800, timeout=150)
        return self.chat([{"role": "user", "content": prompt}])
