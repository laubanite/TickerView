# -*- coding: utf-8 -*-
"""修复子 agent:真自主循环 + 工作记忆折叠 + 越界护栏(§4.2 修复段全部纪律)。

- 动作空间封闭(10 注册工具;judge_candidates 仅付费档,免费档实为 9);
- 约束靠代码不靠 prompt:授权集在**应用时**检查(contracts.Draft 执行),
  越界不计轮耗步、同目标连拒 2 次升级 ask_user 兜底;折叠确定性零 LLM;
- LLM 决策面经 `next_action(context)->dict` 注入(主轨=P1 chat_tools;
  P0=JSON 契约+正则截取,在 loop 层适配;本模块与模型解耦、纯函数化可测);
- 终局语义(v6):give_up → 不触发 meta-judge("认输也是判断力");
  步/轮封顶 → 被动耗尽,meta_eligible=True(外层裁决点位)。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .contracts import (
    AskQuestion, AuthzSet, DiffEntry, Draft, ErrCode, IntentSpec, RepairAttempt,
    StratError, TraceEvent, TraceKind, json_dumps,
)

R_STATUS_PASSED = "passed"
R_STATUS_ASK_USER = "ask_user"
R_STATUS_SCOPE_PENDING = "scope_pending"
R_STATUS_GAVE_UP = "gave_up"
R_STATUS_EXHAUSTED = "exhausted"

#: 封闭动作集(声明式;函数 schema 由 loop 适配进 prompt/chat_tools)
TOOL_NAMES = ("dsl_card", "check_data", "selfcheck", "judge_candidates",
              "remap", "patch_node", "request_scope", "resubmit", "ask_user", "give_up")


@dataclass
class RepairBudget:
    steps_left: int
    rounds_left: int
    selfcheck_run: int = 2          # ≤2 次/run(工具表)
    selfcheck_round: int = 1        # ≤1 次/轮
    judge_round: int = 1            # ≤1 次/轮(付费档)
    _selfcheck_run: int = field(default=0, repr=False)
    _selfcheck_round: int = field(default=0, repr=False)
    _judge_round: int = field(default=0, repr=False)

    def spend_step(self) -> bool:
        if self.steps_left <= 0:
            return False
        self.steps_left -= 1
        return True

    def spend_round(self) -> bool:
        """resubmit = 轮边界;新轮重置轮内配额。"""
        if self.rounds_left <= 0:
            return False
        self.rounds_left -= 1
        self._selfcheck_round = 0
        self._judge_round = 0
        return True

    def take_selfcheck(self) -> bool:
        if self._selfcheck_round >= self.selfcheck_round or \
                self._selfcheck_run >= self.selfcheck_run or self.steps_left <= 0:
            return False
        self._selfcheck_round += 1
        self._selfcheck_run += 1
        return True

    def take_judge(self) -> bool:
        if self._judge_round >= self.judge_round:
            return False
        self._judge_round += 1
        return True


@dataclass
class RepairOutcome:
    status: str
    question: AskQuestion | None = None
    scope_request: dict | None = None
    meta_eligible: bool = False           # v6:仅被动耗尽为 True
    reason: str = ""
    errors: list[StratError] = field(default_factory=list)


class WorkingMemory:
    """确定性折叠(≈50 行,零 LLM;参照 smolagents step-memory 的"只留最近观察")。

    每轮进入模型的有效上下文固定构成:
    {最新 errors 清单, 当前草稿全文, 修复尝试摘要序列(每条一行), 剩余预算}
    """
    ATTEMPT_HEADER = "修复尝试摘要(一行一条):"

    def __init__(self) -> None:
        self.attempts: list[RepairAttempt] = []
        self.fold_count: dict[str, int] = {}      # where → 已修复轮数
        self._observations: list[str] = []

    def fold_errors(self, old_errors: list[StratError]) -> None:
        """触发点=errors 被新一轮覆盖:历史 errors 删除,仅存计数行(计数行在 context 现算)。"""
        for e in old_errors:
            key = e.where or e.code
            self.fold_count[key] = self.fold_count.get(key, 0) + 1

    def add_attempt(self, a: RepairAttempt) -> None:
        self.attempts.append(a)

    def put_observation(self, name: str, text: str) -> None:
        """工具观察:只活一轮(context 渲染后即截断——消费即截断)。"""
        self._observations.append(f"[观察:{name}] {text}")

    def context(self, errors: list[StratError], draft: Draft,
                steps_left: int, rounds_left: int,
                fragments: list[str] | None = None) -> str:
        parts: list[str] = []
        if fragments:
            parts.append("【用户原话片段(语义对账基准)】")
            parts.extend(fragments)
            parts.append("")
        parts.append("【当前错误清单】")
        if errors:
            for e in errors:
                parts.append(f"- {e.code} @ {e.where or '(全局)'}: {e.msg}")
                if e.candidates:
                    parts.append(f"    候选: {e.candidates}")
                if e.related_paths:
                    parts.append(f"    相关路径(已授权): {e.related_paths}")
        else:
            parts.append("(无——若判读已通过,请 resubmit)")
        fold_lines = [f"{where} 已尝试修复 {n} 轮" for where, n in sorted(self.fold_count.items())]
        parts.append("")
        parts.append(self.ATTEMPT_HEADER)
        for line in fold_lines + [a.one_line() for a in self.attempts]:
            parts.append(line)
        parts.append("")
        parts.append("【当前草稿 YAML(全文,代码持有;你只能通过注册工具修改)】")
        parts.append(draft.to_yaml_text())
        parts.append("")
        parts.append(f"【预算】步预算剩 {steps_left},轮预算剩 {rounds_left};"
                     f"remap/patch_node 只接受授权集内目标,越界将被应用时拒绝")
        if self._observations:
            parts.append("")
            parts.extend(self._observations)
            self._observations = []       # 消费即截断
        return "\n".join(parts)


class RepairAgent:
    """自主子循环:模型每步看工具结果自主选动作,直到 resubmit 通过/预算尽/挂起/放弃。"""

    def __init__(self, *, draft: Draft, spec: IntentSpec,
                 errors: list[StratError], budget: RepairBudget,
                 next_action: Callable[[str], dict],
                 gates: Callable[[], list[StratError]],
                 paid: bool = False,
                 selfcheck_fn: Callable[[list[str]], Any] | None = None,
                 judge_fn: Callable[[str, list[str]], Any] | None = None,
                 on_event: Callable[[TraceEvent], None] | None = None) -> None:
        self.draft = draft
        self.spec = spec
        self.errors = list(errors)
        self.budget = budget
        self.next_action = next_action
        self.gates = gates
        self.paid = paid
        self.selfcheck_fn = selfcheck_fn
        self.judge_fn = judge_fn
        self.on_event = on_event
        self.memory = WorkingMemory()
        self.granted: list[str] = []
        self.applied_patches: list[DiffEntry] = []
        self.unavailable_calls = 0
        self.denied: Counter[str] = Counter()        # 同目标连拒计数(兜底防乒乓)
        self.round_no = 1
        self.step_no = 0
        self._scope_request: dict | None = None
        self._question: AskQuestion | None = None
        self._answer_note = ""
        self._rebuild_authz()

    # ---------- 公共面 ----------
    def run(self) -> RepairOutcome:
        return self._loop()

    def resume(self) -> RepairOutcome:
        """ask_user 答复 / request_scope 确认后回子循环:答复成为新观察(§4.2 v3 续流表)。"""
        if self._answer_note:
            self.memory.put_observation("user_answer", self._answer_note)
            self._answer_note = ""
        self._question = None
        self._scope_request = None
        return self._loop()

    def answer(self, text: str) -> None:
        """追问答复:进摘要序列(审计+折叠面)并作为续跑首轮的新观察。"""
        self._answer_note = text
        self.memory.add_attempt(RepairAttempt(self.round_no, self.step_no,
                                              "answer", "", f"用户回答:{text[:120]}", []))

    def grant_scope(self, path: str) -> None:
        """request_scope 的"确认"答复附带把该路径授权扩入本轮。"""
        self.granted.append(path)
        self._rebuild_authz()
        self.memory.put_observation("scope_granted", f"已扩权:{path}")

    def meta_evidence(self) -> dict:
        """v6 meta-judge 输入面:全部来自 compaction 副产品,零新增记忆。"""
        dist: Counter[str] = Counter()
        for e in self.errors:
            dist[e.code] += 1
        for a in self.memory.attempts:
            for c in a.result_codes:
                dist[c] += 1
        return {"attempts": [a.one_line() for a in self.memory.attempts],
                "code_dist": dict(dist),
                "remaining": {"steps": self.budget.steps_left,
                              "rounds": self.budget.rounds_left}}

    # ---------- 内部 ----------
    def _rebuild_authz(self) -> AuthzSet:
        self.authz = AuthzSet.build(self.errors, granted=self.granted)
        return self.authz

    def _event(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            self.on_event(TraceEvent(kind, payload))

    def _loop(self) -> RepairOutcome:
        while True:
            if self.budget.steps_left <= 0:
                return self._exhausted("步预算耗尽(被动)")
            ctx = self.memory.context(self.errors, self.draft,
                                      self.budget.steps_left, self.budget.rounds_left,
                                      fragments=[f"- {f.fragment_id}: {f.text}"
                                                 for f in self.spec.semantic_fragments()])
            action = self.next_action(ctx) or {}
            self._event(TraceKind.REPAIR_STEP, step=self.step_no, round=self.round_no,
                        action=str(action.get("tool") or ("yaml" if "yaml" in action else "?")))
            if not self.budget.spend_step():
                return self._exhausted("步预算耗尽(被动)")
            self.step_no += 1
            if not isinstance(action, dict):
                self.memory.put_observation("contract", "动作必须是 JSON 对象")
                continue
            if "yaml" in action:
                out = self._act_full_yaml(action["yaml"])
                if out is not None:
                    return out
                continue
            tool = str(action.get("tool") or "")
            args = action.get("args") or {}
            out = self._dispatch(tool, args)
            if out is not None:
                return out

    # ---- 动作分发 ----
    def _dispatch(self, tool: str, args: dict) -> RepairOutcome | None:
        if tool not in TOOL_NAMES:
            self.memory.put_observation(
                "contract", f"动作 {tool!r} 不在封闭注册集 {list(TOOL_NAMES)},未执行")
            return None
        handler = getattr(self, f"_act_{tool}")
        return handler(args)

    # 读
    def _act_dsl_card(self, args: dict) -> None:
        from . import tools
        text = tools.dsl_card(str(args.get("query") or ""))
        self.memory.put_observation("dsl_card", text)
        self.memory.add_attempt(RepairAttempt(self.round_no, self.step_no,
                                              "dsl_card", str(args.get("query") or ""),
                                              "查词汇卡", []))

    def _act_check_data(self, args: dict) -> None:
        from . import tools
        res = tools.check_data(str(args.get("symbol") or self.spec.symbol),
                               str(args.get("start") or ""), str(args.get("end") or ""))
        self.memory.put_observation("check_data", json_dumps(res))

    def _act_selfcheck(self, args: dict) -> None:
        fids = [str(x) for x in (args.get("fragment_ids") or [])]
        if not self.selfcheck_fn:
            self.unavailable_calls += 1
            self.memory.put_observation("selfcheck", "本档未启用自检")
            return
        if not self.budget.take_selfcheck():
            self.memory.put_observation(
                "selfcheck", "selfcheck 本轮已用/超 run 上限(≤1/轮、≤2/run),未执行")
            return
        result = self.selfcheck_fn(fids) or []
        drifts = [r for r in result if getattr(r, "code", None) == ErrCode.SEMANTIC_DRIFT
                  or (isinstance(r, dict) and r.get("verdict") == "drift")]
        self.memory.put_observation(
            "selfcheck",
            json_dumps([r if isinstance(r, dict) else r.to_dict()
                        for r in result][:8]) if result else "抽查通过")
        if drifts:
            self.memory.add_attempt(RepairAttempt(self.round_no, self.step_no,
                                                  "selfcheck", ",".join(fids),
                                                  f"自查发现 {len(drifts)} 条 SEMANTIC_DRIFT",
                                                  [ErrCode.SEMANTIC_DRIFT]))

    def _act_judge_candidates(self, args: dict) -> None:
        if not self.paid:
            self.unavailable_calls += 1
            self.memory.put_observation(
                "judge_candidates", "免费档不暴露该工具(advisory 择优仅付费档)")
            return
        cands = [str(c) for c in (args.get("candidates") or [])]
        if not self.judge_fn or not self.budget.take_judge():
            self.memory.put_observation("judge_candidates", "未启用或本轮已用(≤1/轮)")
            return
        res = self.judge_fn(str(args.get("fragment_id") or ""), cands)
        self.memory.put_observation("judge_candidates", json_dumps(res))

    # 写(应用时授权集检查)
    def _act_remap(self, args: dict) -> RepairOutcome | None:
        fid = str(args.get("fragment_id") or "")
        change = args.get("change") or {}
        r = self.draft.apply_remap(fid, change, self.authz)
        return self._finish_write("remap", fid, r,
                                  detail=f"{change.get('mode')}→{fid}")

    def _act_patch_node(self, args: dict) -> RepairOutcome | None:
        path = str(args.get("path") or "")
        r = self.draft.apply_patch_node(path, args.get("value"), self.authz)
        return self._finish_write("patch_node", path, r, detail=path)

    def _finish_write(self, tool: str, target: str, r: Any, detail: str) -> RepairOutcome | None:
        if r.ok:
            self.denied.pop(f"{tool}:{target}", None)
            self.applied_patches.extend(r.diffs)
            self._event(TraceKind.DRAFT_PATCH,
                        diffs=[d.__dict__ for d in r.diffs], tool=tool)
            self.memory.add_attempt(RepairAttempt(self.round_no, self.step_no,
                                                  tool, target, f"已应用 {detail}", []))
            self.memory.put_observation(
                tool, "已应用:" + "; ".join(f"{d.op} {d.path}" for d in r.diffs)
                + "(草稿由代码持有,已重序列化)")
            return None
        # 拒绝路径:SCOPE_DENIED/CONTRACT_INVALID 不计轮、计步(已计);连拒 2 次升级
        code = r.errors[0].code if r.errors else ErrCode.CONTRACT_INVALID
        key = f"{tool}:{target}"
        self.denied[key] += 1
        self.memory.add_attempt(RepairAttempt(self.round_no, self.step_no,
                                              tool, target,
                                              (r.errors[0].msg if r.errors else "拒绝")[:120],
                                              [code]))
        self._event(TraceKind.REPAIR_STEP, action=tool, scope_denied=(code == ErrCode.SCOPE_DENIED),
                    target=target)
        self.memory.put_observation("reject", r.errors[0].msg if r.errors else "拒绝(零副作用)")
        if self.denied[key] >= 2:
            # 防乒乓兜底:同一目标连拒 2 次 → 强制升级用户裁决
            self._question = AskQuestion(
                qid=f"scope_{abs(hash(key)) % 10000}",
                text=(f"修复对「{target}」连续 {self.denied[key]} 次被越界关卡拒绝。"
                      f"最后一轮拒绝理由:{r.errors[0].msg[:160]}。"
                      "请裁决:扩大授权继续,还是交还人工?"),
                options=["grant", "handoff"])
            return RepairOutcome(R_STATUS_ASK_USER, question=self._question,
                                 errors=list(self.errors),
                                 reason="同目标连拒 2 次(兜底,正常应趋零)")
        return None

    def _act_full_yaml(self, payload: Any) -> RepairOutcome | None:
        """降级轨:整篇 YAML → 解析树级 diff,变更集 ⊄ 授权集 → reject 本轮(零副作用)。"""
        if isinstance(payload, str):
            import yaml as _yaml
            try:
                payload = _yaml.safe_load(payload)
            except Exception as exc:  # noqa: BLE001
                self.memory.put_observation("yaml", f"解析失败:{exc}")
                return None
        r = self.draft.apply_full_yaml(payload or {}, self.authz)
        return self._finish_write("yaml_replace", "signals", r, detail="整篇替换")

    # 界
    def _act_request_scope(self, args: dict) -> RepairOutcome:
        path = str(args.get("path") or "")
        reason = str(args.get("reason") or "")
        self._scope_request = {"path": path, "reason": reason}
        self._question = AskQuestion(
            qid=f"scope_{abs(hash(path)) % 10000}",
            text=f"修复判断根因在授权范围外的 `{path}`(理由:{reason})。是否扩大授权到该路径?",
            options=["grant", "deny"])
        return RepairOutcome(R_STATUS_SCOPE_PENDING, question=self._question,
                             scope_request=self._scope_request, errors=list(self.errors))

    def _act_resubmit(self, args: dict) -> RepairOutcome | None:
        if not self.budget.spend_round():
            return self._exhausted("轮预算耗尽(被动)")
        new_errors = self.gates() or []
        self.memory.fold_errors(self.errors)       # 触发点:errors 被新一轮覆盖
        self.errors = new_errors
        self.round_no += 1
        self._rebuild_authz()
        if not new_errors:
            return RepairOutcome(R_STATUS_PASSED, errors=[])
        return None

    def _act_ask_user(self, args: dict) -> RepairOutcome:
        q = str(args.get("question") or "需要你澄清一个语义")
        self._question = AskQuestion(qid=f"repair_q_{self.step_no}", text=q)
        return RepairOutcome(R_STATUS_ASK_USER, question=self._question,
                             errors=list(self.errors))

    def _act_give_up(self, args: dict) -> RepairOutcome:
        return RepairOutcome(R_STATUS_GAVE_UP, meta_eligible=False,
                             reason=str(args.get("reason") or "模型自认修不动"),
                             errors=list(self.errors))

    def _exhausted(self, reason: str) -> RepairOutcome:
        """被动耗尽:步/轮封顶且产物未过关卡 —— v6 meta-judge 唯一触发面。"""
        return RepairOutcome(R_STATUS_EXHAUSTED, meta_eligible=True, reason=reason,
                             errors=list(self.errors))


# ------------------------------------------------------------------ LLM 适配(P0 降级轨)

def parse_action_json(text: str) -> dict | None:
    """降级轨动作解析:正则截取首个 {…}(verifier._parse_scores 同套路),loop 层无感知。"""
    import json
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        js = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return js if isinstance(js, dict) else None


def make_json_action_fn(chat: Callable[[list[dict]], str], *, max_tokens: int = 900) -> Callable:
    """把 chat 端口包成 next_action:单条 JSON 动作契约(选工具+参数)。"""
    def act(ctx: str) -> dict:
        text = chat([{"role": "user", "content": ctx}]) or ""
        return parse_action_json(text) or {"tool": "give_up",
                                           "args": {"reason": "动作 JSON 解析失败"}}
    return act


TOOL_PROMPT = """你是策略修复员。你只能从下列封闭动作中选一个,输出【仅一个 JSON】:
  {"tool":"dsl_card","args":{"query":"…"}}              查词汇卡(只读)
  {"tool":"check_data","args":{"symbol","start","end"}} 数据核验(只读)
  {"tool":"selfcheck","args":{"fragment_ids":[…]}}      主动语义抽查(≤1/轮)
  {"tool":"judge_candidates","args":{…}}                候选盲评排序(仅付费档)
  {"tool":"remap","args":{"fragment_id","change"}}      重映射语义:
      change={"mode":"cond_replace","cond_index"?:0,"new_cond":{…type 必填…},"action"?:{…仅缺失片段新建时}}
      change={"mode":"param_patch","cond_index"?:0,"params":{键:值}}
      change={"mode":"cond_replace","adopt_signal_id":"sid_…"} 孤儿信号重挂(配对修复)
  {"tool":"patch_node","args":{"path":"…","value":…}}   非片段节点补丁(series/indicators/信号顺序)
  {"tool":"request_scope","args":{"path","reason"}}     申请越出授权范围(经用户确认)
  {"tool":"resubmit","args":{}}                         重新对账+校验+回测+判读(轮边界)
  {"tool":"ask_user","args":{"question":"…"}}           升级用户裁决(挂起)
  {"tool":"give_up","args":{"reason":"…"}}              认输交还(部分产物+卡点)
纪律:草稿由代码持有,你只能通过工具改;remap/patch_node 目标必须在提示给出的授权集内,
越界会被应用时拒绝(不耗轮只耗步)。语义可疑或参数含义不明时优先 ask_user,不许臆造。
"""
