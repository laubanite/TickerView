# -*- coding: utf-8 -*-
"""run 会话编排:understand 三态处理 + Pipeline 挂起续跑派发 + 落盘(§4.1/§4.2)。

交互答复经 `ask_fn(question: AskQuestion) -> str` 注入:
- CLI 面 = input();
- 测试面 = 脚本队列;
- 返回空串且 interactive=False → 挂起原样返回(NEEDS_ASK 出口,P2 API 形态)。
落盘三件:trace JSONL(全事件)、nl2strat_run 索引、state 快照(挂起点)。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .contracts import (
    ExitStatus, IntentSpec, ResumePoint, UserVerdict, AskPayload, AskQuestion,
    RunRecord, TraceEvent, TraceKind, errors_to_dict, json_dumps, sha1_of,
)
from .loop import Outcome, Pipeline
from .trace import RunTrace, connect, load_state, save_run, save_state
from .understand import UnderstandService, merge_answers
from .prompts import PROMPT_VERSIONS


@dataclass
class SessionOutcome:
    exit_status: str
    run_id: str = ""
    session_id: str = ""
    outcome: Outcome | None = None
    yaml_path: str = ""
    trace_path: str = ""
    questions: list[AskQuestion] = field(default_factory=list)
    unsupported: list = field(default_factory=list)
    reason: str = ""


class Session:
    def __init__(self, *, chat: Callable[..., str | None], cfg=None,
                 tier: str = "free", interactive: bool = True,
                 ask_fn: Callable[[AskQuestion], str] | None = None,
                 out_dir: Path | None = None, db_path=None,
                 trace_dir: Path | None = None) -> None:
        self.chat = chat
        self.cfg = cfg
        self.tier = tier
        self.interactive = interactive
        self.ask_fn = ask_fn
        self.out_dir = out_dir
        self.db_path = db_path
        self.trace_dir = trace_dir

    # ------------------------------------------------------------ 主入口

    def run(self, text: str, *, session_id: str = "",
            run_id: str = "") -> SessionOutcome:
        run_id = run_id or uuid.uuid4().hex[:10]
        session_id = session_id or uuid.uuid4().hex[:8]
        trace = RunTrace(run_id, base_dir=self.trace_dir)
        trace.start(text, session_id, self.tier)

        def emit(ev: TraceEvent) -> None:
            trace.write(ev)

        # —— understand(NEEDS_ASK 补答:合入答复 → 确定性重核验,不重烧理解 LLM;
        #    未定稿的"重跑 understand"在语义上=补槽再核验,零 LLM 等价实现)
        us = UnderstandService(self.chat, self.cfg)
        ures = us.run(text, session_id=session_id, run_id=run_id)
        rounds = 0
        while ures.state == "NEEDS_ASK" and rounds < 3:
            rounds += 1
            answers: dict[str, str] = {}
            for q in ures.questions:
                ans = self._answer(q)
                if ans is None:
                    return self._suspend_outcome(run_id, session_id, trace, ures.spec,
                                                 [q], ResumePoint.UNDERSTAND,
                                                 "understand 待补答(非交互退出)")
                answers[q.qid] = ans
                if q.field_hint:
                    answers[q.field_hint] = ans
            emit(TraceEvent(TraceKind.RESUME,
                            {"point": ResumePoint.UNDERSTAND,
                             "answers": list(answers.values())[:4]}))
            base_spec = ures.spec or IntentSpec(session_id=session_id, run_id=run_id)
            ures = us.reverify(merge_answers(base_spec, answers))
        if ures.state == "UNSUPPORTED":
            self._record(RunRecord(run_id=run_id, session_id=session_id, tier=self.tier,
                                   input_digest=sha1_of(text),
                                   exit_status=ExitStatus.UNSUPPORTED,
                                   error=json_dumps([vars(u) for u in ures.unsupported])[:900]),
                         emit)
            return SessionOutcome(ExitStatus.UNSUPPORTED, run_id, session_id,
                                  unsupported=ures.unsupported,
                                  reason="词汇表面拒绝(不静默近似)",
                                  trace_path=str(trace.path))
        spec = ures.spec
        assert spec is not None
        spec.session_id, spec.run_id = session_id, run_id

        # —— Pipeline(挂起派发:judge 可疑/修复段/语义 ambiguous)
        pl = Pipeline(spec, chat=self.chat, cfg=self.cfg, tier=self.tier, on_event=emit)
        out = pl.run()
        guard = 0
        while out.exit_status == ExitStatus.NEEDS_ASK and out.pending and guard < 8:
            guard += 1
            point = out.pending.resume_from
            q = out.pending.question
            ans = self._answer(q) if q else ""
            if ans is None:
                save_state(run_id, {"spec": spec.to_dict(), "point": point,
                                    "question": q.to_dict() if q else None})
                self._record(RunRecord(run_id=run_id, session_id=session_id, tier=self.tier,
                                       input_digest=sha1_of(text),
                                       intent_json=spec.to_json(),
                                       exit_status=ExitStatus.NEEDS_ASK,
                                       resume_from=point,
                                       llm_calls=out.llm_calls), emit)
                return SessionOutcome(ExitStatus.NEEDS_ASK, run_id, session_id,
                                      outcome=out, trace_path=str(trace.path),
                                      questions=[q] if q else [],
                                      reason="挂起等待用户裁决(会话可续)")
            emit(TraceEvent(TraceKind.RESUME,
                            {"point": point, "answer": ans[:160]}))
            out = self._dispatch_resume(pl, out, point, q, ans)

        # —— 终局落盘
        yaml_path = ""
        if out.draft is not None and out.exit_status == ExitStatus.READY:
            yaml_path = self._deliver(out, run_id)
        rec = RunRecord(
            run_id=run_id, session_id=session_id, tier=self.tier,
            input_digest=sha1_of(text), intent_json=spec.to_json(),
            yaml_text=out.yaml_text, metrics_json=json_dumps(out.metrics)[:2000],
            rounds=out.repair_rounds, steps=out.repair_steps,
            llm_calls=out.llm_calls, exit_status=out.exit_status,
            semantic_fail_count=out.semantic_fail_count,
            scope_denied_count=out.scope_denied,
            error=(out.reason + (";" if out.reason else "") +
                   json_dumps(errors_to_dict(out.errors)[:6]))[:900],
            cost_json=json_dumps({"llm_calls": out.llm_calls,
                                  "stages": out.cost_versions,
                                  "prompt_versions": PROMPT_VERSIONS}))
        self._record(rec, emit)
        return SessionOutcome(out.exit_status, run_id, session_id, outcome=out,
                              yaml_path=yaml_path, trace_path=str(trace.path),
                              reason=out.reason)

    # ------------------------------------------------------------ 内部

    def _suspend_outcome(self, run_id: str, session_id: str, trace: RunTrace,
                         spec: IntentSpec | None, questions: list[AskQuestion],
                         point: str, reason: str) -> SessionOutcome:
        return SessionOutcome(ExitStatus.NEEDS_ASK, run_id, session_id,
                              trace_path=str(trace.path), questions=questions,
                              reason=f"{reason}@{point}")

    @staticmethod
    def classify_suspect_verdict(answer: str) -> str:
        """可疑类三分支判定(确定性):确认/参数级(含数值)/新增语义(有叙述)。"""
        a = (answer or "").strip()
        if a in ("", "1", "y", "yes", "确认", "继续", UserVerdict.CONFIRM):
            return UserVerdict.CONFIRM
        if a in ("2", UserVerdict.ADJUST_PARAM) or re.search(r"\d", a):
            return UserVerdict.ADJUST_PARAM
        if a in ("3", UserVerdict.ADJUST_NEW) or len(a) >= 6:
            return UserVerdict.ADJUST_NEW
        return UserVerdict.CONFIRM

    def _dispatch_resume(self, pl: Pipeline, out: Outcome, point: str,
                         q: AskQuestion | None, ans: str) -> Outcome:
        if point == ResumePoint.REPAIR_LOOP:
            granted = ""
            text = (ans or "").strip().lower()
            if q and q.qid.startswith("scope") and \
                    text in ("grant", "y", "是", "确认", "1", "扩权"):
                granted = (out.pending.scope_request or {}).get("path", "") \
                    if out.pending else ""
            if getattr(pl, "_pending_agent", None) is not None:
                return pl.resume_repair(ans, scope_granted=granted)
            # 修复循环对象不在(理论不可达):按确认续跑语义关
            return pl.resume_after_suspect(UserVerdict.CONFIRM, ans)
        if q and (q.qid.startswith("sem_") or q.qid.startswith("sem2_")):
            # 语义 ambiguous/二败裁决:确认现译(仅该片段豁免,其余照旧)
            fid = re.sub(r"^sem2?_", "", q.qid)
            return pl.accept_semantic(fid, ans)
        verdict = self.classify_suspect_verdict(ans)
        return pl.resume_after_suspect(verdict, ans)

    def _answer(self, q: AskQuestion | None) -> str | None:
        if self.ask_fn:
            return self.ask_fn(q) if q else ""
        if not self.interactive:
            return None
        print(f"\n? {q.text if q else ''}")
        if q and q.options:
            print("  选项:" + " / ".join(f"{i + 1}.{o}" for i, o in enumerate(q.options)))
        try:
            return input("  回答> ").strip()
        except EOFError:
            return None

    def _deliver(self, out: Outcome, run_id: str) -> str:
        base = Path(self.out_dir) if self.out_dir else _default_out_dir()
        base.mkdir(parents=True, exist_ok=True)
        p = base / f"{run_id}.yaml"
        p.write_text(out.yaml_text, encoding="utf-8")
        return str(p)

    def _record(self, rec: RunRecord, emit) -> None:
        emit(TraceEvent(TraceKind.WRITEBACK, {"exit_status": rec.exit_status}))
        try:
            conn = connect(self.db_path) if self.db_path else connect()
            try:
                save_run(rec, conn)
            finally:
                conn.close()
        except OSError as exc:
            emit(TraceEvent(TraceKind.WRITEBACK, {"ok": False, "error": str(exc)[:200]}))


def _default_out_dir() -> Path:
    from alphaprism.paths import DATA_DIR
    return DATA_DIR / "nl2strat" / "strategies"
