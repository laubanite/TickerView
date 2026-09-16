# -*- coding: utf-8 -*-
"""观测:JSONL trace(过程面)+ SQLite nl2strat_run 索引(结果面)(§4.4)。

- trace 契约在 P0 就设计全(每 LLM 调用/工具调用/状态迁移都是数据);
- 落 data/nl2strat/traces/{run_id}.jsonl(可写用户数据根经 paths.py);
- nl2strat_run 表用 contracts.RUN_TABLE_DDL 现场建(CREATE IF NOT EXISTS,
  不侵入 db.py 生产路径;同库 DB_PATH);
- 运行态快照(草稿树+spec+续流位)落 state/{run_id}.json——供中断续流恢复,
  与 trace 同目录根。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from alphaprism.paths import DATA_DIR, ensure_user_dirs

from .contracts import (
    RUN_TABLE_DDL, RunRecord, TraceEvent, json_dumps,
)

TRACE_DIR = DATA_DIR / "nl2strat" / "traces"
STATE_DIR = DATA_DIR / "nl2strat" / "state"


class RunTrace:
    """单 run 的事件流写入器(逐行 JSON,首行 run_meta 含输入全文)。"""

    def __init__(self, run_id: str, base_dir: Path | None = None) -> None:
        ensure_user_dirs()
        self.run_id = run_id
        self.dir = Path(base_dir) if base_dir else TRACE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.jsonl"

    def start(self, input_text: str, session_id: str, tier: str,
              parent_run_id: str = "") -> None:
        self.write(TraceEvent("run_meta", {
            "run_id": self.run_id, "input_text": input_text, "session_id": session_id,
            "tier": tier, "parent_run_id": parent_run_id,
            "ts": datetime.now().isoformat(timespec="milliseconds")}))

    def write(self, ev: TraceEvent) -> None:
        payload = dict(ev.payload)
        payload.setdefault("ts", datetime.now().isoformat(timespec="milliseconds"))
        payload.setdefault("run_id", self.run_id)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": ev.kind, **payload},
                               ensure_ascii=False, default=str) + "\n")

    def event(self, kind: str, **payload) -> TraceEvent:
        ev = TraceEvent(kind, payload)
        self.write(ev)
        return ev

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- run 索引表

def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """既有 DB 同库连接;nl2strat 表现场建,零侵入生产 schema。"""
    if db_path is None:
        from alphaprism.paths import DB_PATH
        db_path = DB_PATH
    ensure_user_dirs()
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(RUN_TABLE_DDL)
    return conn


def save_run(record: RunRecord, conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    now = datetime.now().isoformat(timespec="seconds")
    record.created_at = record.created_at or now
    record.updated_at = now
    cols = ("run_id", "session_id", "parent_run_id", "spec_revision", "tier",
            "input_digest", "intent_json", "yaml_text", "metrics_json",
            "rounds", "steps", "llm_calls", "exit_status", "resume_from",
            "user_verdict", "semantic_fail_count", "scope_denied_count",
            "meta_decision", "error", "cost_json", "created_at", "updated_at")
    conn.execute(
        f"INSERT OR REPLACE INTO nl2strat_run ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})", record.to_row())
    conn.commit()
    if own:
        conn.close()


def load_run(run_id: str, conn: sqlite3.Connection | None = None) -> RunRecord | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT * FROM nl2strat_run WHERE run_id=?", (run_id,)).fetchone()
        return RunRecord.from_row(row) if row else None
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------- 运行态快照(续流)

def save_state(run_id: str, state: dict, base_dir: Path | None = None) -> Path:
    d = Path(base_dir) if base_dir else STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{run_id}.json"
    p.write_text(json_dumps(state), encoding="utf-8")
    return p


def load_state(run_id: str, base_dir: Path | None = None) -> dict | None:
    d = Path(base_dir) if base_dir else STATE_DIR
    p = d / f"{run_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
