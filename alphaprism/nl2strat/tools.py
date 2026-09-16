# -*- coding: utf-8 -*-
"""外层确定性关卡(§3 模块布局 / §4.2 外层步骤 4~7 / §8 冻结项②授权集消费面)。

- reconcile:fragment↔signal 集合相等对账(零 LLM;缺→MISSING_FRAGMENT,多→EXTRA_SIGNAL);
- validate_yaml:schema+语义校验,错误对象带 candidates 与 related_paths(引用图预计算)
  ——"把工具设计成能喂修复"是硬要求(§3 决策 2);
- run_backtest:真跑引擎(临时 YAML → run_backtest_layered);
- judge:判读分级(硬伤→打回修复 / 可疑→ASK_USER,§4.2 JUDGE 表);
- check_data / dsl_card:修复 agent 的两个只读观察工具。

依赖方向:本模块只 import contracts/vocabulary 与 backtest_engine(只读),
绝不 import llm.py(所有 LLM 调用面在 loop/understand/repair)。
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from .contracts import (
    P0_LAYER, Draft, ErrCode, Fragment, IntentSpec, StratError,
    normalize_path, path_join,
)
from .vocabulary import MAX_COND_DEPTH, Vocabulary, get_vocabulary

# 引擎在运行期注入 ctx.series 的名字(strategy_runner 语义,非 YAML 声明):
RUNTIME_SERIES = {"anchor_cost", "entry_break_level"}
CUSTOM_SERIES_TYPES = {"rolling_max", "rolling_min", "rolling_mean", "shift"}
SERIES_SOURCES = {"open", "high", "low", "close", "volume"}
#: P0 语义键:卖出必须被持仓门控条件保护(否则空仓时吞信号,evaluate_bar 单动作/bar)
HOLDING_GATES = {"layers_held", "layers_any"}

_SIGNAL_KEYS = {"id", "type", "conds", "action", "halt_add", "record_break_level", "layers",
                "after_signal"}


def _e(code: str, where: str, msg: str, candidates: list[str] | None = None,
       related: list[str] | None = None) -> StratError:
    return StratError(code=code, where=where, msg=msg,
                      candidates=candidates or [], related_paths=related or [])


# ================================================================ reconcile(存在性对账)

def reconcile(spec: IntentSpec, draft: Draft) -> list[StratError]:
    """集合相等而非计数相等(§4.2 外层步骤 4;缺省注入的 inferred_default 片段
    同样入基准——相等才有得对)。"""
    errors: list[StratError] = []
    want = {f.fragment_id for f in spec.semantic_fragments()}
    got = draft.fragments_mapped()
    for fid in sorted(want - got):
        errors.append(_e(ErrCode.MISSING_FRAGMENT, f"fragment:{fid}",
                         f"用户语义片段 {fid} 无任何 signal 映射(该写的没写)"))
    for sid, fid in sorted(draft.fragment_of.items(), key=lambda kv: str(kv[1])):
        if fid not in want:
            try:
                i = draft.signal_index(sid)
                where = f"signals[{i}]"
            except KeyError:
                where = ""
            errors.append(_e(ErrCode.EXTRA_SIGNAL, where,
                             f"signal {sid} 回链 {fid} 不在用户语义清单(多了没来头的)",
                             related=[f"fragment:{fid}"] if fid else []))
    return errors


# ================================================================ validate(schema+语义)

def _walk_nested(conds: list, prefix: str, out: list[tuple[str, dict]]) -> None:
    for j, c in enumerate(conds or []):
        p = f"{prefix}.conds[{j}]"
        if not isinstance(c, dict):
            continue
        out.append((p, c))
        if c.get("type") in ("and", "or"):
            _walk_nested(c.get("conds") or [], p, out)


class _RefGraph:
    """引用图预计算:cond→series/indicator/signal 的正反向边(§4.2 kimi #3 授权扩权)。"""

    def __init__(self, draft: Draft):
        self.draft = draft
        self.users: dict[str, list[str]] = {}       # ref 名 → cond 路径列表
        self._scan()

    def _scan(self) -> None:
        for i, sig in enumerate(self.draft.signals()):
            leaves: list[tuple[str, dict]] = []
            conds = sig.get("conds")
            base = f"signals[{i}]"
            if isinstance(conds, list):
                self.users.setdefault("__sig__", [])
                _walk_nested(conds, base, leaves)
            elif sig.get("type"):
                leaves.append((base, sig))
            for path, cond in leaves:
                r = cond.get("ref") if isinstance(cond, dict) else None
                if isinstance(r, str) and r:
                    self.users.setdefault(r, []).append(path)
                s = cond.get("since") if isinstance(cond, dict) else None
                if isinstance(s, str) and s:
                    self.users.setdefault(f"sid:{s}", []).append(path)

    def ref_uses(self, ref: str) -> list[str]:
        return self.users.get(ref, [])

    def sid_uses(self, sid: str) -> list[str]:
        return self.users.get(f"sid:{sid}", [])


def _legal_refs(voc: Vocabulary, draft: Draft) -> dict[str, str]:
    """ref 合法集 = 内建序列 ∪ 运行期注入。**自定义 series 声明不在其中**:
    引擎 `_series_table` 全量预计算内建列,YAML series/indicators 块不被计算
    ——引用自定义名是永不成立的死条件(NO_TRADES 陷阱),必须在校验层拆穿。"""
    legal: dict[str, str] = {}
    for n in voc.series:
        legal[n] = "builtin"
    for n in RUNTIME_SERIES:
        legal[n] = "builtin"
    return legal


def validate_yaml(draft: Draft, spec: IntentSpec | None = None,
                  voc: Vocabulary | None = None) -> list[StratError]:
    """确定性校验(不含 schema 库依赖——手写 ~校验器,requirements 极简纪律)。"""
    voc = voc or get_vocabulary()
    errors: list[StratError] = []
    tree = draft.tree
    graph = _RefGraph(draft)

    # ---- 顶层结构
    top = set(tree)
    allowed_top = {"name", "accounts", "layers", "indicators", "series", "signals"}
    for k in sorted(top - allowed_top):
        errors.append(_e(ErrCode.SCHEMA_VIOLATION, str(k), f"未知顶层键 {k!r}"))
    if not isinstance(tree.get("name"), str) or not tree.get("name"):
        errors.append(_e(ErrCode.SCHEMA_VIOLATION, "name", "name 必须是非空字符串"))

    # ---- accounts/layers 锁 P0 规范形(引擎参数开放度:§7#3,只开放区间与标的)
    accounts = tree.get("accounts") or {}
    if not isinstance(accounts, dict):
        errors.append(_e(ErrCode.SCHEMA_VIOLATION, "accounts", "accounts 必须是对象"))
    else:
        for k in accounts:
            if k not in ("full_cash", "full_allocation"):
                errors.append(_e(ErrCode.P0_SUBSET_VIOLATION, f"accounts.{k}",
                                 f"P0 锁定引擎参数,accounts.{k} 不允许"))
        fa = accounts.get("full_allocation")
        if fa is None or isinstance(fa, bool) or not isinstance(fa, (int, float)) or \
                not (0 < float(fa) <= 0.95):
            errors.append(_e(ErrCode.PARAM_INVALID, "accounts.full_allocation",
                             f"满配比例须 ∈(0,0.95](留佣金/取整缓冲,P0 规范形=0.95),"
                             f"得到 {fa!r}", candidates=["0.95"]))
    layers = tree.get("layers") or {}
    if not isinstance(layers, dict):
        errors.append(_e(ErrCode.SCHEMA_VIOLATION, "layers", "layers 必须是对象"))
    elif set(layers) - {P0_LAYER}:
        errors.append(_e(ErrCode.P0_SUBSET_VIOLATION, "layers",
                         f"P0 单层规范形只允许 {{anchor: 100.0}},得到 {layers}"))

    # ---- series 声明
    series_node = tree.get("series") or {}
    if not isinstance(series_node, dict):
        errors.append(_e(ErrCode.SCHEMA_VIOLATION, "series", "series 必须是对象"))
        series_node = {}
    for name, decl in series_node.items():
        p = f"series.{name}"
        users = sorted(set(graph.ref_uses(str(name))))
        if str(name) in voc.series:
            errors.append(_e(ErrCode.SERIES_SHADOW, p,
                             f"自定义 series {name!r} 撞内建名(P0 禁止覆盖)",
                             related=users))
            continue
        # 引擎事实(main._series_table 全量预计算内建列,声明块不被计算):
        # 任何自定义条目都是死引用源——引用它的 cond 永不成立(NO_TRADES 陷阱)。
        errors.append(_e(
            ErrCode.SERIES_DEF_INVALID, p,
            f"自定义 series {name!r}:P0 引擎只计算内建序列(声明块不被计算),引用它"
            "是永不成立的死条件——删除该声明,cond 改引用内建序列",
            candidates=difflib.get_close_matches(str(name), sorted(voc.series), 3),
            related=users))

    # ---- indicators 声明(P0 同上:块不被计算)
    for idx, ind in enumerate(tree.get("indicators") or []):
        p = f"indicators[{idx}]"
        if not isinstance(ind, dict):
            errors.append(_e(ErrCode.SERIES_DEF_INVALID, p, "indicator 必须是对象"))
            continue
        iid = str(ind.get("id", ""))
        if iid in voc.series:
            errors.append(_e(ErrCode.SERIES_SHADOW, path_join(p, "id"),
                             f"indicator id {iid!r} 撞内建序列名(引擎本就算它,"
                             "声明冗余,删除)"))
        else:
            errors.append(_e(ErrCode.SERIES_DEF_INVALID, p,
                             f"indicator {iid!r}:P0 引擎不计算 indicators 声明块,"
                             "请删除(所需数值若为内建序列,直接在 cond.ref 引用)"))

    # ---- signals
    legal = _legal_refs(voc, draft)
    sigs = tree.get("signals")
    if not isinstance(sigs, list) or not sigs:
        errors.append(_e(ErrCode.EMPTY_SIGNALS, "signals", "策略至少需要一个 signal"))
        return errors
    seen_ids: dict[str, int] = {}
    buy_idx: list[int] = []
    sell_idx: list[int] = []
    cond_index: dict[int, list[tuple[str, dict]]] = {}

    for i, sig in enumerate(sigs):
        sp = f"signals[{i}]"
        if not isinstance(sig, dict):
            errors.append(_e(ErrCode.SCHEMA_VIOLATION, sp, "signal 必须是对象"))
            continue
        sid = str(sig.get("id") or "")
        if not sid:
            errors.append(_e(ErrCode.SCHEMA_VIOLATION, path_join(sp, "id"), "缺 id"))
        elif sid in seen_ids:
            errors.append(_e(ErrCode.DUPLICATE_SIGNAL_ID, sp,
                             f"信号 id {sid!r} 重复(首现于 signals[{seen_ids[sid]}])"))
        else:
            seen_ids[sid] = i
        for k in sorted(set(sig) - _SIGNAL_KEYS):
            errors.append(_e(ErrCode.SCHEMA_VIOLATION, path_join(sp, k), f"未知信号键 {k!r}"))
        for k in sorted(set(sig) & set(voc.closed_signal_keys)):
            errors.append(_e(ErrCode.P0_SUBSET_VIOLATION, path_join(sp, k),
                             f"P0 封闭信号键 {k!r}(多层/状态语义)"))
        act = sig.get("action")
        gated = False
        leaves: list[tuple[str, dict]] = []
        conds = sig.get("conds")
        if isinstance(conds, list):
            _walk_nested(conds, sp, leaves)
        elif sig.get("type"):
            leaves.append((sp, sig))
        cond_index[i] = leaves
        for cp, cond in leaves:
            if isinstance(cond, dict) and cond.get("type") in HOLDING_GATES:
                gated = True
        # 动作
        if not isinstance(act, dict) or not act:
            errors.append(_e(ErrCode.SCHEMA_VIOLATION, path_join(sp, "action"),
                             "action 必须是对象({buy:…} 或 {sell:…})"))
        else:
            has_buy, has_sell = "buy" in act, "sell" in act
            if has_buy and has_sell:
                errors.append(_e(ErrCode.UNKNOWN_ACTION, path_join(sp, "action"),
                                 "buy/sell 不能同信号共存"))
            for k in sorted(set(act) & set(voc.closed_action_keys)):
                errors.append(_e(ErrCode.P0_SUBSET_VIOLATION, path_join(sp, "action", k),
                                 f"P0 封闭动作键 {k!r}"))
            if has_buy:
                if act["buy"] not in voc.buy_layers:
                    errors.append(_e(ErrCode.UNKNOWN_ACTION, path_join(sp, "action", "buy"),
                                     f"P0 单层规范形入场层只能 {list(voc.buy_layers)},"
                                     f"得到 {act['buy']!r}(持仓语义落 anchor 层)",
                                     candidates=list(voc.buy_layers)))
                pct = act.get("pct_of_full")
                if isinstance(pct, bool) or not isinstance(pct, (int, float)) or \
                        not (0 < float(pct) <= 100):
                    errors.append(_e(ErrCode.PARAM_INVALID, path_join(sp, "action", "pct_of_full"),
                                     "pct_of_full 须 ∈(0,100]"))
                buy_idx.append(i)
            elif has_sell:
                if act["sell"] not in voc.sell_actions:
                    errors.append(_e(ErrCode.UNKNOWN_ACTION, path_join(sp, "action", "sell"),
                                     f"卖出动词白名单 {list(voc.sell_actions)},得到 {act['sell']!r}",
                                     candidates=list(voc.sell_actions)))
                if not gated:
                    errors.append(_e(
                        ErrCode.EXIT_BEFORE_ENTRY, sp,
                        f"卖出信号 {sid!r} 无持仓门控条件({sorted(HOLDING_GATES)}),"
                        "空仓 bar 上会吞掉其后所有信号(evaluate_bar 单动作/bar)——"
                        "退出先于持仓成立",
                        candidates=[f"加 cond: {{type: layers_held, layer: {P0_LAYER}}}"]))
                sell_idx.append(i)
            else:
                errors.append(_e(ErrCode.UNKNOWN_ACTION, path_join(sp, "action"),
                                 "action 必须含 buy 或 sell",
                                 candidates=[f"buy: {P0_LAYER}", "sell: clear_anchor"]))

        # 条件递归
        _validate_conds(voc, legal, leaves, graph, errors)
        # 信号互引(days_since.since / after_signal.ref)先收集,后统一查
    # ---- 引用 id 存在性(需全表 id 集)
    for i, leaves in cond_index.items():
        for cp, cond in leaves:
            t = cond.get("type")
            if t == "days_since":
                s = str(cond.get("since") or "")
                if s and s not in seen_ids:
                    cands = difflib.get_close_matches(s, list(seen_ids), 3) \
                        or sorted(seen_ids)[:5]
                    errors.append(_e(ErrCode.SIGNAL_ID_REF_UNDEFINED, cp,
                                     f"days_since.since={s!r} 不是任何信号 id(现有 {sorted(seen_ids)})",
                                     candidates=cands))
            elif t == "after_signal":
                r = str(cond.get("ref") or "")
                if r and r not in seen_ids:
                    cands = difflib.get_close_matches(r, list(seen_ids), 3) \
                        or sorted(seen_ids)[:5]
                    errors.append(_e(ErrCode.SIGNAL_ID_REF_UNDEFINED, cp,
                                     f"after_signal.ref={r!r} 不是任何信号 id(现有 {sorted(seen_ids)})",
                                     candidates=cands))

    # ---- 无入场
    if not buy_idx:
        errors.append(_e(ErrCode.SCHEMA_VIOLATION, "signals",
                         "策略没有任何入场信号(buy)"))
    if errors:
        return errors

    # ---- 顺序语义:入场信号不得排在未门控退出之后?P0 规范:退出在前合法,
    #      但买入信号出现在任一"未持仓门控"卖出之前无意义——已由 EXIT_BEFORE_ENTRY 拦截。
    return errors


def _validate_conds(voc: Vocabulary, legal: dict[str, str],
                    leaves: list[tuple[str, dict]], graph: _RefGraph,
                    errors: list[StratError]) -> None:
    for cp, cond in leaves:
        t = str(cond.get("type") or "")
        card = voc.conds.get(t)
        if card is None:
            errors.append(_e(ErrCode.UNKNOWN_COND, cp,
                             f"条件类型 {t!r} 不在 DSL 注册表",
                             candidates=difflib.get_close_matches(t, voc.cond_names(), 3)))
            continue
        if not card.p0_open:
            errors.append(_e(ErrCode.P0_SUBSET_VIOLATION, cp, f"条件 {t} 在 P0 封闭"))
        if t in ("and", "or"):
            sub = cond.get("conds")
            if not isinstance(sub, list) or not sub:
                errors.append(_e(ErrCode.EMPTY_CONDS, cp, f"{t} 需要非空 conds 列表"))
            # 路径里每多一层 `.conds[` 深一层嵌套(signals[i].conds[j]=1)
            if cp.count(".conds[") >= MAX_COND_DEPTH:
                errors.append(_e(ErrCode.SCHEMA_VIOLATION, cp,
                                 f"and/or 嵌套超上限 {MAX_COND_DEPTH}"))
            continue   # 嵌套子条件已展平进 leaves
        for key in card.required:
            v = cond.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                errors.append(_e(ErrCode.PARAM_MISSING, cp,
                                 f"条件 {t} 缺必填参数 {key!r}(参数集 {card.required})",
                                 candidates=sorted(legal)[:8]))
        # ref 合法性 + related_paths(正向边:未定义 ref → 指向 series 声明处;
        # 引擎只算内建序列,引用声明块自定义名等同引用不存在)
        r = cond.get("ref")
        if isinstance(r, str) and r:
            if r not in legal:
                related = [f"series.{r}"] + [p for p in graph.ref_uses(r) if p != cp]
                errors.append(_e(ErrCode.REF_UNDEFINED, cp,
                                 f"cond.ref={r!r} 非内建序列(引擎不计算声明块自定义"
                                 f"名;若为拼写错请改内建名)",
                                 candidates=difflib.get_close_matches(
                                     r, sorted(legal), 4) or sorted(voc.series)[:6],
                                 related=sorted(set(related))))
        # 参数类型
        for key in card.required + list(card.optional):
            v = cond.get(key)
            if v is None:
                continue
            if key in ("threshold", "multiplier", "ge", "le", "days", "pct", "j_max", "n"):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    errors.append(_e(ErrCode.PARAM_INVALID, path_join(cp, key),
                                     f"{t}.{key} 须为数字,得到 {v!r}"))
        # layers 参数:仅 P0_LAYER
        for lk in ("layer", "layers"):
            if t.startswith("layers_") and lk in cond:
                val = cond[lk]
                names = val if isinstance(val, list) else [val]
                for n in names:
                    if n != P0_LAYER:
                        errors.append(_e(ErrCode.P0_SUBSET_VIOLATION, path_join(cp, lk),
                                         f"P0 单层规范形 layer 只能 {P0_LAYER!r},得到 {n!r}"))


# ================================================================ 回测真跑 + JUDGE

def _years_from_equity(equity: list[dict]) -> float:
    dates = [str(e.get("date", ""))[:10] for e in equity if e.get("date")]
    if len(dates) < 2:
        return 0.0
    from datetime import date
    def _d(s: str) -> date:
        y, m, dd = (int(x) for x in s.split("-"))
        return date(y, m, dd)
    try:
        days = abs((_d(dates[-1]) - _d(dates[0])).days)
    except Exception:  # noqa: BLE001
        return 0.0
    return days / 365.25


_HIGH_FREQ_WORDS = ("高频", "日内", "短线", "每天", "频繁", "快进快出", "T+0")
_LONG_WORDS = ("拿住", "长期", "长线", "波段", "不动", "趋势", "慢慢")


def judge(result: dict, spec: IntentSpec) -> list[StratError]:
    """判读分级(§4.2):硬伤→打回修复;可疑→ASK_USER(判断权在用户)。"""
    errors: list[StratError] = []
    trades = result.get("trades") or []
    equity = result.get("equity") or []
    if not trades:
        errors.append(_e(ErrCode.NO_TRADES, "signals",
                         "回测零成交:条件可能永不同时成立或门控死锁"))
        return errors
    eq = [float(e["equity"]) for e in equity if "equity" in e]
    if eq and max(eq) - min(eq) < 1e-9:
        errors.append(_e(ErrCode.FLAT_EQUITY, "signals", "净值全程无波动(持仓从未发生?)"))
    years = max(_years_from_equity(equity), 1 / 365.25)
    tpy = len(trades) / years
    user_text = " ".join([f.text for f in spec.fragments] + list(spec.notes))
    hf = any(w in user_text for w in _HIGH_FREQ_WORDS)
    lg = any(w in user_text for w in _LONG_WORDS)
    if tpy > 250 and not hf:
        errors.append(_e(ErrCode.SUSPECT_TURNOVER, "signals",
                         f"年成交 {tpy:.0f} 笔,与用户描述无高频含义可能不符"))
    elif lg and tpy > 52 and not hf:
        errors.append(_e(ErrCode.SUSPECT_FREQ_MISMATCH, "signals",
                         f"年成交 {tpy:.0f} 笔,与'长期/波段'类描述时间尺度不符"))
    return errors


def run_backtest_gate(draft: Draft, spec: IntentSpec, *,
                      cache_dir: Path | None = None) -> tuple[dict | None, list[StratError]]:
    """确定性回测关卡:行情覆盖 → 真跑 → judge。返回 (结果或 None, errors)。

    ENGINE_ERROR 的 where 尽量解析到 signals[i](StrategyError 消息含信号 id/类型时)。
    """
    errors = check_data_gate(spec, cache_dir=cache_dir)
    if errors:
        return None, errors
    from backtest_engine.main import run_backtest_layered
    tmp = _write_tmp_yaml(draft, spec)
    try:
        result = run_backtest_layered(spec.symbol, str(tmp), spec.start, spec.end)
    except Exception as exc:  # noqa: BLE001 —— 引擎显式失败也要喂修复,不崩外层
        return None, [_e(ErrCode.ENGINE_ERROR, _where_from_engine_msg(str(exc), draft),
                         f"引擎报错:{type(exc).__name__}: {str(exc)[:300]}")]
    return result, judge(result, spec)


def check_data_gate(spec: IntentSpec, *, cache_dir: Path | None = None) -> list[StratError]:
    csv = _cache_dir(cache_dir) / f"{spec.symbol}_daily.csv"
    if not csv.exists():
        return [_e(ErrCode.DATA_MISSING, "",
                   f"行情缓存缺失:{csv.name}(先跑抓取脚本)")]
    dates = _csv_dates(csv)
    if not dates:
        return [_e(ErrCode.DATA_MISSING, "", f"{csv.name} 无有效行")]
    lo, hi = dates[0], dates[-1]
    if spec.start and spec.end and spec.start > spec.end:
        return [_e(ErrCode.WINDOW_INVALID, "", f"start({spec.start})>end({spec.end})")]
    if spec.start and spec.start > hi:
        return [_e(ErrCode.WINDOW_INVALID, "",
                   f"区间起点 {spec.start} 超出数据覆盖({lo}~{hi})")]
    if spec.end and spec.end < lo:
        return [_e(ErrCode.WINDOW_INVALID, "",
                   f"区间终点 {spec.end} 早于数据起点({lo}~{hi})")]
    return []


def check_data(symbol: str, start: str = "", end: str = "") -> dict:
    """修复 agent 只读工具(§4.2 动作表):行情覆盖核验。"""
    csv = _cache_dir(None) / f"{symbol}_daily.csv"
    if not csv.exists():
        return {"ok": False, "error": ErrCode.DATA_MISSING,
                "msg": f"缓存缺失 {symbol}_daily.csv"}
    dates = _csv_dates(csv)
    if not dates:
        return {"ok": False, "error": ErrCode.DATA_MISSING, "msg": "CSV 无有效行"}
    lo, hi = dates[0], dates[-1]
    in_window = [d for d in dates if (not start or d >= start) and (not end or d <= end)]
    return {"ok": True, "symbol": symbol, "first": lo, "last": hi, "bars": len(dates),
            "window_bars": len(in_window),
            "coverage_pct": round(len(in_window) / max(len(dates), 1) * 100, 1)}


def dsl_card(query: str) -> str:
    """修复 agent 只读工具:词汇卡检索(唯一事实源 = 注册表 AST 派生)。"""
    return get_vocabulary().search(query)


def _cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    from backtest_engine.main import CACHE_DIR
    return Path(CACHE_DIR)


def _csv_dates(csv: Path) -> list[str]:
    dates: list[str] = []
    with open(csv, encoding="utf-8", errors="replace") as f:
        header = f.readline().strip().split(",")
        try:
            di = next(i for i, h in enumerate(header)
                      if h.lower() in ("trade_date", "date"))
        except StopIteration:
            return []
        for line in f:
            parts = line.strip().split(",")
            if len(parts) > di and re.match(r"^\d{4}-\d{2}-\d{2}", parts[di]):
                dates.append(parts[di][:10])
    return sorted(dates)


def _write_tmp_yaml(draft: Draft, spec: IntentSpec) -> Path:
    from alphaprism.paths import DATA_DIR
    tmp_dir = DATA_DIR / "nl2strat" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    p = tmp_dir / f"_run_{spec.run_id or 'x'}.yaml"
    p.write_text(draft.to_yaml_text(), encoding="utf-8")
    return p


_ENGINE_SIG_RE = re.compile(r"signals?\s*[\[(](\d+)[\])]")


def _where_from_engine_msg(msg: str, draft: Draft) -> str:
    m = _ENGINE_SIG_RE.search(msg)
    if m and int(m.group(1)) < len(draft.signals()):
        return f"signals[{m.group(1)}]"
    for kw in ("未知条件类型", "策略缺少 signals"):
        if kw in msg:
            return "signals"
    return ""


# ================================================================ 片段工具(供 plan/draft 装配)

def fragment_index(spec: IntentSpec) -> dict[str, Fragment]:
    return {f.fragment_id: f for f in spec.semantic_fragments()}


def assert_path_valid(path: str) -> None:
    """供工具入口做路径卫生(非法即 ValueError——由 repair 转为工具错误观察)。"""
    normalize_path(path)
