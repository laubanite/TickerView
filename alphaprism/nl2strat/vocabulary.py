# -*- coding: utf-8 -*-
"""DSL 词汇卡:从引擎注册表自动生成(§8 冻结项③,单一事实源防文档漂移)。

- 条件参数需求 = **AST 解析 strategy_v2._CONDS 各 handler 源码**:cond["x"]→必填,
  cond.get("x")/`"x" in cond`→选填。_CONDS 新增条件,卡片自动同步(缺说明只降级,
  不缺参数——参数永远与代码一致);
- 内建序列 = 解析 main._series_table 的 DataFrame 列名字面量;指标 = indicator_calc
  公开函数签名;两轨同源;
- P0 子集边界(§3 关键决策 4):开放单标的单层(=anchor 规范形,见 contracts
  P0_CANONICAL_META)、入场/出场/止损/止盈/时间退出、显式参数;封闭多层加仓/
  scoring/sizing/halt_add/shared_cap/组合池——以 p0 标记 + 动作白名单表达;
- 不可表达清单(§3 关键决策 1):understand 的拒绝路径查表,不静默近似。
"""
from __future__ import annotations

import ast
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from backtest_engine import main as _engine_main
from backtest_engine.core import strategy_v2 as _sv2

# ---------------------------------------------------------------- 语义说明(人写部分)
# 只为"自动参数"补充直觉;缺条目不报错(降级为无说明),保证注册表扩展零阻塞。

_SEMANTICS: dict[str, str] = {
    "cross_above_series": "收盘上穿指定序列(严格大于;字段 source 被引擎忽略,恒比较收盘价)",
    "close_above_series": "收盘在指定序列上方(同 cross_above_series 语义)",
    "close_below_series": "收盘在指定序列下方(严格小于)",
    "adx_above": "趋势强度 ADX(序列 ref,如 adx14)大于 threshold",
    "vol_ratio_above": "放量:当日量/ref 均值 ≥ threshold 倍",
    "vol_ratio_below": "缩量:当日量/ref 均值 ≤ threshold 倍",
    "close_above_ma": "收盘高于均线(ref=某 MA 序列)",
    "close_below_ma": "收盘低于均线(ref=某 MA 序列)",
    "close_below_rolling": "收盘跌破滚动最低序列(ref 需为 rolling_min 序列,如 low20_close)",
    "kdj_golden_cross": "KDJ 低位金叉:K 上穿 D 且 J<j_max(默认 30);需序列 kdj_k/d/j 组",
    "days_since": "距信号 since 触发已过 [ge,le] 个交易日(从未触发→恒 False,防绕过入场)",
    "after_signal": "信号 ref 已触发过(布尔门槛,用于串联入场序列)",
    "layers_held": "指定层有持仓(单层规范形下 layer 只能 anchor)",
    "layers_any": "任一指定层有持仓(layers 选填,默认取 layer)",
    "layers_empty": "指定层全部为空(空仓门控:入场信号防重复建仓)",
    "j_below": "KDJ J 值低于 threshold(超卖)",
    "pct_drop": "单日跌幅 ≥ threshold%(收盘 vs 前收)",
    "consecutive_down": "连续 n 根阴线(收盘<前收)",
    "drawdown_above": "自序列 ref 峰值回撤 ≥ threshold%(ref 常为 peak120)",
    "close_below_cost": "收盘 ≤ 持仓成本×multiplier(默认 0.92;成本=anchor 层加权成本,"
                        "P0 单层规范形下即持仓成本,止损原子条件)",
    "hold_days_ge": "已持有 ≥ days 个交易日(时间退出)",
    "recovery_pct": "回撤修复止盈:收盘 ≥ 峰值−pct%×(峰值−入场收盘)(均值回归)",
    "ma_spread_below": "均线粘合:M5/M10/M20 极差/现价 < threshold%(默认 0.5)",
    "panic_drop": "恐慌跌幅:单日跌幅 ≥ max(1.5×ATR20%,1.5%)",
    "pos_ok": "位置到位:现价 ≥ 近250日低且距其 ≤ max(1.5%,1.5×ATR20%)",
    "and": "子条件全部成立(conds 列表;组合语义的与)",
    "or": "子条件任一成立(conds 列表;组合语义的或)",
}

#: P0 封闭条件(语义依赖多层/盘中状态,注册表有但 agent 不开放;越用→P0_SUBSET_VIOLATION)
_P0_CLOSED_CONDS: set[str] = set()

#: 参数值域提示(修复 agent 与校验器共用)
_PARAM_HINTS: dict[tuple[str, str], str] = {
    ("close_below_cost", "multiplier"): "(0,1] 如 0.92=跌破成本8%",
    ("recovery_pct", "pct"): "(0,100]",
    ("drawdown_above", "threshold"): ">0 百分数",
    ("vol_ratio_above", "threshold"): ">1 通常 1.5~3",
    ("vol_ratio_below", "threshold"): "(0,1] 通常 0.6~1.0",
    ("days_since", "ge"): "≥0 交易日",
    ("pct_drop", "threshold"): ">0 百分数",
}

# ---------------------------------------------------------------- 卡片模型


@dataclass
class CondCard:
    name: str
    required: list[str] = field(default_factory=list)
    optional: dict[str, Any] = field(default_factory=dict)   # name → default(字面量可解析时)
    semantics: str = ""
    takes_ref: bool = False               # 含 ref/series 引用参数
    p0_open: bool = True
    params_hint: dict[str, str] = field(default_factory=dict)

    def card_line(self) -> str:
        req = ",".join(self.required)
        opt = ",".join(f"{k}?={v}" for k, v in self.optional.items())
        args = req + (f";[{opt}]" if opt else "")
        return f"{self.name}({args}): {self.semantics}"


@dataclass
class SeriesCard:
    name: str
    definition: str = ""
    p0_open: bool = True


@dataclass
class IndicatorCard:
    name: str                      # YAML indicators[].type 口径(sma/ema/adx/...)
    params: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class UnsupportedItem:
    term: str                      # 用户可能的说法(检索词)
    reason: str
    nearest: str = ""


#: 内建序列/运行期注入的中文口径对照(唯一事实源;semantic_verify 与漂移筛查共用)。
#: 用户说法命中任一别名 → 对应标准符号即正确翻译(非"对象被换")。
SERIES_CN: dict[str, tuple[str, ...]] = {
    "prev20_high": ("前20日高点", "20日高点", "20日新高", "前高", "近20日最高"),
    "prev60_high": ("前60日高点", "60日高点", "60日新高"),
    "m_20": ("20日均线", "20日移动平均", "月线", "MA20"),
    "m_5": ("5日均线", "周线", "MA5"),
    "m_10": ("10日均线", "MA10"),
    "m_60": ("60日均线", "季线", "MA60"),
    "vol20": ("20日均量", "20日平均成交量", "量均"),
    "vol5": ("5日均量", "5日平均成交量"),
    "atr20_pct": ("20日真实波幅", "ATR"),
    "kdj_k": ("KDJ的K值", "K值", "随机指标K"),
    "kdj_d": ("KDJ的D值", "D值"),
    "adx14": ("14日ADX", "ADX", "趋势强度"),
    "anchor_cost": ("持仓成本", "成本价", "成本"),
    "entry_break_level": ("入场突破位", "突破点"),
    "close": ("收盘", "收盘价"),
    "open": ("开盘", "开盘价"),
    "high": ("最高", "最高价"),
    "low": ("最低", "最低价"),
    "volume": ("成交量", "量"),
}


@dataclass
class Vocabulary:
    conds: dict[str, CondCard] = field(default_factory=dict)
    series: dict[str, SeriesCard] = field(default_factory=dict)
    indicators: dict[str, IndicatorCard] = field(default_factory=dict)
    #: P0 动作白名单(装配规范形;校验器据此判 UNKNOWN_ACTION/P0_SUBSET_VIOLATION)
    buy_layers: tuple[str, ...] = ("anchor",)
    sell_actions: tuple[str, ...] = ("clear_anchor", "clear_all")
    #: 信号节点上 P0 封闭的键(多层加仓/状态语义)
    closed_signal_keys: tuple[str, ...] = ("halt_add", "record_break_level", "layers",
                                           "after_signal")
    #: action 节点上 P0 封闭的键(多层配额)
    closed_action_keys: tuple[str, ...] = ("shared_cap", "cap_override")
    unsupported: list[UnsupportedItem] = field(default_factory=list)

    def ref_values(self) -> set[str]:
        """可被 cond.ref 引用的全部合法名(内建序列;自定义声明由校验器并入)。"""
        return set(self.series)

    def cn_glossary(self) -> str:
        """对照表渲染(喂 semantic_verify / draft:符号=中文,防把符号当错译)。"""
        return "\n".join(f"  {sym} = {'/'.join(als)}" for sym, als in SERIES_CN.items())

    def symbols_in_text(self, text: str) -> set[str]:
        """用户文本命中的标准符号集合(某别名出现 → 该符号即正确译法)。"""
        out: set[str] = set()
        t = text or ""
        for sym, aliases in SERIES_CN.items():
            if any(a in t for a in aliases):
                out.add(sym)
        return out

    def symbol_correct_here(self, symbol: str, user_text: str) -> bool:
        """判定某 drift 是否伪报:被指为"错译"的 symbol 恰是 user_text 的标准译法。

        symbol 是内建名,且其任一中文别名出现在用户原话里 → 这就是正确翻译,
        不该算 object 漂移(小模型常把中英符号差异误当对象错换)。
        """
        sym = (symbol or "").strip()
        aliases = SERIES_CN.get(sym)
        if not aliases:
            return False
        t = user_text or ""
        return any(a in t for a in aliases)

    def open_cond_names(self) -> list[str]:
        return sorted(k for k, c in self.conds.items() if c.p0_open)

    def search(self, query: str, limit: int = 12) -> str:
        """dsl_card 工具检索面:按条件名/参数名/语义词给紧凑卡片。"""
        q = (query or "").strip().lower()
        hits: list[CondCard] = []
        for name, card in self.conds.items():
            if not card.p0_open:
                continue
            hay = f"{name} {card.semantics} {' '.join(card.required)} " \
                  f"{' '.join(card.optional)} {card.params_hint}"
            if q and (q in name.lower() or q in hay):
                hits.append(card)
        hits.sort(key=lambda c: (q not in c.name.lower(), c.name))
        lines = [c.card_line() for c in hits[:limit]]
        if not lines:
            lines = [f"(检索 {query!r} 无命中;条件全集见 cond_names;勿臆造条件类型)"]
        return "\n".join(lines)

    def cond_names(self) -> list[str]:
        return self.open_cond_names()

    def to_dict(self) -> dict:
        return {
            "schema_version": "1",
            "conds": {k: vars(v) for k, v in self.conds.items()},
            "series": sorted(self.series),
            "indicators": sorted(self.indicators),
            "buy_layers": list(self.buy_layers),
            "sell_actions": list(self.sell_actions),
            "unsupported": [vars(u) for u in self.unsupported],
        }


# ---------------------------------------------------------------- 自动派生


def _ast_keys(fn: Callable) -> tuple[list[str], list[str]]:
    """AST 解析 handler:cond["k"]→必填;cond.get("k")/"k" in cond→选填。"""
    src = inspect.getsource(fn)
    tree = ast.parse(src)
    req: list[str] = []
    opt: list[str] = []

    _Index = getattr(ast, "Index", None)       # <3.12 的 Index 包装(3.12 已移除)

    def _const(node) -> str | None:
        if _Index is not None and isinstance(node, _Index):
            node = node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == "cond":
            key = _const(node.slice)
            if key and key not in req:
                req.append(key)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "cond":
            key = _const(node.args[0]) if node.args else None
            if key and key not in opt:
                opt.append(key)
        elif isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.In) \
                and isinstance(node.comparators[0], ast.Name) \
                and node.comparators[0].id == "cond":
            key = _const(node.left)
            if key and key not in opt:
                opt.append(key)
    # `"le" in cond and cond["le"]…` 模式:有成员测试守卫的键是选填(即便也以下标读)
    req = [k for k in req if k not in opt]
    return req, opt


def _get_defaults(fn: Callable) -> dict[str, Any]:
    """cond.get("k", <literal>) 的默认值(仅字面量可静态取)。"""
    out: dict[str, Any] = {}
    try:
        tree = ast.parse(inspect.getsource(fn))
    except (OSError, SyntaxError, TypeError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "cond" and len(node.args) >= 2:
            key = node.args[0]
            key = key.value if isinstance(key, ast.Constant) else None
            val = node.args[1]
            if isinstance(key, str) and isinstance(val, ast.Constant):
                out[key] = val.value
    return out


def _series_names() -> list[str]:
    """解析 main._series_table 源码中 DataFrame({...}) 的键字面量。"""
    src = inspect.getsource(_engine_main._series_table)
    names: list[str] = []
    for m in re.finditer(r'"([A-Za-z_0-9]+)":', src):
        names.append(m.group(1))
    # f-string 动态列(prev_close{k} 类)由 handler 参数保证;仅收字面量
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _indicator_cards() -> dict[str, IndicatorCard]:
    from backtest_engine.core import indicator_calc as ic
    out: dict[str, IndicatorCard] = {}
    for fname, note in (("sma", "简单均线"), ("ema", "指数均线"),
                        ("atr", "平均真实波幅"), ("adx", "趋势强度"),
                        ("macd", "MACD(dif/dea/hist)"),
                        ("stoch_kd", "KDJ 之 K/D(strategy_v2 金叉用)"),
                        ("stoch_kdj", "KDJ 之 J")):
        fn = getattr(ic, fname, None)
        if fn is None:
            continue
        params = [p for p in inspect.signature(fn).parameters
                  if p not in ("high", "low", "close", "s")]
        out[fname] = IndicatorCard(name=fname, params=params, note=note)
    return out


#: 不可表达清单(§3 关键决策 1;understand 拒绝路径 + dsl_card 反查)
_UNSUPPORTED: list[UnsupportedItem] = [
    UnsupportedItem("基本面/财务因子(ROE、市盈率、财报)",
                    "引擎仅行情技术面(价量+技术指标),无财务数据通道",
                    "同类技术面替代:均线趋势/波动率过滤"),
    UnsupportedItem("盘中分钟级择时",
                    "回测引擎为日线口径(次开盘撮合)",
                    "日线近似:收盘判定+次日开盘执行"),
    UnsupportedItem("期货/杠杆做空",
                    "撮合仅 A 股 ETF/股票多头(T+1/T+0 现货规则)",
                    "只能表达'清仓离场',无做空腿"),
    UnsupportedItem("多标的组合轮动/截面选股",
                    "P0 单标的单层;run_portfolio 属产品自有语义(方案 §7#4)",
                    "先做单标的,组合轮动留二期"),
    UnsupportedItem("期权/可转债策略", "无对应标的与撮合模型", "—"),
    UnsupportedItem("网格/马丁格尔多层加仓",
                    "P0 封闭 scoring/sizing/多层加仓(方案 §3 决策 4)",
                    "单次进出+止损止盈的等价描述"),
    UnsupportedItem("预测未来涨跌/保证收益", "回测只能做样本内统计",
                    "给出可检验的条件规则"),
]


def build_vocabulary() -> Vocabulary:
    """从注册表构建词汇卡(每次进程内构建一次即可,无 IO)。"""
    conds: dict[str, CondCard] = {}
    for name, fn in _sv2._CONDS.items():       # noqa: SLF001 - 单一事实源就是要读它
        req, opt = _ast_keys(fn)
        # and/or 的 conds 是子树列表,不算 ref
        card = CondCard(
            name=name,
            required=[k for k in req if k != "type"],
            optional={k: _get_defaults(fn).get(k, "…") for k in opt},
            semantics=_SEMANTICS.get(name, "(新注册条件,暂无说明)"),
            takes_ref=("ref" in req or "ref" in opt),
            p0_open=name not in _P0_CLOSED_CONDS,
            params_hint={k: v for (cn, k), v in _PARAM_HINTS.items() if cn == name},
        )
        conds[name] = card
    series = {n: SeriesCard(name=n, definition=f"内建序列 {n}(main._series_table 预计算)")
              for n in _series_names()}
    return Vocabulary(conds=conds, series=series, indicators=_indicator_cards(),
                      unsupported=list(_UNSUPPORTED))


_CACHE: Vocabulary | None = None


def get_vocabulary() -> Vocabulary:
    global _CACHE
    if _CACHE is None:
        _CACHE = build_vocabulary()
    return _CACHE


#: 组合逻辑的递归校验深度上限(嵌套 and/or;词汇表契约,不是 prompt 约定)
MAX_COND_DEPTH = 3
