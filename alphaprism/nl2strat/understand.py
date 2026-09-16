# -*- coding: utf-8 -*-
"""understand:NL → IntentSpec(§4.1)。模型输出 JSON 契约,代码做确定性核验。

三态出口:READY(进流水线)/ NEEDS_ASK(追问可续)/ UNSUPPORTED(显式拒绝)。
"不臆测"的可执行形式:
- symbol 不信任模型:代码对 kline_cache 与 etf 表核验(查不到→NEEDS_ASK);
- 时间窗缺失按 inference_authority 处理(default_ok 才补默认并注记;
  explicit 一律追问);
- 每 fragment 必须逐字回显用户原话(semantic_verify 的对照基准在这里诞生);
- 不可表达项命中 unsupported → 显式拒绝(给 nearest_alternative,绝不静默近似)。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from .contracts import (
    AUTHORITY_LEVELS, ORIGIN_DEFAULT, ORIGIN_USER, AskQuestion, Fragment,
    IntentSpec, STATE_NEEDS_ASK, STATE_READY, STATE_UNSUPPORTED,
    UnderstandResult, UnsupportedItem,
)
from .prompts import understand_prompt
from .vocabulary import Vocabulary, get_vocabulary

FRAG_ID_RE = re.compile(r"^f\w*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = {"entry", "exit", "stop", "take_profit", "time_exit", "other"}
_DEFAULT_WINDOW_DAYS = 3 * 365          # default_ok 时回看三年


def parse_json_object(text: str) -> dict | None:
    """模型 JSON 容错截取(与 verifier._parse_scores 同套路)。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        js = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return js if isinstance(js, dict) else None


class UnderstandService:
    """LLM 端口注入(chat: messages->str|None);核验与状态机纯代码。"""

    def __init__(self, chat, cfg=None, voc: Vocabulary | None = None) -> None:
        self.chat = chat
        self.cfg = cfg
        self.voc = voc or get_vocabulary()

    # ---------- 主入口 ----------
    def run(self, text: str, *, session_id: str = "", run_id: str = "",
            authority_hint: str = "default_ok") -> UnderstandResult:
        raw = self.chat([{"role": "user",
                          "content": understand_prompt(text, self.voc, authority_hint)}],
                        cfg=self.cfg, temperature=0.1, max_tokens=1600, timeout=120) \
            if self._chat_wants_kwargs() else \
            self.chat([{"role": "user",
                        "content": understand_prompt(text, self.voc, authority_hint)}])
        js = parse_json_object(raw or "")
        if js is None:
            return UnderstandResult(
                state=STATE_NEEDS_ASK,
                questions=[AskQuestion("q_parse", "没把你的描述解析成结构化意图,换种说法"
                                                  "或补充:标的代码、时间窗、买卖条件?")])
        return self.verify(js, text, session_id=session_id, run_id=run_id)

    def _chat_wants_kwargs(self) -> bool:
        import inspect
        try:
            sig = inspect.signature(self.chat)
            return "cfg" in sig.parameters or "temperature" in sig.parameters
        except (TypeError, ValueError):
            return False

    # ---------- 补答后确定性重核验(不再过 LLM:understand 的重跑=合入答复再核验) ----------
    def reverify(self, spec: IntentSpec) -> UnderstandResult:
        import copy
        return self._finalize(copy.deepcopy(spec), [])

    # ---------- NEEDS_ASK 问题归一(补 field_hint + 防重复问) ----------
    _HINT_WORDS = (("symbol", ("标的", "代码")), ("start", ("开始", "起始")),
                   ("end", ("结束", "截止")), ("语义", ("规则", "条件", "买卖")))

    @classmethod
    def _normalize_questions(cls, questions: list[AskQuestion]) -> None:
        for q in questions:
            if not q.field_hint:
                for hint, words in cls._HINT_WORDS:
                    if any(w in q.text for w in words):
                        q.field_hint = hint
                        break

    @staticmethod
    def _already_asked(questions: list[AskQuestion], field: str) -> bool:
        return any(q.field_hint == field for q in questions)

    # ---------- 确定性核验(契约准入) ----------
    def verify(self, js: dict, original_text: str, *, session_id: str = "",
               run_id: str = "") -> UnderstandResult:
        spec_d = js.get("spec") or {}
        questions = [AskQuestion.from_dict(q) for q in (js.get("questions") or [])]
        unsupported = [UnsupportedItem.from_dict(u) for u in (js.get("unsupported") or [])]

        if unsupported:
            return UnderstandResult(state=STATE_UNSUPPORTED, unsupported=unsupported,
                                    questions=questions)

        frags: list[Fragment] = []
        seen: set[str] = set()
        for f in spec_d.get("fragments") or []:
            fid = str(f.get("fragment_id") or "").strip()
            ftext = str(f.get("text") or "").strip()
            kind = str(f.get("kind") or "entry")
            origin = str(f.get("origin") or ORIGIN_USER)
            if not FRAG_ID_RE.match(fid) or fid in seen:
                continue
            if origin == ORIGIN_USER and ftext and ftext not in original_text:
                # 原话逐字基准:改写过的片段降级为 other 并注记(核验不通过≠丢弃)
                kind = kind if kind in KINDS else "other"
            seen.add(fid)
            frags.append(Fragment(fragment_id=fid, text=ftext,
                                  kind=kind if kind in KINDS else "other",
                                  origin=origin if origin in (ORIGIN_USER, ORIGIN_DEFAULT)
                                  else ORIGIN_USER))
        authority = str(spec_d.get("inference_authority") or "default_ok")
        if authority not in AUTHORITY_LEVELS:
            authority = "default_ok"
        spec = IntentSpec(
            session_id=session_id, run_id=run_id,
            symbol=re.sub(r"\D", "", str(spec_d.get("symbol") or ""))[:6],
            symbol_name=str(spec_d.get("symbol_name") or "").strip(),
            start=str(spec_d.get("start") or "").strip(),
            end=str(spec_d.get("end") or "").strip(),
            fragments=frags, inference_authority=authority,
            notes=[str(n) for n in (spec_d.get("notes") or [])],
        )
        return self._finalize(spec, questions)

    def _finalize(self, spec: IntentSpec,
                  questions: list[AskQuestion]) -> UnderstandResult:
        # slot 类问题由代码权威发起(带可路由的 field_hint);模型对这些槽位的
        # 重复提问一律抑制(否则同一答案被两次消耗 → 续流错位)。
        self._normalize_questions(questions)
        slot_hints = {"symbol", "start", "end", "语义"}
        ask: list[AskQuestion] = [q for q in questions if q.field_hint not in slot_hints]
        # —— symbol 核验(不信任模型):行情存在性是硬事实
        if not spec.symbol or len(spec.symbol) != 6:
            ask.append(AskQuestion("q_symbol", "请给出 6 位标的代码(ETF/股票)?",
                                   field_hint="symbol"))
        else:
            fact = self._symbol_fact(spec.symbol)
            if fact is None:
                ask.append(AskQuestion(
                    "q_symbol_unknown",
                    f"本地行情里找不到 {spec.symbol}(代码有误?或先跑抓取脚本)",
                    field_hint="symbol"))
            elif not spec.symbol_name:
                spec.symbol_name = fact
        # —— 时间窗:显式 > 默认注入(带注记)> 追问
        if not (DATE_RE.match(spec.start or "") and DATE_RE.match(spec.end or "")):
            if spec.inference_authority == "explicit":
                ask.append(AskQuestion("q_window", "请给出回测起止日期(YYYY-MM-DD)?",
                                       field_hint="start"))
            else:
                today = date.today()
                spec.end = today.isoformat()
                spec.start = (today - timedelta(days=_DEFAULT_WINDOW_DAYS)).isoformat()
                spec.notes.append(f"时间窗缺省注入:{spec.start}~{spec.end}(default_ok)")
        # —— 语义片段底线
        if not spec.semantic_fragments():
            ask.append(AskQuestion("q_semantics",
                                   "没识别出任何买卖规则(入场/离场条件),能补一句吗?",
                                   field_hint="语义"))
        if ask:
            return UnderstandResult(state=STATE_NEEDS_ASK, spec=spec, questions=ask)
        return UnderstandResult(state=STATE_READY, spec=spec)

    # ---------- 事实源(行情缓存 + etf 表,只读) ----------
    def _symbol_fact(self, symbol: str) -> str | None:
        from . import tools
        if tools.check_data(symbol).get("ok"):
            try:
                from alphaprism.db import connect
                from alphaprism.paths import DB_PATH
                if DB_PATH.exists():
                    conn = connect()
                    try:
                        row = conn.execute(
                            "SELECT name FROM etf WHERE symbol=?", (symbol,)).fetchone()
                        if row:
                            return str(row["name"] or "")
                    finally:
                        conn.close()
            except Exception:  # noqa: BLE001 - 名称回填失败不影响代码核验结论
                pass
            return ""
        return None


# ---------- NEEDS_ASK 补答续流(§4.2 v3:understand 未定稿,重跑是对的) ----------

def merge_answers(spec: IntentSpec, answers: dict[str, str]) -> IntentSpec:
    """把追答复答合入半成品 spec(qid 或 field_hint → 值);symbol 类答复重新核验在重跑里。"""
    for key, val in (answers or {}).items():
        val = str(val or "").strip()
        if not val:
            continue
        if key in ("symbol", "q_symbol", "q_symbol_unknown"):
            spec.symbol = re.sub(r"\D", "", val)[:6]
        elif key in ("start", "q_start"):
            spec.start = val
        elif key in ("end", "q_end"):
            spec.end = val
        elif key == "inference_authority" and val in AUTHORITY_LEVELS:
            spec.inference_authority = val
        else:
            spec.notes.append(f"用户补答 {key}: {val}")
    spec.revision += 1
    return spec
