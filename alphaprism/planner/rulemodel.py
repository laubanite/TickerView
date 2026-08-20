"""RuleModel:作战地图 → 机器可核对的结构化契约(产品方案 §4.1)。

解析器读、生成器(§5.7)写,同一个模型。约束:
- levels 每个数字必须带溯源注释(红线溯源原则)
- rules 条件必须是可计算表达式;不可计算的进 alerts 不进 rules
- 解析失败/有歧义 → 进 unresolved(待人工确认),绝不猜测
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

SCHEMA_VERSION = "0.1.0"


@dataclass
class Meta:
    doc_date: str = ""                      # 文档日期
    version: str = ""                       # 版本
    data_source: str = ""                   # 数据来源
    purpose: str = ""                       # 用途
    principles: list[str] = field(default_factory=list)  # 适用原则
    source_file: str = ""                   # 源文件路径
    raw: str = ""                           # 文档头原文


@dataclass
class MaValue:
    period: str                             # "MA20"
    value: float
    raw: str = ""


@dataclass
class Level:
    """关键价位。name 取枚举值,price 必须可溯源(source 标注日期低点/均线等)。"""
    name: str                               # 买区下沿/买区上沿/突破点/减仓红线/生命线/止损价/目标位1/2/3
    price: float
    source: str = ""                        # 溯源注释,如 "8/14低" / "MA20"
    raw: str = ""


@dataclass
class GateRule:
    """大盘门控规则:{条件, 命中动作}。"""
    condition: str
    action: str
    raw: str = ""


@dataclass
class MarketGate:
    """§一 大盘环境 → 组合层门控。"""
    instrument: str = "上证指数"
    code: str = ""
    close: float | None = None
    change_pct: float | None = None
    close_note: str = ""                    # 收盘解读(平量光头阳线等)
    ma: list[MaValue] = field(default_factory=list)
    macd: str = ""
    kdj: dict[str, float] = field(default_factory=dict)  # {K,D,J}
    resistance: list[Level] = field(default_factory=list)
    support: list[Level] = field(default_factory=list)
    conclusion: str = ""                    # 环境结论
    rules: list[GateRule] = field(default_factory=list)
    raw: str = ""


@dataclass
class BudgetItem:
    """弹药预算分配:标的 → 预算占比。"""
    instrument: str = ""                    # 名称或代码
    code: str = ""
    priority: int | None = None
    note: str = ""                          # 定位/理由
    budget_pct: float | None = None
    raw: str = ""


@dataclass
class Global:
    """组合层:大盘门控 + 弹药预算 + 通用纪律 + 风险。"""
    market_gate: MarketGate = field(default_factory=MarketGate)
    budget_total: float | None = None
    budget_items: list[BudgetItem] = field(default_factory=list)
    discipline: list[str] = field(default_factory=list)   # §六 通用纪律
    risks: list[str] = field(default_factory=list)        # §七 风险提示
    raw: str = ""


@dataclass
class Position:
    """§二 持仓表(只展示,不做决策)。"""
    shares: int | None = None
    cost: float | None = None
    price: float | None = None
    market_value: float | None = None
    loss_pct: float | None = None
    loss_amount: float | None = None
    recover_need_pct: float | None = None   # 解套需涨
    raw: str = ""


@dataclass
class Rule:
    """触发规则(操作建议表一行)。条件必须是可计算表达式。"""
    action: str                             # 加仓①/加仓②/目标位减仓/红线减仓/新仓止损/清仓红线/换仓...
    condition: str
    operation: str                          # 具体操作
    computable: bool = True                 # 条件是否可计算(不可计算进 alerts 不进 rules)
    raw: str = ""


@dataclass
class Alert:
    """非数值预警/催化/风险,只入快照不做核对。"""
    kind: str                               # 预警 | 催化 | 风险
    content: str
    level: str = "info"                     # 🔴 🟢 🟡 ⚪ / info
    raw: str = ""


@dataclass
class Instrument:
    """标的层。"""
    code: str
    name: str
    type: str = ""                          # 波段仓 | 解套仓
    structure: str = ""                     # 上升 | 震荡 | 下降
    position: Position | None = None
    levels: list[Level] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    analysis: dict[str, str] = field(default_factory=dict)  # 今日盘面/技术指标/基本面/消息面
    raw: str = ""


@dataclass
class PlaybookRow:
    """剧本修正表一行(盘前生成,当日覆盖基准)。"""
    instrument: str = ""
    code: str = ""
    overnight: str = ""                     # 隔夜影响
    action: str = ""                        # 今日动作
    raw: str = ""


@dataclass
class JournalEntry:
    """每日盯盘记录(按日期追加,不覆盖历史)。"""
    date: str
    title: str = ""
    kind: str = "其他"                      # 盘前 | 盘中 | 盘后 | 复盘 | 待办 | 其他
    content: str = ""
    raw: str = ""


@dataclass
class Daily:
    """日级层:剧本修正表 + 盯盘记录。"""
    playbook: list[PlaybookRow] = field(default_factory=list)
    journal: list[JournalEntry] = field(default_factory=list)


@dataclass
class RuleModel:
    """整份作战地图的结构化模型。解析器读、生成器写,同一契约。"""
    schema_version: str = SCHEMA_VERSION
    meta: Meta = field(default_factory=Meta)
    global_: Global = field(default_factory=Global)
    instruments: list[Instrument] = field(default_factory=list)
    daily: Daily = field(default_factory=Daily)
    unresolved: list[str] = field(default_factory=list)   # 待人工确认队列(绝不猜测)
    warnings: list[str] = field(default_factory=list)     # 解析告警(可继续,但需知悉)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def instrument_by_code(self, code: str) -> Instrument | None:
        return next((i for i in self.instruments if i.code == code), None)

    def instrument_by_name(self, name: str) -> Instrument | None:
        return next((i for i in self.instruments if i.name == name), None)

    def summary(self) -> dict[str, Any]:
        """解析摘要(CLI 展示用)。"""
        inst = [
            {
                "code": i.code,
                "name": i.name,
                "type": i.type,
                "levels": [f"{l.name}={l.price}" for l in i.levels],
                "rules": [r.action for r in i.rules],
                "alerts": [a.content[:20] for a in i.alerts],
            }
            for i in self.instruments
        ]
        return {
            "schema_version": self.schema_version,
            "doc_date": self.meta.doc_date,
            "instruments": inst,
            "market_gate": {
                "close": self.global_.market_gate.close,
                "conclusion": self.global_.market_gate.conclusion,
                "rules": [g.action for g in self.global_.market_gate.rules],
            },
            "budget_items": [b.instrument for b in self.global_.budget_items],
            "playbook_rows": [p.instrument for p in self.daily.playbook],
            "journal_entries": len(self.daily.journal),
            "unresolved": len(self.unresolved),
            "warnings": len(self.warnings),
        }