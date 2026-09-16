# -*- coding: utf-8 -*-
"""外层固定编排状态机(§4.2):understand→plan→draft→[draft_selfcheck]→
reconcile→validate→run→judge→repair*→semantic_verify→synthesize。

纪律:
- 外层固定、可单测、成本可预算;唯一的自主性在 repair.RepairAgent;
- 确定性步骤(reconcile/validate/run/judge)零 LLM、可重跑;
- 三出口:READY / NEEDS_ASK(会话可续,resume_from 落 run 状态)/ UNSUPPORTED;
- 中断续流落点表(v3 转正):每个挂起点恢复后**已过段一律不重跑**:
    understand 补答 → 重跑 understand(未定稿,重跑是对的)
    JUDGE 可疑·确认 → 直达 semantic_verify(不重跑 run/judge,不豁免忠实度检查)
    JUDGE 可疑·调整(参数级)→ IntentSpec patch → 重进 draft(下游全部)
    JUDGE 可疑·调整(新增语义)→ 增量 plan → draft → reconcile(仅新 fragment)
    修复段 ask_user / request_scope 答复 → 回子循环成为新观察
    semantic_verify ambiguous → 澄清答复 → 仅涉事 fragment 重过语义关
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .contracts import (
    ExitStatus, Fragment, IntentSpec, PlanTask, ResumePoint, SemanticEvidence,
    SemanticVerdict, SemanticVerifyResult, AskPayload, AskQuestion, BudgetSpec,
    Draft, ErrCode, StratError, TraceEvent, TraceKind, UserVerdict,
    budget_for, admissible_verdicts, json_dumps, sha1_of,
)
from .prompts import (
    REPAIR_SYSTEM, draft_prompt, plan_prompt, semantic_verify_prompt,
)
from .repair import (
    R_STATUS_ASK_USER, R_STATUS_EXHAUSTED, R_STATUS_GAVE_UP, R_STATUS_PASSED,
    R_STATUS_SCOPE_PENDING, RepairAgent, RepairBudget, TOOL_PROMPT,
)
from .understand import parse_json_object
from .vocabulary import Vocabulary, get_vocabulary
from . import tools as gates


@dataclass
class RepairHistory:
    """跨多次修复调用的连续性:SEMANTIC_DRIFT 同 fragment 二次失败→ASK_USER。"""
    semantic_fail: dict[str, int] = field(default_factory=dict)
    rounds_used: int = 0


@dataclass
class Outcome:
    exit_status: str
    spec: IntentSpec | None = None
    draft: Draft | None = None
    yaml_text: str = ""
    metrics: dict = field(default_factory=dict)
    report: str = ""
    llm_report: bool = False
    pending: AskPayload | None = None          # NEEDS_ASK 时非空(答复面)
    unsupported: list = field(default_factory=list)
    errors: list[StratError] = field(default_factory=list)
    reason: str = ""
    llm_calls: int = 0
    repair_rounds: int = 0
    repair_steps: int = 0
    scope_denied: int = 0
    semantic_fail_count: int = 0
    cost_versions: dict = field(default_factory=dict)


class ChatPort:
    """chat 注入适配器(测试 fake 与生产 llm.chat 同签名;调用计数进 trace)。"""

    def __init__(self, chat: Callable[..., str | None], cfg: Any = None,
                 on_event: Callable[[TraceEvent], None] | None = None) -> None:
        self.chat = chat
        self.cfg = cfg
        self.on_event = on_event
        self.calls = 0
        self.versions: dict[str, int] = {}

    def ask(self, prompt: str, *, stage: str, max_tokens: int = 1400,
            temperature: float = 0.1) -> str | None:
        import inspect
        self.calls += 1
        self.versions[stage] = self.versions.get(stage, 0) + 1
        if self.on_event:
            self.on_event(TraceEvent(TraceKind.LLM_CALL, {
                "stage": stage, "prompt_hash": sha1_of(prompt),
                "prompt_chars": len(prompt)}))
        try:
            params = inspect.signature(self.chat).parameters
        except (TypeError, ValueError):
            params = {}
        if "cfg" in params:
            return self.chat([{"role": "user", "content": prompt}], cfg=self.cfg,
                             temperature=temperature, max_tokens=max_tokens, timeout=150)
        return self.chat([{"role": "user", "content": prompt}])


class SemanticVerifier:
    """semantic_verify 关卡(§4.2 v4):rubric 固定单调用;准入确定性把关。
    draft_selfcheck(产源自查)与修复段 selfcheck 工具共用本实现(同源同契约)。

    两道代码侧防线压低小模型误报:
    1. 映射式呈现——片段↔兑现条件由 draft.semantic_view 配对好,模型只裁忠实度;
    2. 伪漂移确定性筛——object/param 漂移所指符号恰是用户原话的标准译法、
       或 evidence 路径在草稿树里根本不存在 → 判为误报直接丢弃(不喂修复)。"""

    def __init__(self, chat_port: ChatPort, voc: Vocabulary | None = None) -> None:
        self.chat_port = chat_port
        self.voc = voc or get_vocabulary()

    def run(self, spec: IntentSpec, draft: Draft,
            only_fragments: list[str] | None = None,
            stage: str = "semantic_verify") -> SemanticVerifyResult:
        pairs = draft.semantic_view(only_fragments)
        prompt = semantic_verify_prompt(spec, pairs, self.voc)
        raw = self.chat_port.ask(prompt, stage=stage, max_tokens=1400)
        if raw is None:
            return SemanticVerifyResult(model_note="LLM 不可达:语义关卡跳过(注记入 trace)")
        js = parse_json_object(raw)
        verdicts = [SemanticVerdict.from_dict(v) for v in ((js or {}).get("verdicts") or [])
                    if isinstance(v, dict)]
        kept = admissible_verdicts(verdicts)
        survivors, screened = self._screen(spec, draft, kept)
        return SemanticVerifyResult(verdicts=survivors,
                                    dropped_invalid=len(verdicts) - len(kept),
                                    screened=screened)

    def _screen(self, spec: IntentSpec, draft: Draft,
                verdicts: list[SemanticVerdict]) -> tuple[list, list]:
        text_of = {f.fragment_id: f.text for f in spec.semantic_fragments()}
        survivors: list[SemanticVerdict] = []
        screened: list[dict] = []
        for v in verdicts:
            if v.verdict != "drift":
                survivors.append(v)
                continue
            user_text = text_of.get(v.fragment_id, "")
            # (a) evidence 路径必须真实存在(幻觉路径=无效裁决)
            if not _path_exists(draft.tree, v.evidence.yaml_path):
                screened.append({"fragment_id": v.fragment_id, "dimension": v.dimension,
                                 "reason": f"证据路径不存在:{v.evidence.yaml_path}",
                                 "verdict": "drift"})
                continue
            # (b) 被指"错译"的对象恰是用户原话的标准 DSL 符号 → 伪漂移
            hit = _referenced_symbol(v, self.voc, user_text, draft)
            if hit:
                screened.append({"fragment_id": v.fragment_id, "dimension": v.dimension,
                                 "reason": f"{hit} 是用户原话的标准符号译法(非对象错换)",
                                 "verdict": "drift"})
                continue
            survivors.append(v)
        return survivors, screened


def _path_exists(tree, path: str) -> bool:
    from .contracts import resolve_path
    try:
        resolve_path(tree, path)
        return True
    except (KeyError, IndexError, ValueError):
        return False


def _referenced_symbol(v: SemanticVerdict, voc: Vocabulary, user_text: str,
                       draft: Draft) -> str:
    """若漂移所指 actual/证据节点值恰为 user_text 的标准符号 → 返回该符号,否则空。"""
    from .contracts import resolve_path
    cands: set[str] = set()
    # actual 里出现的内建符号 token
    for tok in re.findall(r"[a-z][a-z0-9_]{2,}", v.actual or ""):
        if tok in voc.series:
            cands.add(tok)
    # evidence.yaml_path 解析到的实际值(若是 ref 字段)
    try:
        val = resolve_path(draft.tree, v.evidence.yaml_path)
        if isinstance(val, str) and val in voc.series:
            cands.add(val)
    except (KeyError, IndexError, ValueError):
        pass
    for sym in cands:
        if voc.symbol_correct_here(sym, user_text):
            return sym
    return ""


class Pipeline:
    """固定编排。P0 免费档单候选;付费双轨(P1)以 paid=True 生效。"""

    def __init__(self, spec: IntentSpec, *, chat: Callable[..., str | None], cfg: Any = None,
                 tier: str = "free", on_event: Callable[[TraceEvent], None] | None = None,
                 start_stage: str = "") -> None:
        self.spec = spec
        self.chat_port = ChatPort(chat, cfg, on_event)
        self.tier = tier
        self.budget: BudgetSpec = budget_for(tier)
        self.voc = get_vocabulary()
        self.on_event = on_event or (lambda ev: None)
        self.draft: Draft | None = None
        self.plan_tasks: list[PlanTask] = []
        self.result: dict | None = None
        self.history = RepairHistory()
        self.notes: list[str] = []
        self.verifier = SemanticVerifier(self.chat_port, self.voc)
        self.last_semantic: SemanticVerifyResult | None = None
        self._semantic_cache: tuple[str, SemanticVerifyResult] | None = None
        self._repair_steps_total = 0
        self._scope_denied_total = 0
        self._accepted_frags: set[str] = set()
        self._pending_agent: RepairAgent | None = None
        self._start_stage = start_stage

    def accept_semantic(self, fragment_id: str, note: str = "") -> Outcome:
        """用户对 ambiguous/二败片段裁决"现译可接受"→ 仅豁免该片段,其余照旧。"""
        self._accepted_frags.add(fragment_id)
        self.notes.append(f"用户裁决:片段 {fragment_id} 接受现译(标注回流)")
        self._emit(TraceEvent(TraceKind.RESUME,
                              {"point": ResumePoint.SEMANTIC_VERIFY,
                               "accepted": fragment_id, "answer": note[:120]}))
        out = self._semantic_stage()
        return out or self._finish()

    # ------------------------------------------------------------- 主流程

    def run(self) -> Outcome:
        s = self.spec
        # 1. plan
        errs = self._plan()
        if errs:
            return self._outcome_from_errors(errs)
        # 2. draft
        outcome = self._draft_stage()
        if outcome:
            return outcome
        # 2b. draft_selfcheck(付费档必跑;免费档默认关——§4.2 v5 G1)
        if self.tier == "paid":
            self._draft_selfcheck()
        # 3~6. 对账→校验→回测→判读
        outcome = self._gates_stage()
        if outcome:
            return outcome
        # 7. semantic_verify(硬关卡)
        outcome = self._semantic_stage()
        if outcome:
            return outcome
        return self._finish()

    # ------------------------------------------------------------- 各段

    def _plan(self) -> list[StratError]:
        prompt = plan_prompt(self.spec, self.voc)
        raw = self.chat_port.ask(prompt, stage="plan", max_tokens=1200)
        js = parse_json_object(raw or "")
        tasks = [PlanTask.from_dict(t) for t in ((js or {}).get("tasks") or [])
                 if isinstance(t, dict)]
        self.plan_tasks = [t for t in tasks if t.fragment_id]
        self._emit(TraceKind.STATE_TRANSITION, to="plan_done", tasks=len(self.plan_tasks))
        return []   # plan 缺项由 reconcile 兜底(集合对账),这里不拦

    def _draft_stage(self) -> Outcome | None:
        prompt = draft_prompt(self.spec, self.plan_tasks, self.voc)
        if self.tier == "paid":
            return self._draft_paid(prompt)
        # 免费档:单候选(预算表口径,§4.2);装配失败重试 1 次(耗全局 LLM 计数)
        raw = self.chat_port.ask(prompt, stage="draft", max_tokens=2000)
        d = self._assemble(raw)
        if d is None:
            self.notes.append("draft 首装失败,重试 1 次")
            raw = self.chat_port.ask(
                prompt + "\n\n(上一版输出无法解析为契约 JSON;只输出 JSON,别加解释)",
                stage="draft_retry", max_tokens=2000)
            d = self._assemble(raw)
        if d is None:
            return self._fail("draft JSON 装配失败(重试后仍失败)")
        self.draft = d
        self._emit(TraceKind.STATE_TRANSITION, to="draft_done",
                   yaml_hash=self.draft.yaml_hash())
        return None

    def _draft_paid(self, prompt: str) -> Outcome | None:
        raise NotImplementedError("付费双轨 P1 实装")   # §6 P1

    def _assemble(self, raw: str | None) -> Draft | None:
        js = parse_json_object(raw or "")
        if not js or not isinstance(js.get("entries"), list):
            return None
        try:
            return Draft.from_entries(str(js.get("name") or "nl2strat"),
                                      js["entries"],
                                      series=js.get("series") or {},
                                      indicators=js.get("indicators") or [])
        except (ValueError, TypeError) as exc:
            self.notes.append(f"draft 装配拒绝:{exc}")
            return None

    def _blind_pick(self, candidates: list[Draft]) -> Draft:
        """付费双候选盲评择优(verifier 独立调用;samples=2 口径同 morning)。"""
        # P1 实装;P0 不可达(免费档单候选)。留结构不留半成品逻辑。
        return candidates[0]

    def _gates_stage(self) -> Outcome | None:
        """reconcile→validate→run→judge(全确定性);不过则进修复子循环。

        判读分级(v2 #6):硬伤→修复;可疑/ask_user 路由 → 用户裁决挂起,不烧修复预算。
        """
        assert self.draft is not None
        errors = self._all_gates()
        guard = 0
        while errors and guard < 10:
            guard += 1
            hard, suspect = split_errors(errors)
            ask_route = [e for e in errors if e.route == "ask_user"]
            if not hard and (suspect or ask_route):
                return self.judge_suspect_ask(suspect or ask_route)
            outcome = self._repair(hard or errors, guard - 1)
            if outcome is not None:
                return outcome
            errors = self._all_gates()
        if errors:
            hard, suspect = split_errors(errors)
            if not hard:
                return self.judge_suspect_ask(suspect or errors)
            return self._exhausted(errors)
        return None

    def _all_gates(self) -> list[StratError]:
        assert self.draft is not None
        errs = gates.reconcile(self.spec, self.draft)
        errs += gates.validate_yaml(self.draft, self.spec, self.voc)
        if errs:
            return errs
        result, errs2 = gates.run_backtest_gate(self.draft, self.spec)
        if result is not None:
            self.result = result          # 可疑类不毁结果:确认续跑直达语义关不重跑
        if errs2:
            return errs2
        return []

    # ------------------------------------------------------------- 修复段

    def _repair(self, errors: list[StratError], round_no: int) -> Outcome | None:
        """修复预算全局记账(步/轮跨多次修复调用连续;§4.2 预算表口径)。"""
        steps_left = max(self.budget.repair_steps - self._repair_steps_total, 0)
        rounds_left = max(self.budget.repair_rounds - self.history.rounds_used, 0)
        if steps_left <= 0 or rounds_left <= 0:
            return self._exhausted(errors, reason="修复预算耗尽(全局记账)")
        agent = RepairAgent(
            draft=self.draft, spec=self.spec, errors=errors,
            budget=RepairBudget(steps_left=steps_left, rounds_left=rounds_left),
            next_action=self._repair_action_fn(),
            gates=self._all_gates,
            paid=(self.tier == "paid"),
            selfcheck_fn=self._selfcheck_fn,
            judge_fn=self._judge_fn,
            on_event=self._emit)
        out = agent.run()
        self._repair_steps_total += agent.step_no
        self.history.rounds_used += rounds_left - agent.budget.rounds_left
        self._scope_denied_total += sum(agent.denied.values())
        return self._settle_repair(agent, out)

    def _repair_action_fn(self) -> Callable[[str], dict]:
        base = REPAIR_SYSTEM.format(tool_prompt=TOOL_PROMPT) + \
            "\n\n符号对照表(中文说法↔DSL 标准符号):\n" + self.voc.cn_glossary()

        def act(ctx: str) -> dict:
            raw = self.chat_port.ask(base + "\n\n" + ctx,
                                     stage="repair", max_tokens=900)
            from .repair import parse_action_json
            return parse_action_json(raw or "") or {
                "tool": "give_up", "args": {"reason": "修复动作 JSON 解析失败"}}
        return act

    def _selfcheck_fn(self, fragment_ids: list[str]):
        res = self.verifier.run(self.spec, self.draft,
                                only_fragments=fragment_ids or None,
                                stage="selfcheck")
        return [StratError(code=ErrCode.SEMANTIC_DRIFT, where=f"fragment:{v.fragment_id}",
                           msg=f"自查: {v.dimension} {v.expected}≠{v.actual}",
                           related_paths=[v.evidence.yaml_path])
                for v in res.drifts()]

    def _judge_fn(self, fragment_id: str, candidates: list[str]) -> dict:
        """修复段候选盲评(advisory;§4.2 v5 G3)——P1 付费档实装,P0 结构占位。"""
        return {"ranking": candidates, "reasons": ["P0 未启用盲评排序"]}

    def _settle_repair(self, agent: RepairAgent, out) -> Outcome | None:
        if out.status == R_STATUS_PASSED:
            return None
        if out.status == R_STATUS_ASK_USER:
            return self._suspend(ResumePoint.REPAIR_LOOP, out.question or AskQuestion(
                "repair", "修复需要你裁决"), verdict_kind=UserVerdict.ANSWER,
                agent=agent)
        if out.status == R_STATUS_SCOPE_PENDING:
            q = out.question or AskQuestion("scope", f"是否扩权到 {out.scope_request}")
            return self._suspend(ResumePoint.REPAIR_LOOP, q,
                                 verdict_kind=UserVerdict.ANSWER, agent=agent,
                                 scope_request=out.scope_request)
        # gave_up / exhausted → v6 meta-judge 点位(P1 实装裁决;P0 直接终局)
        if out.meta_eligible:
            self.notes.append("meta-judge(P1)点位:被动耗尽,P0 直接 EXHAUSTED;"
                              f"裁决证据={json_dumps(agent.meta_evidence())[:300]}")
        return self._exhausted(out.errors, reason=out.reason or out.status)

    # ------------------------------------------------------------- 语义关

    def _semantic_stage(self) -> Outcome | None:
        assert self.draft is not None
        key = self.draft.state_hash()
        if self._semantic_cache and self._semantic_cache[0] == key:
            res = self._semantic_cache[1]      # draft 与 fragments 双哈希未变 → 复用
            self._emit(TraceKind.SEMANTIC_VERIFY, reused=True, verdicts=[])
        else:
            res = self.verifier.run(self.spec, self.draft)
            self._semantic_cache = (key, res)
            self._emit(TraceKind.SEMANTIC_VERIFY,
                       verdicts=[v.to_dict() for v in res.verdicts],
                       dropped_invalid=res.dropped_invalid)
        self.last_semantic = res
        if self._accepted_frags:
            res.verdicts = [v for v in res.verdicts
                            if v.fragment_id not in self._accepted_frags]
        if res.ambiguities():
            a = res.ambiguities()[0]
            q = AskQuestion(
                qid=f"sem_{a.fragment_id}",
                text=(f"你的原话「{a.evidence.quote}」本身有歧义"
                      f"(涉及 {a.evidence.yaml_path}),你的意思是?"),
                options=["按现译继续", "澄清后只重过该片段语义关"])
            return self._suspend(ResumePoint.SEMANTIC_VERIFY, q,
                                 verdict_kind=UserVerdict.ANSWER,
                                 only_fragment=a.fragment_id)
        drifts = res.drifts()
        if not drifts:
            return None
        errors = [StratError(code=ErrCode.SEMANTIC_DRIFT,
                             where=f"fragment:{v.fragment_id}",
                             msg=f"语义漂移[{v.dimension}] 期望「{v.expected}」译文「{v.actual}」",
                             related_paths=[v.evidence.yaml_path])
                  for v in drifts]
        # 同片段本轮只记一次(模型可能多维度各报一条,不能据此判"屡修屡败")
        for fid in dict.fromkeys(v.fragment_id for v in drifts):
            self.history.semantic_fail[fid] = self.history.semantic_fail.get(fid, 0) + 1
        twice = [f for f, n in self.history.semantic_fail.items() if n >= 2]
        if twice:
            v = next(x for x in drifts if x.fragment_id == twice[0])
            q = AskQuestion(
                qid=f"sem2_{twice[0]}",
                text=(f"片段 {twice[0]} 两次修复后语义仍对不上"
                      f"(期望「{v.expected}」/现译「{v.actual}」),摆给你裁决:接受现译"
                      "还是改规则?"))
            return self._suspend(ResumePoint.SEMANTIC_VERIFY, q,
                                 verdict_kind=UserVerdict.CONFIRM,
                                 only_fragment=twice[0])
        # 二次对账前注入修复循环(计入修复轮)
        guard = 0
        while errors and guard < 10:
            guard += 1
            out = self._repair(errors, guard - 1)
            if out is not None:
                return out
            res2 = self.verifier.run(self.spec, self.draft)
            self._semantic_cache = None
            self._emit(TraceKind.SEMANTIC_VERIFY,
                       verdicts=[v.to_dict() for v in res2.verdicts])
            errors = [StratError(code=ErrCode.SEMANTIC_DRIFT,
                                 where=f"fragment:{v.fragment_id}",
                                 msg=f"语义漂移[{v.dimension}] {v.expected}≠{v.actual}",
                                 related_paths=[v.evidence.yaml_path])
                      for v in res2.drifts()
                      if v.fragment_id not in self._accepted_frags]
            for fid in dict.fromkeys(v.fragment_id for v in res2.drifts()):
                self.history.semantic_fail[fid] = \
                    self.history.semantic_fail.get(fid, 0) + 1
        if errors:
            return self._exhausted(errors, reason="semantic drift 修复未尽")
        return None

    # ------------------------------------------------------------- NEEDS_ASK 分级(v2 #6)

    def judge_suspect_ask(self, suspect: list[StratError]) -> Outcome:
        """JUDGE 可疑类 → 用户裁决三分支(确认继续/调整参数级/调整新增语义)。"""
        e = suspect[0]
        q = AskQuestion(
            qid="judge_suspect",
            text=(f"判读可疑:{e.msg}。这不是硬伤,你说了算——"
                  "①确认继续(交付含此特征的策略)②调整参数(说新值)③补充/修改规则。"),
            options=[UserVerdict.CONFIRM, UserVerdict.ADJUST_PARAM, UserVerdict.ADJUST_NEW])
        return self._suspend(ResumePoint.SEMANTIC_VERIFY, q,
                             verdict_kind="", pending_errors=suspect)

    def resume_after_suspect(self, verdict: str, answer: str = "") -> Outcome:
        """续流落点执行(v3 转正表):各分支已过段一律不重跑。"""
        if verdict == UserVerdict.CONFIRM:
            self._emit(TraceKind.RESUME, user_verdict=verdict, to=ResumePoint.SEMANTIC_VERIFY)
            self.notes.append("用户确认可疑类继续(误杀标注,回流调参 §4.4)")
            out = self._semantic_stage()
            return out or self._finish()
        if verdict == UserVerdict.ADJUST_PARAM:
            self._patch_spec_param(answer)
            self._emit(TraceKind.RESUME, user_verdict=verdict, to=ResumePoint.DRAFT)
            o = self._draft_stage()
            if o:
                return o
            o = self._gates_stage()
            if o:
                return o
            o = self._semantic_stage()
            return o or self._finish()
        if verdict == UserVerdict.ADJUST_NEW:
            self._add_fragment_from_answer(answer)
            self._emit(TraceKind.RESUME, user_verdict=verdict, to=ResumePoint.PLAN_INCREMENTAL)
            self._plan()          # 增量口径:重跑 plan 便宜且幂等(P0 全量代替增量集)
            o = self._draft_stage()
            if o:
                return o
            o = self._gates_stage()
            if o:
                return o
            o = self._semantic_stage()
            return o or self._finish()
        return self._fail(f"未知裁决 {verdict}")

    def _patch_spec_param(self, answer: str) -> None:
        """参数级调整:IntentSpec 注记 + 相关 fragment 文本更新(正则提取 数字%)。"""
        self.spec.revision += 1
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*%?", answer)
        self.spec.notes.append(f"用户参数调整:{answer}"
                               + (f"(值={m.group(1)})" if m else ""))
        self.spec.inference_authority = "explicit"

    def _add_fragment_from_answer(self, answer: str) -> None:
        fid = f"f{len(self.spec.fragments) + 1}"
        kind = "entry" if any(w in answer for w in ("买", "进", "突破")) else "exit"
        self.spec.fragments.append(Fragment(fid, answer, kind))
        self.spec.revision += 1

    # ------------------------------------------------------------- 挂起/终局/收尾

    def _suspend(self, point: str, question: AskQuestion,
                 verdict_kind: str = "", *, agent: RepairAgent | None = None,
                 scope_request: dict | None = None,
                 only_fragment: str = "",
                 pending_errors: list[StratError] | None = None) -> Outcome:
        self._emit(TraceKind.INTERRUPT, point=point, resume_from=point,
                   question=question.to_dict())
        payload = AskPayload(point="pipeline", question=question,
                             scope_request=scope_request, resume_from=point,
                             repair_context={"only_fragment": only_fragment}
                             if only_fragment else None)
        out = self._outcome(ExitStatus.NEEDS_ASK, pending=payload,
                            errors=pending_errors or [])
        out.reason = f"suspend@{point} verdict_kind={verdict_kind}"
        self._pending_agent = agent
        return out

    def resume_repair(self, answer: str, *, scope_granted: str = "") -> Outcome | None:
        """修复段挂起后进程内续跑(CLI 交互面;跨进程恢复属 P2 API)。"""
        agent = getattr(self, "_pending_agent", None)
        if agent is None:
            return self._fail("无可恢复的修复循环")
        agent.answer(answer)
        if scope_granted:
            agent.grant_scope(scope_granted)
        out = agent.resume()
        self._pending_agent = None
        settled = self._settle_repair(agent, out)
        if settled is not None:
            return settled
        errors = self._all_gates()
        if errors:
            return self._exhausted(errors, reason="续流后关卡仍未过")
        sem = self._semantic_stage()
        return sem or self._finish()

    def _exhausted(self, errors: list[StratError], reason: str = "") -> Outcome:
        out = self._outcome(ExitStatus.EXHAUSTED, errors=errors)
        out.reason = reason or "修复预算烧尽(产物+卡点交还,trace 全量留存)"
        return out

    def _fail(self, reason: str) -> Outcome:
        out = self._outcome(ExitStatus.FAILED)
        out.reason = reason
        return out

    def _finish(self) -> Outcome:
        assert self.draft is not None
        from .synthesize import SynthesizeService
        svc = SynthesizeService(lambda msgs: self.chat_port.ask(
            msgs[-1]["content"], stage="synthesize", max_tokens=1800, temperature=0.3))
        metrics = (self.result or {}).get("metrics") or {}
        report, llm_ok = svc.run(self.spec, self.draft.to_yaml_text(), metrics, self.notes)
        self._emit(TraceKind.STATE_TRANSITION, to="synthesize_done", llm=llm_ok)
        out = self._outcome(ExitStatus.READY, report=report, llm_report=llm_ok)
        out.metrics = metrics
        return out

    def _outcome(self, status: str, **kw) -> Outcome:
        return Outcome(
            exit_status=status, spec=self.spec, draft=self.draft,
            yaml_text=self.draft.to_yaml_text() if self.draft else "",
            llm_calls=self.chat_port.calls,
            repair_rounds=self.history.rounds_used,
            repair_steps=self._repair_steps_total,
            scope_denied=self._scope_denied_total,
            semantic_fail_count=sum(self.history.semantic_fail.values()),
            cost_versions=dict(self.chat_port.versions), **kw)

    def _emit(self, ev_or_kind, **kw) -> None:
        ev = ev_or_kind if isinstance(ev_or_kind, TraceEvent) \
            else TraceEvent(str(ev_or_kind), kw)
        self.on_event(ev)


# ================================================================ NEEDS_ASK(可疑)路由辅助

def split_errors(errors: list[StratError]) -> tuple[list[StratError], list[StratError]]:
    """(硬伤→修复, 可疑→ASK_USER)。"""
    from .contracts import SUSPECT_CODES
    hard = [e for e in errors if e.code not in SUSPECT_CODES]
    suspect = [e for e in errors if e.code in SUSPECT_CODES]
    return hard, suspect
