"""作战地图 → RuleModel 解析器(产品方案 §4.1,里程碑1)。

把自然语言+表格的 .md 作战地图解析成结构化 RuleModel。
设计约束:
  ① levels 每个数字必须带溯源注释(红线溯源原则)
  ② rules 条件必须是可计算表达式;不可计算(如"情绪面")进 alerts 不进 rules
  ③ 解析失败/有歧义 → 进 unresolved(待人工确认),绝不猜测
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .rulemodel import (
    Alert, BudgetItem, GateRule, Global, Instrument, JournalEntry,
    Level, MaValue, MarketGate, Meta, Position, PlaybookRow, Rule, RuleModel,
)

# ---------------------------------------------------------------- 常量

# 章节标题 → 逻辑段
SECTION_PATTERNS = [
    (r"^#+\s*一[、.．]?\s*(大盘环境|大盘)", "market"),
    (r"^#+\s*二[、.．]?\s*(一览|持仓|ETF 一览|七只)", "positions"),
    (r"^#+\s*三[、.．]?\s*逐只详细分析", "instruments"),
    (r"^#+\s*四[、.．]?\s*(基本面|消息面.*预警|预警速查)", "alerts"),
    (r"^#+\s*五[、.．]?\s*(操作优先级|资金分配|加仓预算)", "budget"),
    (r"^#+\s*六[、.．]?\s*通用纪律", "discipline"),
    (r"^#+\s*七[、.．]?\s*风险提示", "risks"),
    (r"^#+\s*八[、.．]?\s*(复盘记录|触发信号速查|速查)", "cheatsheet"),
    (r"^#+\s*(每日盯盘记录|盯盘记录)", "journal"),
]

CODE_RE = re.compile(r"(\d{6})(?:\.(SH|SZ))?")


# ---------------------------------------------------------------- 表格工具

def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|")


def _is_separator_row(line: str) -> bool:
    s = line.strip().strip("|")
    return bool(s) and all(re.fullmatch(r":?-{2,}:?", p.strip()) for p in s.split("|"))


def _is_heading(line: str) -> bool:
    return bool(re.match(r"^#{1,6}\s+", line.strip()))


def _is_code_block(line: str) -> bool:
    return line.strip().startswith("```")


def _first_number(text: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def _first_percent(text: str) -> float | None:
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def _extract_codes(text: str) -> list[str]:
    return [m.group(1) for m in CODE_RE.finditer(text)]


def _norm_code(code: str) -> str:
    m = CODE_RE.match(code.strip())
    return m.group(1) if m else code.strip()


def _col_idx(cols: dict[str, int], substr: str) -> int | None:
    """按子串找列索引(表头可能带"（数据来源）"等后缀)。找不到返回 None。"""
    for name, idx in cols.items():
        if substr in name:
            return idx
    return None


def _range_of(text: str) -> tuple[float | None, float | None]:
    """从"0.855-0.860"或"0.855~0.860"提取区间 (下沿, 上沿)。非区间返回 (None, None)。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~～至]\s*(\d+(?:\.\d+)?)", text)
    if not m:
        return None, None
    lo, hi = float(m.group(1)), float(m.group(2))
    return (lo, hi) if lo <= hi else (hi, lo)


def _extract_one_level(seg: str) -> tuple[float | None, str]:
    """从一段文本提取一个价位 + 溯源。返回 (price, source),提取不到返回 (None, "")。

    优先级:
    1. "=1.012" / "≈1.012"(数据来源注释里的实际值,如 "8/19=1.012")
    2. "1.012(溯源)" 括号紧跟数字
    3. 第一个独立小数(跳过 MA 周期数字、日期数字)
    """
    # 1) 带 = / ≈ 的实际值(排除 "8/19=1.012" 里的 8/19,取 = 后的值)
    m = re.search(r"[=≈](\d+(?:\.\d+)?)", seg)
    if m:
        return float(m.group(1)), ""
    # 2) 数字(溯源)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[（(]([^）)]*)[）)]", seg)
    if m:
        return float(m.group(1)), m.group(2)
    # 3) 第一个独立小数:排除 MA 周期(MA20)和日期(8/19)
    for m in re.finditer(r"(\d+(?:\.\d+)?)", seg):
        num = float(m.group(1))
        before = seg[max(0, m.start() - 3):m.start()]
        # 跳过 "MA20" 的周期数字
        if re.search(r"MA\s*$", before):
            continue
        # 跳过日期数字(如 "8/19" 的 8)
        if re.search(r"\d/\s*$", before):
            continue
        return num, ""
    return None, ""


def _section_of(line: str) -> str | None:
    if not _is_heading(line):
        return None
    for pat, name in SECTION_PATTERNS:
        if re.search(pat, line):
            return name
    return None


# ---------------------------------------------------------------- 解析器主体

class BattleMapParser:
    """把作战地图 .md 解析为 RuleModel。

    parse() 返回 RuleModel。解析过程不抛异常:任何无法确定的内容进
    unresolved/warnings,绝不猜测(设计约束③)。
    """

    def __init__(self, text: str | None = None, path: str | Path | None = None) -> None:
        if text is None:
            if path is None:
                raise ValueError("BattleMapParser 需要 text 或 path 之一")
            text = Path(path).read_text(encoding="utf-8")
        self.text = text
        self.path = str(path) if path else ""
        self.model = RuleModel()
        self.model.meta.source_file = self.path
        self._lines = text.splitlines()

    # -- 主流程 ---------------------------------------------------------

    def parse(self) -> RuleModel:
        sections = self._segment()
        self._parse_meta(sections.get("header", []))
        self._parse_market(sections.get("market", []))
        self._parse_positions(sections.get("positions", []))
        self._parse_alerts_section(sections.get("alerts", []))
        self._parse_budget(sections.get("budget", []))
        self._parse_discipline(sections.get("discipline", []))
        self._parse_risks(sections.get("risks", []))
        self._parse_cheatsheet(sections.get("cheatsheet", []))
        self._parse_journal(sections.get("journal", []))
        self._parse_instruments(sections.get("instruments", []))
        return self.model

    def _segment(self) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current = "header"
        in_code = False
        for line in self._lines:
            if _is_code_block(line):
                in_code = not in_code
                continue
            if not in_code:
                sec = _section_of(line)
                if sec is not None:
                    current = sec
                    sections.setdefault(current, [])
                    continue
            sections.setdefault(current, []).append(line)
        return sections

    def _table_blocks(self, lines: list[str]) -> tuple[list[list[str]], list[str]]:
        """收集表格。返回 (数据行, 表头)。取第一个带表头的表格。"""
        tables = self._all_tables(lines)
        if not tables:
            return [], []
        rows, header = tables[0]
        return rows, header

    def _all_tables(self, lines: list[str]) -> list[tuple[list[list[str]], list[str]]]:
        """收集所有带表头的表格。返回 [(rows, header), ...]。"""
        tables: list[tuple[list[list[str]], list[str]]] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if _is_table_row(line) and not _is_separator_row(line):
                cells = _split_row(line)
                if i + 1 < n and _is_separator_row(lines[i + 1]):
                    header = cells
                    rows: list[list[str]] = []
                    i += 2
                    while i < n and _is_table_row(lines[i]) and not _is_separator_row(lines[i]):
                        rows.append(_split_row(lines[i]))
                        i += 1
                    tables.append((rows, header))
                    continue
            i += 1
        return tables

    # -- 文档头 ---------------------------------------------------------

    def _parse_meta(self, lines: list[str]) -> None:
        m = self.model.meta
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if "文档日期" in s:
                m.doc_date = self._kv(s, "文档日期")
            elif "数据来源" in s:
                m.data_source = self._kv(s, "数据来源")
            elif "版本" in s:
                m.version = self._kv(s, "版本")
            elif "用途" in s:
                m.purpose = self._kv(s, "用途")
            elif "适用原则" in s:
                p = self._kv(s, "适用原则")
                if p:
                    m.principles = [x.strip() for x in re.split(r"[+、,，]", p) if x.strip()]
        m.raw = "\n".join(lines).strip()

    @staticmethod
    def _kv(line: str, key: str) -> str:
        m = re.search(re.escape(key) + r"\*{0,2}\s*[：:]\s*(.+)", line)
        return m.group(1).strip().strip("*") if m else ""

    # -- 大盘环境 -------------------------------------------------------

    def _parse_market(self, lines: list[str]) -> None:
        gate = self.model.global_.market_gate
        rows, _ = self._table_blocks(lines)
        for row in rows:
            if len(row) < 2:
                continue
            self._market_row(gate, row[0], row[1], row[2] if len(row) > 2 else "")
        for line in lines:
            s = line.strip()
            if s.startswith("**环境结论**") or s.startswith("环境结论"):
                gate.conclusion = s.split("**", 2)[-1].lstrip("：: ").strip(" *")
            elif s.startswith("> ⚠️") or s.startswith(">⚠️"):
                self.model.warnings.append(s.lstrip("> ").strip())
        # 从环境结论抽取大盘门控规则(含"前...不加仓/暂停/防守"等硬规则)
        for rule in self._gate_rules_from_conclusion(gate.conclusion):
            gate.rules.append(rule)
        gate.raw = "\n".join(lines).strip()

    @staticmethod
    def _gate_rules_from_conclusion(conclusion: str) -> list[GateRule]:
        """从环境结论抽取"若…则…"式大盘门控。

        例:"大盘 J 值回落到 60 以下前，所有 ETF 不加仓" →
           {condition:"J<60", action:"所有ETF不加仓"}
        """
        rules: list[GateRule] = []
        if not conclusion:
            return rules
        # 常见模式:X 前，Y 不加仓/暂停/防守
        m = re.search(r"([^。；；]+?)\s*前\s*[，,]\s*(所有[^。]+?(?:不加仓|暂停|防守|不追)[^。]*)", conclusion)
        if m:
            rules.append(GateRule(condition=m.group(1).strip(), action=m.group(2).strip(),
                                  raw=conclusion))
        return rules

    def _market_row(self, gate: MarketGate, item: str, value: str, note: str) -> None:
        if "收盘" in item:
            gate.close = _first_number(value)
            gate.change_pct = _first_percent(value)
            gate.close_note = note
        elif "均线" in item:
            for ma, v in re.findall(r"MA(\d+)\s+([\d.]+)", value):
                gate.ma.append(MaValue(period=f"MA{ma}", value=float(v), raw=value))
        elif "MACD" in item:
            gate.macd = f"{value} {note}".strip()
        elif "KDJ" in item:
            k = re.search(r"K\s*([\d.]+)", value)
            d = re.search(r"D\s*([\d.]+)", value)
            j = re.search(r"J\s*([\d.]+)", value)
            if k:
                gate.kdj["K"] = float(k.group(1))
            if d:
                gate.kdj["D"] = float(d.group(1))
            if j:
                gate.kdj["J"] = float(j.group(1))
        elif "压力" in item:
            gate.resistance = self._levels_from_text(value, "压力")
        elif "支撑" in item:
            gate.support = self._levels_from_text(value, "支撑")

    @staticmethod
    def _levels_from_text(text: str, name: str = "压力") -> list[Level]:
        """从"0.873(MA60) → 0.884(8/7高)"或"放量过 0.885"抽取价位 + 溯源。

        鲁棒性:
        - 跳过 MA 周期数字(如 "MA20" 的 20,不是价位)
        - 优先取 "=1.012" / "≈1.012" 带注释的实际值(数据来源格式)
        - 日期数字(如 "8/19=1.012" 的 8/19)不作为价位
        """
        out: list[Level] = []
        for seg in re.split(r"[→➜]", text):
            seg = seg.strip()
            if not seg:
                continue
            price, src = _extract_one_level(seg)
            if price is None:
                continue
            out.append(Level(name=name, price=price, source=src, raw=seg))
        return out

    # -- 持仓一览表 -----------------------------------------------------

    def _parse_positions(self, lines: list[str]) -> None:
        rows, header = self._table_blocks(lines)
        if not header:
            return
        cols = {c: i for i, c in enumerate(header)}
        for row in rows:
            code = self._row_code(row, cols)
            if not code:
                continue
            inst = self._ensure_instrument(code, self._row_name(row, cols))
            pos = Position()
            if "持有股数" in cols:
                pos.shares = self._int(row[cols["持有股数"]])
            if "成本" in cols:
                pos.cost = _first_number(row[cols["成本"]])
            if "现价" in cols:
                pos.price = _first_number(row[cols["现价"]])
            if "市值" in cols:
                pos.market_value = _first_number(row[cols["市值"]])
            if "浮亏%" in cols:
                pos.loss_pct = _first_percent(row[cols["浮亏%"]])
            if "浮亏金额" in cols:
                pos.loss_amount = _first_number(row[cols["浮亏金额"]])
            if "解套需涨" in cols:
                pos.recover_need_pct = _first_percent(row[cols["解套需涨"]])
            pos.raw = " | ".join(row)
            inst.position = pos

    def _row_code(self, row: list[str], cols: dict[str, int]) -> str:
        if "代码" in cols:
            return _norm_code(row[cols["代码"]])
        for cell in row:
            codes = _extract_codes(cell)
            if codes:
                return codes[0]
        return ""

    def _row_name(self, row: list[str], cols: dict[str, int]) -> str:
        return row[cols["名称"]] if "名称" in cols else ""

    def _cheat_name(self, row: list[str], cols: dict[str, int]) -> str:
        """速查表无"名称"列,从"品种"单元格提取名称(如 '通信 515050' → '通信')。"""
        if "名称" in cols and cols["名称"] < len(row):
            return row[cols["名称"]]
        if "品种" in cols and cols["品种"] < len(row):
            cell = row[cols["品种"]]
            # 去掉代码,取剩余文本
            cleaned = CODE_RE.sub("", cell).strip()
            return cleaned
        return ""

    @staticmethod
    def _int(text: str) -> int | None:
        v = _first_number(text)
        return int(v) if v is not None else None

    # -- 逐只分析 -------------------------------------------------------

    def _parse_instruments(self, lines: list[str]) -> None:
        for code, name, block in self._instrument_blocks(lines):
            inst = self._ensure_instrument(code, name)
            self._parse_instrument_block(inst, block)

    def _instrument_blocks(self, lines: list[str]) -> list[tuple[str, str, list[str]]]:
        blocks: list[tuple[str, str, list[str]]] = []
        cur_code = cur_name = ""
        cur_block: list[str] = []
        for line in lines:
            if _is_heading(line) and re.search(r"^\s*#{3,}\s*\d+\s*\.", line):
                if cur_code:
                    blocks.append((cur_code, cur_name, cur_block))
                title = re.sub(r"^#{3,}\s*\d+\s*\.\s*", "", line.strip())
                code = self._title_code(title)
                name = self._title_name(title, code)
                cur_code, cur_name = code, name
                cur_block = [line]
            else:
                cur_block.append(line)
        if cur_code:
            blocks.append((cur_code, cur_name, cur_block))
        return blocks

    @staticmethod
    def _title_code(title: str) -> str:
        m = CODE_RE.search(title)
        return m.group(1) if m else ""

    @staticmethod
    def _title_name(title: str, code: str) -> str:
        name = title
        if code:
            name = name.split(code)[0]
        name = re.split(r"[（(]", name)[0].strip()
        return name

    def _parse_instrument_block(self, inst: Instrument, lines: list[str]) -> None:
        # 关键位地图(压力/支撑列表)
        for line in lines:
            s = line.strip()
            if s.startswith("- 压力") or s.startswith("压力："):
                inst.levels += self._levels_from_text(s.split("压力", 1)[1].lstrip("：: "), "压力")
            elif s.startswith("- 支撑") or s.startswith("支撑："):
                inst.levels += self._levels_from_text(s.split("支撑", 1)[1].lstrip("：: "), "支撑")
            elif s.startswith("**今日盘面**"):
                inst.analysis["盘面"] = s.split("**", 2)[-1].strip()
            elif s.startswith("**技术指标**"):
                inst.analysis["技术指标"] = s.split("**", 2)[-1].strip()
            elif s.startswith("**基本面"):
                inst.analysis["基本面"] = s.split("**", 2)[-1].strip()
            elif s.startswith("**消息面"):
                inst.analysis["消息面"] = s.split("**", 2)[-1].strip()
        # 操作建议表
        rows, header = self._table_blocks(lines)
        if header and "动作" in header and "触发条件" in header and "具体操作" in header:
            for row in rows:
                rule = self._rule_from_row(row, header)
                if rule is None:
                    continue
                if rule.computable:
                    inst.rules.append(rule)
                else:
                    # §4.1:不可计算条件进 alerts 不进 rules
                    self.model.unresolved.append(
                        f"{inst.code} [{rule.action}] 条件含主观词,转预警待确认: {rule.condition}")
                    inst.alerts.append(Alert(kind="预警", content=rule.condition, raw=rule.raw))
        # 基本面预警(逐只块内的"基本面预警"行)
        for line in lines:
            s = line.strip()
            if s.startswith("**基本面预警**"):
                content = s.split("**", 2)[-1].strip().lstrip("：: ")
                if content:
                    inst.alerts.append(Alert(kind="预警", content=content, raw=s))
        inst.raw = "\n".join(lines).strip()

    def _rule_from_row(self, row: list[str], header: list[str]) -> Rule | None:
        cols = {c: i for i, c in enumerate(header)}
        action = row[cols["动作"]] if "动作" in cols and cols["动作"] < len(row) else ""
        condition = row[cols["触发条件"]] if "触发条件" in cols and cols["触发条件"] < len(row) else ""
        operation = row[cols["具体操作"]] if "具体操作" in cols and cols["具体操作"] < len(row) else ""
        if not action or not condition:
            return None
        computable = self._is_computable(condition)
        return Rule(action=action, condition=condition, operation=operation,
                    computable=computable, raw=" | ".join(row))

    @staticmethod
    def _is_computable(condition: str) -> bool:
        """判断触发条件是否可计算(价格/量能/指标/均线/大盘门控)。

        含"情绪面/消息面/预期/政策/观察"等主观词 → 不可计算(进 alerts)。
        仅含价格区间/放量/缩量/均线/KDJ/MACD/站稳/跌破/突破/回踩等 → 可计算。
        """
        subjective = ["情绪", "消息面", "预期", "政策", "观察", "资金认可",
                      "证伪", "兑现", "判断", "逻辑", "催化"]
        if any(w in condition for w in subjective):
            return False
        return True

    def _ensure_instrument(self, code: str, name: str = "") -> Instrument:
        inst = self.model.instrument_by_code(code)
        if inst is None:
            inst = Instrument(code=code, name=name)
            self.model.instruments.append(inst)
        elif name and not inst.name:
            inst.name = name
        return inst

    # -- 基本面/消息面预警速查(§四) -------------------------------------

    def _parse_alerts_section(self, lines: list[str]) -> None:
        rows, header = self._table_blocks(lines)
        if not header:
            return
        cols = {c: i for i, c in enumerate(header)}
        # 列名可能为:品种/最重要的基本面指标/看空预警触发/当前状态
        for row in rows:
            code = self._row_code(row, cols)
            name = self._row_name(row, cols)
            if not code:
                continue
            inst = self._ensure_instrument(code, name)
            content_parts = []
            for key, label in (("最重要的基本面指标", "最重要指标"),
                               ("看空预警触发", "看空预警")):
                if key in cols and cols[key] < len(row) and row[cols[key]]:
                    content_parts.append(f"{label}:{row[cols[key]]}")
            level = "info"
            if "当前状态" in cols and cols["当前状态"] < len(row) and row[cols["当前状态"]]:
                state = row[cols["当前状态"]]
                if "🔴" in state:
                    level = "red"
                elif "🟢" in state:
                    level = "green"
                elif "🟡" in state:
                    level = "yellow"
                content_parts.append(f"当前状态:{state}")
            if content_parts:
                inst.alerts.append(Alert(kind="催化", content="；".join(content_parts),
                                         level=level, raw=" | ".join(row)))

    # -- 加仓预算分配(§五) ----------------------------------------------

    def _parse_budget(self, lines: list[str]) -> None:
        g = self.model.global_
        # 可用子弹
        for line in lines:
            s = line.strip()
            m = re.search(r"可用子弹\s*约?\s*([\d,]+)\s*元", s)
            if m:
                g.budget_total = float(m.group(1).replace(",", ""))
        # 预算分配表(优先级/品种/解套距离/定位/预算占比)
        rows, header = self._table_blocks(lines)
        if not header:
            return
        cols = {c: i for i, c in enumerate(header)}
        for row in rows:
            item = BudgetItem()
            if "品种" in cols and cols["品种"] < len(row):
                cell = row[cols["品种"]]
                codes = _extract_codes(cell)
                item.instrument = cell
                if codes:
                    item.code = codes[0]
            if "优先级" in cols and cols["优先级"] < len(row):
                item.priority = self._int(row[cols["优先级"]])
            if "预算占比" in cols and cols["预算占比"] < len(row):
                item.budget_pct = _first_percent(row[cols["预算占比"]])
            if "定位" in cols and cols["定位"] < len(row):
                item.note = row[cols["定位"]]
            if item.instrument:
                item.raw = " | ".join(row)
                g.budget_items.append(item)

    # -- 通用纪律(§六) & 风险提示(§七) -----------------------------------

    def _parse_discipline(self, lines: list[str]) -> None:
        for line in lines:
            s = line.strip()
            if s and not _is_table_row(s) and not _is_heading(s):
                # 编号列表行,如 "1. **J值>85不追高**..."
                s2 = re.sub(r"^\s*\d+[.、]\s*", "", s)
                if s2:
                    self.model.global_.discipline.append(s2)

    def _parse_risks(self, lines: list[str]) -> None:
        for line in lines:
            s = line.strip()
            if s and not _is_table_row(s) and not _is_heading(s):
                s2 = re.sub(r"^\s*\d+[.、]\s*", "", s)
                if s2:
                    self.model.global_.risks.append(s2)

    # -- 触发信号速查(§八) ----------------------------------------------

    def _parse_cheatsheet(self, lines: list[str]) -> None:
        """触发信号速查表:买点(回踩)/买点(突破)/减仓红线/生命线。并入 levels。"""
        # 先建表行对应的 instrument(速查表可能含持仓表没有的标的,如 通信 只在此出现)
        rows, header = self._table_blocks(lines)
        if not header:
            return
        cols = {c: i for i, c in enumerate(header)}
        for row in rows:
            code = self._row_code(row, cols)
            if not code:
                continue
            self._ensure_instrument(code, self._cheat_name(row, cols))
        # 再按红线溯源原则注归类类型(波段仓/解套仓,按名称匹配)
        for line in lines:
            s = line.strip()
            if "波段仓" not in s or "解套仓" not in s:
                continue
            for t, key in (("波段仓", "波段仓"), ("解套仓", "解套仓")):
                m = re.search(re.escape(key) + r"[（(]([^）)]*)[）)]", s)
                if not m:
                    continue
                for name in re.split(r"[/、,，]", m.group(1)):
                    name = name.strip()
                    if not name:
                        continue
                    inst = self._match_instrument_name(name)
                    if inst is not None:
                        inst.type = t
        # 最后把速查表的价位并入 levels(列名可能带"（数据来源）"后缀,用子串匹配)
        for row in rows:
            code = self._row_code(row, cols)
            if not code:
                continue
            inst = self._ensure_instrument(code, self._cheat_name(row, cols))
            for key, lname in (("买点（回踩）", "买区"), ("买点(回踩)", "买区"),
                               ("买点（突破）", "突破点"), ("买点(突破)", "突破点"),
                               ("减仓红线", "减仓红线"), ("生命线", "生命线")):
                ci = _col_idx(cols, key)
                if ci is None or ci >= len(row) or not row[ci]:
                    continue
                cell = row[ci]
                vals = self._levels_from_text(cell)
                # 买区是区间(如 0.855-0.860):显式拆出下沿/上沿
                if lname == "买区":
                    lo, hi = _range_of(cell)
                    if lo is not None:
                        inst.levels.append(Level(name="买区下沿", price=lo, source="", raw=cell))
                    if hi is not None:
                        inst.levels.append(Level(name="买区上沿", price=hi, source="", raw=cell))
                    continue
                for v in vals:
                    v.name = lname
                    inst.levels.append(v)

    def _match_instrument_name(self, short: str) -> Instrument | None:
        """按简称匹配 instrument(如 '通信' 匹配 '通信ETF华夏')。"""
        for inst in self.model.instruments:
            if inst.name == short:
                return inst
            if short and (short in inst.name or inst.name in short):
                return inst
        return None

    # -- 每日盯盘记录 -----------------------------------------------------

    def _parse_journal(self, lines: list[str]) -> None:
        """按 '### 📅 日期(星期) 时段' 切段,每段一条 JournalEntry。"""
        cur_date = ""
        cur_lines: list[str] = []
        entries: list[JournalEntry] = []

        def flush() -> None:
            if not cur_date:
                return
            text = "\n".join(cur_lines).strip()
            kind = self._journal_kind(text)
            entries.append(JournalEntry(date=cur_date, content=text, kind=kind, raw=text))

        for line in lines:
            m = re.match(r"^#+\s*📅\s*(\d{4}-\d{2}-\d{2})", line.strip())
            if m:
                flush()
                cur_date = m.group(1)
                cur_lines = [line]
            else:
                cur_lines.append(line)
        flush()
        self.model.daily.journal = entries
        # 从盯盘记录里提取剧本修正表(品种/隔夜影响/今日动作)
        for entry in entries:
            self._parse_playbook_from_journal(entry.content)

    def _parse_playbook_from_journal(self, content: str) -> None:
        """从盯盘条目中提取剧本修正表。签名:含 品种 + 今日动作 列。"""
        for rows, header in self._all_tables(content.splitlines()):
            if not header:
                continue
            # 剧本修正表必须含"今日动作"(区别于"今日预案"等)
            if not ("品种" in header or "代码" in header):
                continue
            if "今日动作" not in header and "动作" not in header:
                continue
            cols = {c: i for i, c in enumerate(header)}
            for row in rows:
                pb = PlaybookRow()
                if "品种" in cols and cols["品种"] < len(row):
                    cell = row[cols["品种"]]
                    codes = _extract_codes(cell)
                    pb.instrument = cell
                    if codes:
                        pb.code = codes[0]
                if "隔夜影响" in cols and cols["隔夜影响"] < len(row):
                    pb.overnight = row[cols["隔夜影响"]]
                if "今日动作" in cols and cols["今日动作"] < len(row):
                    pb.action = row[cols["今日动作"]]
                elif "动作" in cols and cols["动作"] < len(row):
                    pb.action = row[cols["动作"]]
                if pb.instrument and not self._pb_duplicate(pb):
                    pb.raw = " | ".join(row)
                    self.model.daily.playbook.append(pb)

    def _pb_duplicate(self, pb: PlaybookRow) -> bool:
        for existing in self.model.daily.playbook:
            if existing.code and existing.code == pb.code:
                return True
            if pb.code and not existing.code and existing.instrument == pb.instrument:
                return True
        return False

    @staticmethod
    def _journal_kind(text: str) -> str:
        if "盘前" in text[:400]:
            return "盘前"
        if "复盘" in text or "对账" in text:
            return "复盘"
        if "收盘对账" in text:
            return "盘后"
        return "其他"


# ---------------------------------------------------------------- 便捷入口

def parse_file(path: str | Path) -> RuleModel:
    """解析作战地图 .md 文件 → RuleModel。"""
    return BattleMapParser(path=path).parse()


def parse_text(text: str) -> RuleModel:
    """解析作战地图文本 → RuleModel。"""
    return BattleMapParser(text=text).parse()