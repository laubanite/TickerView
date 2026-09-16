# -*- coding: utf-8 -*-
"""nl2strat 数据契约(§8 冻结项①②:唯一事实源,契约即文档)。

覆盖范围:
- 节点路径语法(where / related_paths / patch_node 共用一套寻址):normalize_path;
- 错误码表与路由元数据(§8 冻结项④,并入本文件):ERROR_META;
- 授权集(Draft.apply_* 应用时检查;降级轨事后 diff 复用同一语义):AuthzSet;
- IntentSpec / Fragment / PlanTask / Draft 装配模型 / 语义裁决 / trace 事件 /
  resume_from 续流 / meta_judge 三路裁决(v6 契约,P1 实装) / 会话钩子三字段
  (session_id、run 血缘 parent_run_id、IntentSpec.revision——P2 增量编辑直接生长在此)。

纪律:
- 全部契约对象带 schema_version,JSON 落盘与恢复只走 to_dict/from_dict;
- semantic_verify 的 drift 判定强制证据(quote + yaml_path),无证据按无效丢弃;
- 草稿是代码持有的状态(Draft 树),模型永不回吐整篇 YAML;越界通道不存在——
  写动作在**应用时**对照授权集,拒绝即零副作用。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable

SCHEMA_VERSION = "1"

# 模型档(P1 接入付费双轨;P0 固定 FREE)
TIER_FREE = "free"
TIER_PAID = "paid"


# ================================================================ 节点路径
#
# 语法:点分标识符 + 方括号下标。dict 键用 `.key`,list 下标用 `[i]`。
#   signals[3].conds[1].threshold   series.vol5   indicators[0].params.period
# 伪路径:`fragment:<fid>` 表示"该语义片段的映射产物"(缺失 fragment 的新增授权,
# 不对应树内位置,由 AuthzSet 单独记账)。

_PATH_SEG = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?$")
_FRAGMENT_PATH = re.compile(r"^fragment:([A-Za-z0-9_\-]+)$")


def is_fragment_path(path: str) -> bool:
    return bool(_FRAGMENT_PATH.match(path or ""))


def fragment_of_path(path: str) -> str | None:
    m = _FRAGMENT_PATH.match(path or "")
    return m.group(1) if m else None


def normalize_path(path: str) -> str:
    """规范化节点路径;非法即抛 ValueError(契约层显式失败,不静默)。"""
    p = (path or "").strip()
    if not p:
        raise ValueError("空节点路径")
    if is_fragment_path(p):
        return p
    for seg in p.split("."):
        if not _PATH_SEG.match(seg):
            raise ValueError(f"非法路径段 {seg!r}(path={path!r})")
    return p


def path_tokens(path: str) -> list[Any]:
    """拆为 token 序列:str=字典键,int=列表下标。伪路径整体作为单 token。"""
    p = normalize_path(path)
    if is_fragment_path(p):
        return [p]
    toks: list[Any] = []
    for seg in p.split("."):
        m = _PATH_SEG.match(seg)
        assert m is not None
        toks.append(m.group(1))
        if m.group(2) is not None:
            toks.append(int(m.group(2)))
    return toks


def path_within(prefix: str, path: str) -> bool:
    """`prefix` 子树是否包含 `path`(prefix==path 也算;path 是 prefix 的祖先则不算——
    替换祖先会波及兄弟节点,必须由祖先级授权覆盖)。"""
    a, b = path_tokens(prefix), path_tokens(path)
    return len(a) <= len(b) and b[:len(a)] == a


def path_parent(path: str) -> str | None:
    """去掉最后一个 token 后的父路径;顶层返回 None。"""
    if is_fragment_path(path):
        return None
    toks = path_tokens(path)[:-1]
    return render_tokens(toks) if toks else None


def render_tokens(toks: list[Any]) -> str:
    out = ""
    first = True
    for t in toks:
        if isinstance(t, int):
            out += f"[{t}]"
        else:
            out += t if first else f".{t}"
        first = False
    return out


def path_join(prefix: str, key: str, index: int | None = None) -> str:
    p = f"{prefix}.{key}" if prefix else str(key)
    return f"{p}[{index}]" if index is not None else p


def resolve_path(root: Any, path: str) -> Any:
    """取节点值;不存在抛 KeyError/IndexError(调用方显式处理)。"""
    node = root
    for tok in path_tokens(path):
        if isinstance(tok, int):
            node = node[tok]
        else:
            if not isinstance(node, dict) or tok not in node:
                raise KeyError(f"路径不存在:{path}")
            node = node[tok]
    return node


def assign_path(root: Any, path: str, value: Any) -> None:
    """就地替换节点值(写动作的唯一落树入口)。
    list 下标==长度视为尾部插入(与 patch_node 的 insert 语义一致)。"""
    toks = path_tokens(path)
    node = root
    for tok in toks[:-1]:
        node = node[tok]
    last = toks[-1]
    if isinstance(last, int):
        if last == len(node) and isinstance(node, list):
            node.append(value)
        else:
            node[last] = value
    else:
        if not isinstance(node, dict):
            raise ValueError(f"不能向非字典节点写入键 {last!r}")
        node[last] = value


def delete_path(root: Any, path: str) -> Any:
    """删除节点(list 弹元素 / dict 弹键),返回被删值。"""
    toks = path_tokens(path)
    node = root
    for tok in toks[:-1]:
        node = node[tok]
    last = toks[-1]
    if isinstance(last, int):
        return node.pop(last)
    return node.pop(last)


# ================================================================ 错误码表(§8 冻结项④)

class ErrCode:
    # —— 结构/语义校验(validate)
    YAML_UNPARSEABLE = "YAML_UNPARSEABLE"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"          # 顶层结构/未知键/类型不符
    UNKNOWN_COND = "UNKNOWN_COND"                  # 条件类型不在 _CONDS 注册表
    REF_UNDEFINED = "REF_UNDEFINED"                # cond.ref 未定义(内建表与声明都没有)
    SERIES_SHADOW = "SERIES_SHADOW"                # 自定义 series 名撞内建名(P0 禁止)
    SERIES_DEF_INVALID = "SERIES_DEF_INVALID"      # series 声明参数非法(未知 type/source…)
    PARAM_MISSING = "PARAM_MISSING"                # 条件必填参数缺失
    PARAM_INVALID = "PARAM_INVALID"                # 参数类型/值域非法
    UNKNOWN_ACTION = "UNKNOWN_ACTION"              # 动作动词不在 P0 白名单
    EMPTY_SIGNALS = "EMPTY_SIGNALS"
    EMPTY_CONDS = "EMPTY_CONDS"                    # 信号无条件
    DUPLICATE_SIGNAL_ID = "DUPLICATE_SIGNAL_ID"
    SIGNAL_ID_REF_UNDEFINED = "SIGNAL_ID_REF_UNDEFINED"  # days_since/after_signal 指错
    EXIT_BEFORE_ENTRY = "EXIT_BEFORE_ENTRY"        # 顺序:退出类信号排在入场前
    P0_SUBSET_VIOLATION = "P0_SUBSET_VIOLATION"    # 用了 P0 封闭语义(多层/scoring…)
    # —— 完整性对账(reconcile,确定性零 LLM)
    MISSING_FRAGMENT = "MISSING_FRAGMENT"          # 该写的没写(fragment 无映射)
    EXTRA_SIGNAL = "EXTRA_SIGNAL"                  # 多了没来头的(signal 回链不在 fragments)
    # —— 语义对账(semantic_verify / draft_selfcheck / selfcheck)
    SEMANTIC_DRIFT = "SEMANTIC_DRIFT"              # 写进去的不是那个意思(带证据)
    SEMANTIC_AMBIGUOUS = "SEMANTIC_AMBIGUOUS"      # 原话本身歧义 → 直接 ASK_USER
    # —— 护栏(越界关卡)
    SCOPE_DENIED = "SCOPE_DENIED"                  # 应用时拒绝:不在授权集
    CONTRACT_INVALID = "CONTRACT_INVALID"          # 写动作参数不合契约(如重排 id 不齐)
    # —— 判读(judge)
    DATA_MISSING = "DATA_MISSING"                  # 行情缓存缺失/覆盖不足
    WINDOW_INVALID = "WINDOW_INVALID"
    ENGINE_ERROR = "ENGINE_ERROR"                  # 回测抛错(含 StrategyError)
    NO_TRADES = "NO_TRADES"                        # 硬伤:零成交
    FLAT_EQUITY = "FLAT_EQUITY"                    # 硬伤:净值全平
    SUSPECT_TURNOVER = "SUSPECT_TURNOVER"          # 可疑:高频与描述不符 → ASK_USER
    SUSPECT_FREQ_MISMATCH = "SUSPECT_FREQ_MISMATCH"
    # —— 预算/终局
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


#: 路由元数据:repair=注入修复子循环 / ask_user=升级用户裁决 / fail=终局出口
ERROR_META: dict[str, dict[str, str]] = {
    ErrCode.YAML_UNPARSEABLE: {"route": "repair", "desc": "YAML 无法解析"},
    ErrCode.SCHEMA_VIOLATION: {"route": "repair", "desc": "顶层结构不合规"},
    ErrCode.UNKNOWN_COND: {"route": "repair", "desc": "未知条件类型"},
    ErrCode.REF_UNDEFINED: {"route": "repair", "desc": "引用未定义序列"},
    ErrCode.SERIES_SHADOW: {"route": "repair", "desc": "自定义序列撞内建名"},
    ErrCode.SERIES_DEF_INVALID: {"route": "repair", "desc": "序列声明非法"},
    ErrCode.PARAM_MISSING: {"route": "repair", "desc": "条件缺必填参数"},
    ErrCode.PARAM_INVALID: {"route": "repair", "desc": "条件参数非法"},
    ErrCode.UNKNOWN_ACTION: {"route": "repair", "desc": "动作不在白名单"},
    ErrCode.EMPTY_SIGNALS: {"route": "repair", "desc": "无信号"},
    ErrCode.EMPTY_CONDS: {"route": "repair", "desc": "信号无条件"},
    ErrCode.DUPLICATE_SIGNAL_ID: {"route": "repair", "desc": "信号 id 重复"},
    ErrCode.SIGNAL_ID_REF_UNDEFINED: {"route": "repair", "desc": "信号互引不存在"},
    ErrCode.EXIT_BEFORE_ENTRY: {"route": "repair", "desc": "退出先于入场"},
    ErrCode.P0_SUBSET_VIOLATION: {"route": "repair", "desc": "越出 P0 语义子集"},
    ErrCode.MISSING_FRAGMENT: {"route": "repair", "desc": "语义片段缺映射"},
    ErrCode.EXTRA_SIGNAL: {"route": "repair", "desc": "多出无来头信号"},
    ErrCode.SEMANTIC_DRIFT: {"route": "repair", "desc": "译文与用户原话不符"},
    ErrCode.SEMANTIC_AMBIGUOUS: {"route": "ask_user", "desc": "原话歧义,请用户澄清"},
    ErrCode.SCOPE_DENIED: {"route": "guard", "desc": "越界修改被拒(不计轮,耗步)"},
    ErrCode.CONTRACT_INVALID: {"route": "guard", "desc": "写动作不合契约(不计轮,耗步)"},
    ErrCode.DATA_MISSING: {"route": "ask_user", "desc": "行情数据缺失"},
    ErrCode.WINDOW_INVALID: {"route": "ask_user", "desc": "回测区间非法"},
    ErrCode.ENGINE_ERROR: {"route": "repair", "desc": "引擎执行报错"},
    ErrCode.NO_TRADES: {"route": "repair", "desc": "硬伤:零成交"},
    ErrCode.FLAT_EQUITY: {"route": "repair", "desc": "硬伤:净值无波动"},
    ErrCode.SUSPECT_TURNOVER: {"route": "ask_user", "desc": "可疑:换手与描述不符"},
    ErrCode.SUSPECT_FREQ_MISMATCH: {"route": "ask_user", "desc": "可疑:频率与时间尺度不符"},
    ErrCode.BUDGET_EXHAUSTED: {"route": "fail", "desc": "预算耗尽"},
}

#: 语义可疑类(用户裁决"确认继续"=误杀标注,"调整"=真阳性,§4.2 JUDGE 分级)
SUSPECT_CODES = {ErrCode.SUSPECT_TURNOVER, ErrCode.SUSPECT_FREQ_MISMATCH}


# ================================================================ 基础契约工具

def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _from_dict(cls, data: dict):  # noqa: ANN202 - 泛型 dataclass 工厂
    """白名单构造:未知键忽略,缺省用字段默认(跨版本 JSON 前向兼容)。"""
    if not isinstance(data, dict):
        raise TypeError(f"{cls.__name__} 需要从 dict 恢复,得到 {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def sha1_of(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


# ================================================================ 理解段契约

FRAGMENT_KINDS = ("entry", "exit", "stop", "take_profit", "time_exit", "other")
ORIGIN_USER = "user_explicit"
ORIGIN_DEFAULT = "inferred_default"   # 缺省注入(对账相等基准的组成部分,§4.2 plan)
AUTHORITY_LEVELS = ("explicit", "default_ok", "ask")


@dataclass
class Fragment:
    """一条用户语义片段 = 对账/映射/语义验证的最小单元。"""
    fragment_id: str
    text: str                                   # 用户原话(摘录)
    kind: str = "entry"                         # FRAGMENT_KINDS
    origin: str = ORIGIN_USER                   # user_explicit | inferred_default

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Fragment":
        return _from_dict(cls, d)


@dataclass
class IntentSpec:
    """understand 的结构化产出(§4.1 槽位)。P0 只开放标的+区间覆盖。"""
    schema_version: str = SCHEMA_VERSION
    # —— v6 会话钩子三字段(零实现成本,P2 多轮增量编辑生长在此;§0.5#2)
    session_id: str = ""
    run_id: str = ""
    parent_run_id: str = ""                     # run 血缘(P0 恒空,P2 续跑指向上游 run)
    revision: int = 1                           # IntentSpec 版本(增量 patch 时 +1)
    # —— 语义槽位
    symbol: str = ""                            # 代码,如 159516
    symbol_name: str = ""                       # 中文名(核验回填)
    start: str = ""
    end: str = ""
    fragments: list[Fragment] = field(default_factory=list)
    position_intent: str = "single_full"        # P0 固定单层满配
    inference_authority: str = "default_ok"     # explicit | default_ok | ask
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fragments"] = [f.to_dict() if isinstance(f, Fragment) else f for f in self.fragments]
        return d

    def to_json(self) -> str:
        return json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> "IntentSpec":
        spec = _from_dict(cls, {k: v for k, v in d.items() if k != "fragments"})
        spec.fragments = [Fragment.from_dict(f) for f in (d.get("fragments") or [])]
        return spec

    def fragments_hash(self) -> str:
        return sha1_of(json_dumps([f.to_dict() for f in self.fragments]))

    def semantic_fragments(self) -> list[Fragment]:
        """参与 reconcile/semantic_verify 对账的片段(入场/出场/止损/止盈/时间退出)。"""
        return [f for f in self.fragments if f.kind in FRAGMENT_KINDS]


@dataclass
class AskQuestion:
    qid: str
    text: str
    field_hint: str = ""                        # 补答建议填哪个槽(symbol/start/…)
    options: list[str] = field(default_factory=list)
    default_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AskQuestion":
        return _from_dict(cls, d)


@dataclass
class UnsupportedItem:
    text: str                                   # 用户原话中不可表达项
    reason: str
    nearest_alternative: str = ""               # 最近似方案(不静默近似,§4.1)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UnsupportedItem":
        return _from_dict(cls, d)


# UnderstandResult 三态(§4.1):READY 进流水线 / NEEDS_ASK 追问可续 / UNSUPPORTED 显式拒绝
STATE_READY = "READY"
STATE_NEEDS_ASK = "NEEDS_ASK"
STATE_UNSUPPORTED = "UNSUPPORTED"


@dataclass
class UnderstandResult:
    state: str = STATE_READY
    spec: IntentSpec | None = None
    questions: list[AskQuestion] = field(default_factory=list)
    unsupported: list[UnsupportedItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _drop_none({
            "schema_version": SCHEMA_VERSION,
            "state": self.state,
            "spec": self.spec.to_dict() if self.spec else None,
            "questions": [q.to_dict() for q in self.questions],
            "unsupported": [u.to_dict() for u in self.unsupported],
        })

    @classmethod
    def from_dict(cls, d: dict) -> "UnderstandResult":
        return cls(
            state=d.get("state", STATE_READY),
            spec=IntentSpec.from_dict(d["spec"]) if d.get("spec") else None,
            questions=[AskQuestion.from_dict(q) for q in (d.get("questions") or [])],
            unsupported=[UnsupportedItem.from_dict(u) for u in (d.get("unsupported") or [])],
        )


@dataclass
class PlanTask:
    """plan 子任务:一条用户语义 → 一个 DSL 条件候选(§4.2 逐条映射)。"""
    fragment_id: str
    original: str = ""                          # 原话回显
    mapping_hypothesis: str = ""                # 拟用条件/序列的语义描述(非 YAML)
    inferred_default: bool = False              # 缺省注入条目(对账基准的组成)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlanTask":
        return _from_dict(cls, d)


# ================================================================ 语义裁决契约(证据强制)

SEMANTIC_DIMENSIONS = ("direction", "object", "param", "combo")  # 方向/对象/参数/组合
VERDICTS = ("ok", "drift", "ambiguous")


@dataclass
class SemanticEvidence:
    quote: str = ""              # 用户原句摘录
    yaml_path: str = ""          # YAML 节点路径

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticEvidence":
        return _from_dict(cls, d)


@dataclass
class SemanticVerdict:
    """semantic_verify 单条裁决(§4.2 rubric)。"""
    fragment_id: str
    dimension: str = "direction"            # SEMANTIC_DIMENSIONS
    verdict: str = "ok"                     # ok | drift | ambiguous
    expected: str = ""                      # 原话要求什么
    actual: str = ""                        # 译文做了什么
    evidence: SemanticEvidence = field(default_factory=SemanticEvidence)
    source: str = "semantic_verify"         # semantic_verify | draft_selfcheck | selfcheck

    def is_admissible(self) -> bool:
        """确定性准入:维度/取值合法;drift|ambiguous 必须带双证据(无证据→无效丢弃)。"""
        if self.dimension not in SEMANTIC_DIMENSIONS or self.verdict not in VERDICTS:
            return False
        if self.verdict in ("drift", "ambiguous"):
            return bool(self.evidence.quote.strip() and self.evidence.yaml_path.strip())
        return bool(self.fragment_id)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = self.evidence.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticVerdict":
        v = _from_dict(cls, {k: val for k, val in d.items() if k != "evidence"})
        ev = d.get("evidence")
        v.evidence = SemanticEvidence.from_dict(ev) if isinstance(ev, dict) else SemanticEvidence()
        return v


def admissible_verdicts(verdicts: Iterable[SemanticVerdict]) -> list[SemanticVerdict]:
    """过滤出准入子集(判定本身归 LLM,准入结构归确定性代码——§4.2"确定性落在结构准入")。"""
    return [v for v in verdicts if v.is_admissible()]


@dataclass
class SemanticVerifyResult:
    verdicts: list[SemanticVerdict] = field(default_factory=list)   # 准入后
    dropped_invalid: int = 0                                          # 无证据被丢弃数
    #: 确定性筛除的伪漂移(verdict, reason)——小模型把标准符号当错译之类,代码兜底
    screened: list = field(default_factory=list)
    model_note: str = ""

    def drifts(self) -> list[SemanticVerdict]:
        return [v for v in self.verdicts if v.verdict == "drift"]

    def ambiguities(self) -> list[SemanticVerdict]:
        return [v for v in self.verdicts if v.verdict == "ambiguous"]

    @property
    def faithful(self) -> bool:
        return not self.drifts() and not self.ambiguities()

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION,
                "verdicts": [v.to_dict() for v in self.verdicts],
                "dropped_invalid": self.dropped_invalid,
                "screened": list(self.screened),
                "model_note": self.model_note}

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticVerifyResult":
        return cls(verdicts=[SemanticVerdict.from_dict(v) for v in (d.get("verdicts") or [])],
                   dropped_invalid=int(d.get("dropped_invalid") or 0),
                   model_note=str(d.get("model_note") or ""))


# ================================================================ 写动作契约(§8 冻结项②)

REMAP_MODES = ("cond_replace", "param_patch")


@dataclass
class DiffEntry:
    """一次已应用写调用的变更条目(审计面:无条件进 trace,与护栏面解耦)。"""
    op: str                    # replace | insert | delete | reorder
    path: str
    before: Any = None         # 摘要化(截断 repr),完整草稿在 tree 快照
    after: Any = None
    origin: str = ""           # remap:fragment_id | patch_node | resume_patch

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApplyResult:
    """写动作统一返回:草稿要么全变要么不变(应用时拒绝=零副作用)。"""
    ok: bool
    diffs: list[DiffEntry] = field(default_factory=list)
    errors: list["StratError"] = field(default_factory=list)   # SCOPE_DENIED / CONTRACT_INVALID

    def error_codes(self) -> list[str]:
        return [e.code for e in self.errors]


@dataclass
class StratError:
    """面向修复设计的错误对象(§3 关键决策 2):where+candidates+related_paths 喂足修复 agent。"""
    code: str
    where: str = ""                       # 节点路径或 fragment:<fid>
    msg: str = ""
    candidates: list[str] = field(default_factory=list)     # 最近似合法值(如条件名)
    related_paths: list[str] = field(default_factory=list)  # 引用图预计算派生路径

    @property
    def route(self) -> str:
        return (ERROR_META.get(self.code) or {}).get("route", "repair")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StratError":
        return _from_dict(cls, d)


def errors_to_dict(errors: list[StratError]) -> list[dict]:
    return [e.to_dict() for e in errors]


def errors_from_dict(items: list[dict]) -> list[StratError]:
    return [StratError.from_dict(i) for i in items or []]


@dataclass(frozen=True)
class AuthzSet:
    """授权集 = ∪(errors.where 子树 ∪ errors.related_paths ∪ granted 扩权路径)。

    remap 天然限于 fragment 粒度(该片段的映射子树 + 缺失片段的 fragment:<fid>);
    patch_node 在**应用时**对照本集合,不在 → 该调用拒绝(草稿不动、零副作用)。
    """
    paths: frozenset[str] = frozenset()
    fragments: frozenset[str] = frozenset()   # 伪路径 fragment:<fid> 展开后的 fid

    @staticmethod
    def build(errors: list[StratError], granted: Iterable[str] = ()) -> "AuthzSet":
        paths: set[str] = set()
        frags: set[str] = set()
        for e in errors:
            for p in [e.where, *e.related_paths]:
                if not p:
                    continue
                try:
                    norm = normalize_path(p)
                except ValueError:
                    continue
                fid = fragment_of_path(norm)
                if fid:
                    frags.add(fid)
                else:
                    paths.add(norm)
        for g in granted:
            norm = normalize_path(g)
            fid = fragment_of_path(norm)
            if fid:
                frags.add(fid)
            else:
                paths.add(norm)
        return AuthzSet(frozenset(paths), frozenset(frags))

    def allows(self, path: str) -> bool:
        """path 的变更效果子树必须完整落在某个授权子树内(authorized 是祖先或自身)。"""
        try:
            norm = normalize_path(path)
        except ValueError:
            return False
        return any(path_within(a, norm) for a in self.paths)

    def allows_fragment(self, fragment_id: str) -> bool:
        return fragment_id in self.fragments

    def merged(self, extra_paths: Iterable[str] = (), extra_fragments: Iterable[str] = ()) -> "AuthzSet":
        return AuthzSet(
            self.paths | {normalize_path(p) for p in extra_paths},
            self.fragments | set(extra_fragments),
        )

    def describe(self) -> list[str]:
        return sorted(self.paths) + [f"fragment:{f}" for f in sorted(self.fragments)]


# ================================================================ Draft 装配模型
#
# 草稿=代码持有的状态:YAML 节点树 + signal→fragment 回链表。
# 两个写工具(remap/patch_node)都是声明式的,由本类负责落到树并重序列化;
# 多 fragment 并行修复 = 多次工具调用的自然合成,不存在"谁组装"问题。

#: P0 单层规范形(§7#2 拍板取单层):持仓落 anchor 层——成本类退出条件
# (close_below_cost/hold_days_ge/recovery_pct)在引擎里读 anchor_cost,只有
# anchor 层承载成本;故 P0 以「单层=anchor」为唯一合法装配形。
# full_allocation=0.95(非 1.0):引擎 buy_layer 定仓不含佣金(现有产品策略
# 1/3 配仓从未触界),满配 100% 买入会令现金转负、触发卫生断言——5% 现金
# 缓冲吸收佣金与整手取整边缘(P0 不开放资金参数,该比例非用户语义)。
P0_CANONICAL_META: dict[str, Any] = {
    "accounts": {"full_cash": 100000, "full_allocation": 0.95},
    "layers": {"anchor": 100.0},
}
P0_LAYER = "anchor"
P0_SELL_ALL = "clear_anchor"
_FRAG_ID = re.compile(r"^[A-Za-z0-9_\-]{1,48}$")


@dataclass
class Draft:
    schema_version: str = SCHEMA_VERSION
    tree: dict = field(default_factory=dict)            # 引擎可直接序列化的 YAML 节点树
    fragment_of: dict = field(default_factory=dict)     # signal_id -> fragment_id

    # ---------- 构建 ----------
    @classmethod
    def blank(cls, name: str = "nl2strat") -> "Draft":
        return cls(tree={"name": name, **copy.deepcopy(P0_CANONICAL_META),
                         "indicators": [], "series": {}, "signals": []})

    @classmethod
    def from_entries(cls, name: str, entries: list[dict],
                     series: dict | None = None,
                     indicators: list[dict] | None = None) -> "Draft":
        """从 draft LLM 的结构化输出装配(code owns the tree)。

        entry = {"fragment_id": str, "signal": {"action": {...},
                 "conds": [cond, ...] | 平铺单条件(type+参数)}}
        规范形:signals 一律 type: and + conds 列表(平铺条件代码归一化),
        signal id 代码生成(sid_<fid>),模型不自取 id——消除 id 冲突类错误。
        """
        d = cls.blank(name)
        d.tree["series"] = copy.deepcopy(series or {})
        d.tree["indicators"] = copy.deepcopy(indicators or [])
        for i, e in enumerate(entries):
            fid = str(e.get("fragment_id") or "").strip()
            sig = e.get("signal")
            if not _FRAG_ID.match(fid) or not isinstance(sig, dict):
                raise ValueError(f"entry[{i}] 缺 fragment_id 或 signal 非法")
            conds = _normalize_conds(sig)
            action = sig.get("action")
            if not isinstance(action, dict) or not action:
                raise ValueError(f"entry[{i}] (fragment {fid}) signal 缺 action")
            sid = _unique_signal_id(d, f"sid_{fid}")
            node = {"id": sid, "type": "and", "conds": conds, "action": copy.deepcopy(action)}
            for opt in ("halt_add", "record_break_level", "layers"):
                if opt in sig:
                    node[opt] = sig[opt]
            d.tree["signals"].append(node)
            d.fragment_of[sid] = fid
        return d

    # ---------- 读 ----------
    def signals(self) -> list[dict]:
        return self.tree.get("signals") or []

    def signal_index(self, signal_id: str) -> int:
        for i, s in enumerate(self.signals()):
            if s.get("id") == signal_id:
                return i
        raise KeyError(signal_id)

    def indexes_of_fragment(self, fragment_id: str) -> list[int]:
        return [i for i, s in enumerate(self.signals())
                if self.fragment_of.get(s.get("id", "")) == fragment_id]

    def fragments_mapped(self) -> set[str]:
        return set(self.fragment_of.values())

    def semantic_view(self, fragment_ids: list[str] | None = None) -> list[dict]:
        """片段→实现条件的机械配对(semantic_verify 的定责视图)。

        代码负责"哪几条 cond 兑现了哪个片段"这一**确定性**关联,模型只需判断
        给定配对是否忠实——把开放式的"整篇 YAML 找茬"收窄为逐对核对,免费小模型
        误报率显著下降。返回 [{fragment_id, action, conds:[{path,type,ref,…}]}]。
        """
        want = set(fragment_ids) if fragment_ids else None
        out: list[dict] = []
        for fid in ([f for f in (fragment_ids or [])] or
                    sorted(set(self.fragment_of.values()))):
            if want is None and fid not in set(self.fragment_of.values()):
                continue
            idxs = self.indexes_of_fragment(fid)
            for i in idxs:
                sig = self.signals()[i]
                conds: list[dict] = []
                for j, c in enumerate(sig.get("conds") or []):
                    conds.append({"path": f"signals[{i}].conds[{j}]", **c})
                out.append({"fragment_id": fid, "signal_id": sig.get("id"),
                            "action": sig.get("action"), "conds": conds})
        return out

    # ---------- 序列化/哈希 ----------
    def to_yaml_text(self) -> str:
        import yaml
        return yaml.safe_dump(self.tree, allow_unicode=True, sort_keys=False,
                              default_flow_style=False)

    def yaml_hash(self) -> str:
        return sha1_of(self.to_yaml_text())

    def state_hash(self) -> str:
        """(yaml_hash, fragments 映射) 联合哈希——semantic_verify 缓存键(§4.2 v5)。"""
        return sha1_of(self.yaml_hash() + "|" + json_dumps(
            [self.fragment_of.get(s.get("id", "")) for s in self.signals()]))

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "tree": copy.deepcopy(self.tree),
                "fragment_of": dict(self.fragment_of)}

    @classmethod
    def from_dict(cls, d: dict) -> "Draft":
        dr = cls(schema_version=str(d.get("schema_version") or SCHEMA_VERSION),
                 tree=copy.deepcopy(d.get("tree") or {}),
                 fragment_of=dict(d.get("fragment_of") or {}))
        return dr

    def clone(self) -> "Draft":
        return Draft.from_dict(self.to_dict())

    # ---------- 写动作(应用时授权集检查;拒绝=零副作用) ----------
    def apply_remap(self, fragment_id: str, change: dict, authz: AuthzSet) -> ApplyResult:
        """remap 两档 schema(§8 冻结项②):

        - cond_replace: {"mode":"cond_replace","cond_index":0?, "new_cond":{type:...},
                         "action":{...}?(仅新建映射时可带)}
            · 该 fragment 已有映射 → 替换指定 conds[j] 子树;
            · 无映射(MISSING_FRAGMENT 修复)→ 以 new_cond(+action)新建 signal;
        - param_patch: {"mode":"param_patch","cond_index":0?,"params":{"threshold":5}}
            只动指定条件的参数键(参数错是最常见修复,给原子动作)。
        - relink(配对口): {"mode":"cond_replace","adopt_signal_id":"sid_x"}
            把孤儿信号(MISSING+EXTRA 同时出现=映射贴错标签)重挂到本 fragment 下,
            不开第三条写通道——仍是 fragment 粒度的"重映射一条语义映射"。
        """
        if not isinstance(change, dict):
            return ApplyResult(False, errors=[_scope_err(ErrCode.CONTRACT_INVALID,
                                                         "", "change 必须是对象")])
        adopt = str(change.get("adopt_signal_id") or "")
        if adopt:
            return self._remap_relink(fragment_id, adopt, authz)
        mode = str(change.get("mode") or "")
        if mode not in REMAP_MODES:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, "",
                f"remap.mode 必须是 {REMAP_MODES},得到 {mode!r}")])
        idxs = self.indexes_of_fragment(fragment_id)
        if mode == "cond_replace":
            new_cond = change.get("new_cond")
            if not isinstance(new_cond, dict) or "type" not in new_cond:
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.CONTRACT_INVALID, "", "cond_replace 需要 new_cond(含 type)")])
            if not idxs:
                # 新建映射(MISSING_FRAGMENT 修复):授权来源 = fragment 伪路径授权
                if not authz.allows_fragment(fragment_id):
                    return ApplyResult(False, errors=[_scope_err(
                        ErrCode.SCOPE_DENIED, f"fragment:{fragment_id}",
                        f"为 fragment {fragment_id} 新建映射不在授权集(需 MISSING_FRAGMENT 注入或扩权)",
                        related_paths=[] )])
                return self._remap_insert(fragment_id, new_cond, change.get("action"))
            if "action" in change:
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.CONTRACT_INVALID, "",
                    "已有映射的 action 变更请走 patch_node('signals[i].action',…)")])
            ci = int(change.get("cond_index", 0))
            i = idxs[0]
            path = f"signals[{i}].conds[{ci}]"
            # 效果路径级授权(典型注入:where=signals[i].conds[j]):
            if not (authz.allows_fragment(fragment_id) or authz.allows(path)):
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.SCOPE_DENIED, f"fragment:{fragment_id}",
                    f"重映射落点 {path} 不在本轮授权集 = {authz.describe()}",
                    related_paths=[f"signals[{i}]" for i in idxs])])
            try:
                resolve_path(self.tree, path)
            except (KeyError, IndexError):
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.CONTRACT_INVALID, path,
                    f"cond_index {ci} 越界(该 signal 共 "
                    f"{len(self.signals()[i].get('conds') or [])} 个条件)")])
            before = copy.deepcopy(resolve_path(self.tree, path))
            assign_path(self.tree, path, copy.deepcopy(new_cond))
            return ApplyResult(True, diffs=[DiffEntry("replace", path, _brief(before),
                                                      _brief(new_cond), f"fragment:{fragment_id}")])
        # param_patch
        params = change.get("params")
        if not isinstance(params, dict) or not params:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, "", "param_patch 需要非空 params")])
        if not idxs:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, f"fragment:{fragment_id}",
                "param_patch 目标不存在:该 fragment 尚无映射(换条件请用 cond_replace)")])
        ci = int(change.get("cond_index", 0))
        i = idxs[0]
        path = f"signals[{i}].conds[{ci}]"
        if not (authz.allows_fragment(fragment_id) or authz.allows(path)):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.SCOPE_DENIED, f"fragment:{fragment_id}",
                f"param_patch 落点 {path} 不在本轮授权集 = {authz.describe()}",
                related_paths=[f"signals[{i}]" for i in idxs])])
        try:
            cond = resolve_path(self.tree, path)
        except (KeyError, IndexError):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, path, f"cond_index {ci} 越界")])
        if not isinstance(cond, dict):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, path, "param_patch 目标是标量,请改用 cond_replace")])
        diffs = []
        for k, v in params.items():
            sub = path_join(path, k)
            before = copy.deepcopy(cond.get(k))
            cond[k] = v
            diffs.append(DiffEntry("replace", sub, _brief(before), _brief(v),
                                   f"fragment:{fragment_id}"))
        return ApplyResult(True, diffs=diffs)

    def _remap_relink(self, fragment_id: str, signal_id: str, authz: AuthzSet) -> ApplyResult:
        """把既有 signal 的重挂目标从旧 fragment 改到本 fragment(语义映射的"重挂")。

        典型配对:MISSING_FRAGMENT(f1) + EXTRA_SIGNAL(f0→signals[j])——映射贴错
        标签时一次修好两条,不开第三条写通道。授权:MISSING 给 fragment:f1 伪路径,
        EXTRA 的 where 给 signals[j],任一命中即可(两者常同轮出现)。
        """
        try:
            j = self.signal_index(signal_id)
        except KeyError:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, f"signals[{signal_id}]",
                f"adopt_signal_id {signal_id!r} 不存在")])
        if not (authz.allows_fragment(fragment_id) or authz.allows(f"signals[{j}]")):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.SCOPE_DENIED, signal_id,
                f"重挂 signals[{j}]→{fragment_id} 不在本轮授权集 = {authz.describe()}",
                related_paths=[f"signals[{j}]"])])
        old = self.fragment_of.get(signal_id)
        if old == fragment_id:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, signal_id,
                f"signals[{j}] 已挂在 {fragment_id},无需重挂")])
        self.fragment_of[signal_id] = fragment_id
        return ApplyResult(True, diffs=[DiffEntry(
            "replace", "fragment_of", old, fragment_id, f"fragment:{fragment_id}")])

    def _remap_insert(self, fragment_id: str, new_cond: dict, action: Any) -> ApplyResult:
        if not isinstance(action, dict) or not action:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, f"fragment:{fragment_id}",
                "为缺失片段新建映射必须带 action(如 {sell: clear_anchor})")])
        sid = _unique_signal_id(self, f"sid_{fragment_id}")
        node = {"id": sid, "type": "and", "conds": [copy.deepcopy(new_cond)],
                "action": copy.deepcopy(action)}
        self.tree["signals"].append(node)
        self.fragment_of[sid] = fragment_id
        idx = len(self.tree["signals"]) - 1
        return ApplyResult(True, diffs=[DiffEntry("insert", f"signals[{idx}]", None,
                                                  _brief(node), f"fragment:{fragment_id}")])

    def apply_patch_node(self, path: str, value: Any, authz: AuthzSet) -> ApplyResult:
        """非 fragment 节点的原子写(series/indicators 修正、信号顺序调整)。
        应用时检查授权集,不存在越界通道。"""
        try:
            norm = normalize_path(path)
        except ValueError as exc:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, str(path), f"路径非法:{exc}")])
        if is_fragment_path(norm):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, norm, "patch_node 不接受伪路径 fragment:*")])
        if not authz.allows(norm):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.SCOPE_DENIED, norm,
                f"越界修改 {norm},本轮授权 = {authz.describe()}",
                related_paths=list(authz.paths))])
        if norm == "signals":
            return self._patch_signals_order(value)
        if value is None:          # 删除语义(删死声明/冗余节点;必须已存在)
            try:
                before = copy.deepcopy(resolve_path(self.tree, norm))
            except (KeyError, IndexError):
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.CONTRACT_INVALID, norm, "删除目标不存在")])
            try:
                delete_path(self.tree, norm)
            except (KeyError, IndexError) as exc:
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.CONTRACT_INVALID, norm, f"删除失败:{exc}")])
            return ApplyResult(True,
                               diffs=[DiffEntry("remove", norm, _brief(before), None)])
        try:
            before = copy.deepcopy(resolve_path(self.tree, norm))
            op = "replace"
        except (KeyError, IndexError):
            before, op = None, "insert"
        parent = path_parent(norm)
        try:
            if parent is None:
                return ApplyResult(False, errors=[_scope_err(
                    ErrCode.CONTRACT_INVALID, norm, "顶层键变更请走 patch_node('signals',…) 或 remap")])
            assign_path(self.tree, norm, copy.deepcopy(value))
        except (KeyError, IndexError, ValueError) as exc:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, norm, f"赋值失败:{exc}")])
        return ApplyResult(True, diffs=[DiffEntry(op, norm, _brief(before), _brief(value))])

    def _patch_signals_order(self, value: Any) -> ApplyResult:
        """signals 整体重排/替换:必须覆盖全部现有 signal id(不吞映射;新增走 remap)。"""
        if not isinstance(value, list) or not all(isinstance(s, dict) and s.get("id") for s in value):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, "signals", "重排值必须是信号对象列表")])
        old_ids = [s.get("id") for s in self.signals()]
        new_ids = [s.get("id") for s in value]
        if sorted(map(str, old_ids)) != sorted(map(str, new_ids)):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.CONTRACT_INVALID, "signals",
                "重排信号集与原集不等(增删走 remap,这里只允许重排)")])
        before = [s.get("id") for s in self.signals()]
        self.tree["signals"] = copy.deepcopy(value)
        return ApplyResult(True, diffs=[DiffEntry("reorder", "signals", before, new_ids)])

    def apply_full_yaml(self, parsed: dict, authz: AuthzSet) -> ApplyResult:
        """降级轨入口:模型回吐整篇 YAML → 解析树级 diff,变更集 ⊄ 授权集 → reject 本轮。
        与主轨共用 AuthzSet 语义与 SCOPE_DENIED 错误码;通过则吸收为该草稿。"""
        if not isinstance(parsed, dict):
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.YAML_UNPARSEABLE, "", "整篇 YAML 必须是对象")])
        candidate = Draft(schema_version=self.schema_version,
                          tree=copy.deepcopy(parsed),
                          fragment_of=dict(self.fragment_of))
        # 信号 id↔fragment 回链按 id 继承;id 消失=隐式删除 → 视作未授权变更
        cand_ids = {s.get("id") for s in candidate.signals()}
        lost = [sid for sid in self.fragment_of if sid not in cand_ids]
        if lost:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.SCOPE_DENIED, "signals",
                f"整篇替换丢掉了信号-片段回链 {lost}(降级轨禁止增删信号,只能改授权内节点)")])
        diffs = tree_diff(self.tree, candidate.tree)
        viol = [d for d in diffs if not authz.allows(d.path)]
        if viol:
            return ApplyResult(False, errors=[_scope_err(
                ErrCode.SCOPE_DENIED, viol[0].path,
                f"越界变更 {len(viol)} 处:{[d.path for d in viol][:6]},"
                f"本轮授权 = {authz.describe()}", related_paths=list(authz.paths))])
        self.tree = candidate.tree
        return ApplyResult(True, diffs=diffs)


def _normalize_conds(sig: dict) -> list[dict]:
    """显式 conds 列表/单对象,或平铺单条件 → 统一 conds 列表(顺序保语义)。

    真实模型三种写法都出现过:`conds:[…]`、`conds:{…}`(单对象)、平铺 `type:`。
    """
    conds = sig.get("conds")
    if isinstance(conds, dict) and conds.get("type"):     # 单条件对象
        return [_normalize_conds_recursive(conds)]
    if isinstance(conds, list) and conds:
        out = []
        for c in conds:
            if isinstance(c, dict) and c.get("type"):
                out.append(_normalize_conds_recursive(c))
            elif isinstance(c, dict):
                raise ValueError("conds 项缺 type")
            else:
                raise ValueError("conds 项必须是对象")
        return out
    flat = {k: copy.deepcopy(v) for k, v in sig.items()
            if k not in ("id", "action", "conds", "halt_add", "layers",
                         "after_signal", "record_break_level")}
    if "type" not in flat:
        raise ValueError("signal 既无 conds 也无 type")
    return [flat]


def _normalize_conds_recursive(c: dict) -> dict:
    if c.get("type") in ("and", "or") and isinstance(c.get("conds"), list):
        return {**c, "conds": [_normalize_conds_recursive(x) for x in c["conds"]
                               if isinstance(x, dict) and x.get("type")]}
    return copy.deepcopy(c)


def _unique_signal_id(draft: "Draft", base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", base)[:40] or "sig"
    sid, n = base, 2
    while sid in draft.fragment_of:
        sid, n = f"{base}_{n}", n + 1
    return sid


def _scope_err(code: str, where: str, msg: str,
               candidates: list[str] | None = None,
               related_paths: list[str] | None = None) -> StratError:
    return StratError(code=code, where=where, msg=msg,
                      candidates=candidates or [], related_paths=related_paths or [])


def _brief(v: Any, limit: int = 240) -> Any:
    """diff 摘要化(完整状态由草稿树承担,审计只需可读切片)。"""
    s = json_dumps(v)
    return s if len(s) <= limit else s[:limit] + "…"


def tree_diff(a: Any, b: Any, path: str = "") -> list[DiffEntry]:
    """解析树级 diff(节点路径粒度;供降级轨越界校验与审计)。"""
    diffs: list[DiffEntry] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            p = path_join(path or "_root", str(k)) if path else str(k)
            if k not in a:
                diffs.append(DiffEntry("insert", p, None, _brief(b[k])))
            elif k not in b:
                diffs.append(DiffEntry("delete", p, _brief(a[k]), None))
            else:
                diffs.extend(tree_diff(a[k], b[k], p))
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}[{i}]"
            if i >= len(a):
                diffs.append(DiffEntry("insert", p, None, _brief(b[i])))
            elif i >= len(b):
                diffs.append(DiffEntry("delete", p, _brief(a[i]), None))
            else:
                diffs.extend(tree_diff(a[i], b[i], p))
    elif a != b:
        p = path or "$"
        diffs.append(DiffEntry("replace", p, _brief(a), _brief(b)))
    return diffs


# ================================================================ 修复循环/记忆契约

@dataclass
class RepairAttempt:
    """一次失败修复尝试(压成一行进工作记忆;也是 v6 meta-judge 的输入面——
    compaction 的副产品即裁决证据,不新增记忆面)。"""
    round_no: int
    step_no: int
    action: str                       # 工具名
    target: str = ""                  # fragment_id / path
    summary: str = ""                 # 一行摘要(折叠产物)
    result_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RepairAttempt":
        return _from_dict(cls, d)

    def one_line(self) -> str:
        codes = ",".join(self.result_codes) or "-"
        return f"[R{self.round_no}S{self.step_no}] {self.action} {self.target}: {self.summary}({codes})"


# ================================================================ 续流/终局/meta-judge

class ExitStatus:
    RUNNING = "RUNNING"
    READY = "READY"                    # 三出口之成功
    NEEDS_ASK = "NEEDS_ASK"            # 三出口之追问(会话可续;pending_questions 落 trace)
    UNSUPPORTED = "UNSUPPORTED"        # 三出口之拒绝(不静默近似)
    EXHAUSTED = "EXHAUSTED"            # 修复烧尽(交还部分产物+卡点;v6 前经 meta-judge)
    FAILED = "FAILED"                  # 不可修复故障(LLM 全挂/引擎环境错)


class ResumePoint:
    """nl2strat_run.resume_from 落点枚举(v3 续流表:已过段一律不重跑)。"""
    UNDERSTAND = "understand"          # NEEDS_ASK 补答 → 重跑 understand(未定稿,重跑是对的)
    SEMANTIC_VERIFY = "semantic_verify"  # 可疑类确认继续 → 直达语义关(不豁免忠实度)
    DRAFT = "draft"                    # 可疑类调整(参数级) → patch 后重进 draft 下游
    REPAIR_LOOP = "repair_loop"        # 修复段 ask_user/request_scope 答复 → 回子循环当新观察
    PLAN_INCREMENTAL = "plan_incremental"  # 调整(新增语义级) → 增量 plan(仅新 fragment)


class UserVerdict:
    CONFIRM = "confirm"                # 可疑类:确认继续(=启发式误杀标注,回流 §4.4)
    ADJUST_PARAM = "adjust_param"      # 调整(参数级):patch IntentSpec → 重进 draft
    ADJUST_NEW = "adjust_new"          # 调整(新增语义):增量 plan → draft → reconcile…
    ANSWER = "answer"                  # 修复段追问答复 / request_scope 确认


class MetaDecision:
    """v6 轻版升级裁决三路(P1 实装;P0 冻结契约,trace 事件形状先就位)。"""
    CONTINUE = "continue"              # 追加小额步预算(~2 步),用完即 EXHAUSTED
    REDRAFT = "redraft"                # 带原话+失败证据重跑 plan→draft(固定降档单候选)
    HANDOFF = "handoff"                # 维持 EXHAUSTED 交还用户


@dataclass
class AskPayload:
    """中断时落库的待续流载荷(nl2strat_run.pending_ask_json;答复后清除)。"""
    point: str = ""                    # ResumePoint 之一所在的阶段
    question: AskQuestion | None = None
    scope_request: dict | None = None  # {path, reason}(request_scope 挂起时)
    repair_context: dict | None = None  # 回子循环所需的最小恢复面
    resume_from: str = ""

    def to_dict(self) -> dict:
        return _drop_none({
            "schema_version": SCHEMA_VERSION,
            "point": self.point,
            "question": self.question.to_dict() if self.question else None,
            "scope_request": self.scope_request,
            "repair_context": self.repair_context,
            "resume_from": self.resume_from,
        })

    @classmethod
    def from_dict(cls, d: dict) -> "AskPayload":
        q = d.get("question")
        return cls(point=str(d.get("point") or ""),
                   question=AskQuestion.from_dict(q) if isinstance(q, dict) else None,
                   scope_request=d.get("scope_request"),
                   repair_context=d.get("repair_context"),
                   resume_from=str(d.get("resume_from") or ""))


# ================================================================ trace 事件契约(§4.4)

class TraceKind:
    LLM_CALL = "llm_call"              # {provider,model,tier,prompt_hash,tokens,latency}
    TOOL_CALL = "tool_call"            # {name,args_digest,result_codes}
    STATE_TRANSITION = "state_transition"
    REPAIR_STEP = "repair_step"        # {action,scope_denied?,scope_granted?,step?,round?}
    MEMORY_COMPACTION = "memory_compaction"   # {dropped:[…]}
    INTERRUPT = "interrupt"            # {point,resume_from}
    RESUME = "resume"                  # {user_verdict}
    SEMANTIC_VERIFY = "semantic_verify"  # {verdicts:[…],dropped_invalid}
    DRAFT_PATCH = "draft_patch"        # {diffs:[…]} —— 每次已应用写调用无条件记录(审计面)
    META_JUDGE = "meta_judge"          # {trigger,decision,evidence_ref}(v6;P1 起出现)
    WRITEBACK = "writeback"            # run 表/trace 落盘状态


@dataclass
class TraceEvent:
    kind: str
    payload: dict = field(default_factory=dict)

    def to_line(self) -> str:
        return json_dumps({"kind": self.kind, **self.payload})


# ================================================================ run 索引表行(P0 建表)

@dataclass
class RunRecord:
    """SQLite nl2strat_run 索引行(§4.4;input 摘要不落原文全文外的敏感面)。"""
    run_id: str = ""
    session_id: str = ""               # v6 会话钩子(P0 生成,P2 对话面复用)
    parent_run_id: str = ""            # run 血缘
    spec_revision: int = 1             # IntentSpec.revision 快照
    tier: str = TIER_FREE
    input_digest: str = ""             # 自然语言输入 sha1(全文在 trace 首行)
    intent_json: str = ""
    yaml_text: str = ""
    metrics_json: str = ""
    rounds: int = 0
    steps: int = 0
    llm_calls: int = 0
    exit_status: str = ExitStatus.RUNNING
    resume_from: str = ""
    user_verdict: str = ""
    semantic_fail_count: int = 0
    scope_denied_count: int = 0
    meta_decision: str = ""            # v6 列(P0 恒空)
    error: str = ""
    cost_json: str = ""                # {tokens,prompt_versions}
    created_at: str = ""
    updated_at: str = ""

    def to_row(self) -> tuple:
        f = asdict(self)
        return tuple(f[k] for k in _RUN_COLUMNS)

    @classmethod
    def from_row(cls, row) -> "RunRecord":
        d = {k: row[k] for k in _RUN_COLUMNS if k in row.keys()}
        return _from_dict(cls, d)


_RUN_COLUMNS = (
    "run_id", "session_id", "parent_run_id", "spec_revision", "tier",
    "input_digest", "intent_json", "yaml_text", "metrics_json",
    "rounds", "steps", "llm_calls", "exit_status", "resume_from",
    "user_verdict", "semantic_fail_count", "scope_denied_count",
    "meta_decision", "error", "cost_json", "created_at", "updated_at",
)

RUN_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS nl2strat_run (
    {_RUN_COLUMNS[0]:<14} TEXT PRIMARY KEY,
    session_id         TEXT,
    parent_run_id      TEXT,
    spec_revision      INTEGER NOT NULL DEFAULT 1,
    tier               TEXT NOT NULL DEFAULT 'free',
    input_digest       TEXT,
    intent_json        TEXT,
    yaml_text          TEXT,
    metrics_json       TEXT,
    rounds             INTEGER NOT NULL DEFAULT 0,
    steps              INTEGER NOT NULL DEFAULT 0,
    llm_calls          INTEGER NOT NULL DEFAULT 0,
    exit_status        TEXT NOT NULL DEFAULT '{ExitStatus.RUNNING}',
    resume_from        TEXT,
    user_verdict       TEXT,
    semantic_fail_count INTEGER NOT NULL DEFAULT 0,
    scope_denied_count INTEGER NOT NULL DEFAULT 0,
    meta_decision      TEXT,
    error              TEXT,
    cost_json          TEXT,
    created_at         TEXT,
    updated_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_nl2strat_run_session ON nl2strat_run (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nl2strat_run_exit ON nl2strat_run (exit_status);
"""

# ================================================================ 预算契约(§4.2 v6 表)

@dataclass(frozen=True)
class BudgetSpec:
    """LLM 调用预算构成。外层固定段 + 修复循环;meta_judge 三路是**条件界**
    (仅被动耗尽时计入,happy path 感知不到组件存在)。"""
    plan: int = 1
    draft: int = 1                       # 付费档=2(两候选);盲评调用另计
    draft_blind_eval: int = 0            # 付费档 2(samples=2)
    draft_selfcheck: int = 0             # 付费档 1(免费档默认关)
    semantic_verify: int = 1             # 哈希命中产源自查结论时记 0(复用)
    synthesize: int = 1
    repair_steps: int = 6                # 免费 6 / 付费 9(步=调用 1:1)
    repair_rounds: int = 3               # 轮(重起草后续用)
    meta_judge: int = 1                  # v6:每 run ≤1(被动耗尽才触发)
    redraft_plan: int = 1                # redraft 裁决后追加
    redraft_draft: int = 1
    redraft_selfcheck: int = 0           # 付费档重起草后再跑一次产源自查(§4.2 v6 账目)
    time_limit_s: int = 150
    time_extra_s: int = 30

    @property
    def happy_path_max(self) -> int:
        return (self.plan + self.draft + self.draft_blind_eval + self.draft_selfcheck
                + self.semantic_verify + self.synthesize + self.repair_steps)

    @property
    def worst_case_max(self) -> int:
        return (self.happy_path_max + self.meta_judge + self.redraft_plan
                + self.redraft_draft + self.redraft_selfcheck)


BUDGET_FREE = BudgetSpec()                                   # ≤10 happy / ≤13 最坏
BUDGET_PAID = BudgetSpec(draft=2, draft_blind_eval=2, draft_selfcheck=1, repair_steps=9,
                         redraft_selfcheck=1, time_limit_s=300, time_extra_s=60)  # ≤17/≤21


def budget_for(tier: str) -> BudgetSpec:
    return BUDGET_PAID if tier == TIER_PAID else BUDGET_FREE


# 语义验证往返(SEMANTIC_DRIFT 注入修复)计入修复轮数(实质重做,区别于越界打回);
# 越界拒绝(SCOPE_DENIED)不计轮、计入步预算(耗步不耗轮)——loop/repair 执行此口径。
