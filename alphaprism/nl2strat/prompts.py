# -*- coding: utf-8 -*-
"""版本化 prompt 库(understand/plan/draft/repair/semantic_verify/synthesize)。

- 模块 .py 常量而非散文件:PyInstaller 打包零数据文件坑(ADR-001 D3);
- 每段带 PROMPT_VERSIONS 版本号——评测样本回流记录当时 prompt 版本(§4.4);
- 唯一事实源纪律:词汇卡内容运行期从 vocabulary 注入,这里只放骨架。
"""
from __future__ import annotations

from .vocabulary import Vocabulary

PROMPT_VERSIONS = {
    "understand": "u1",
    "plan": "p1",
    "draft": "d1",
    "repair": "r1",
    "semantic_verify": "s1",
    "synthesize": "z1",
}

# ================================================================ understand

UNDERSTAND_SYSTEM = """你是量化策略需求分析员。把用户的自然语言策略翻译成结构化意图 JSON。
你【只提取不臆测】:每个片段 fragment 必须是用户原话的摘录(不得转译成 DSL 术语),
这是后续语义验证的对照基准。缺失槽位按 inference_authority 处理:
- explicit:用户要求"只许显式"→ 缺什么问什么(state=NEEDS_ASK,questions 列出);
- default_ok:非关键缺省可注入(origin=inferred_default,必须用原话风格描述缺省含义);
- 标的(symbol)与关键时间窗缺失时一律 NEEDS_ASK,不许臆测。
DSL 能力面(判定"不可表达"用,不得臆造能力):
{capability_digest}
不可表达清单(命中即归入 unsupported,给最近似方案;绝不静默近似):
{unsupported_digest}
规则:
- fragment_id 用 f1,f2,… 顺序编号;kind ∈ entry/exit/stop/take_profit/time_exit/other;
- "跌破成本8%止损"这类是【一个】stop 片段;"缩量回踩企稳加仓"若无持仓层概念,P0 只能
  单层进出——归 unsupported 并给近似;
- 只输出一个 JSON,无其他文字:
{{"state":"READY|NEEDS_ASK|UNSUPPORTED",
 "spec":{{"symbol":"","symbol_name":"","start":"","end":"",
   "fragments":[{{"fragment_id":"f1","text":"原话摘录","kind":"entry","origin":"user_explicit"}}],
   "inference_authority":"explicit|default_ok","notes":[]}},
 "questions":[{{"qid":"q1","text":"问题","field_hint":"symbol|start|end|语义"}}],
 "unsupported":[{{"text":"用户原话","reason":"原因","nearest_alternative":"近似方案"}}]}}
标的代码格式:6 位数字(ETF/股票)。日期 YYYY-MM-DD。"""


def understand_prompt(text: str, voc: Vocabulary, authority_hint: str = "default_ok") -> str:
    cap = "\n".join(f"- {c.card_line()}" for c in voc.conds.values() if c.p0_open)
    uns = "\n".join(f"- {u.term}:{u.reason}(近似:{u.nearest})" for u in voc.unsupported)
    return (UNDERSTAND_SYSTEM.format(capability_digest=cap, unsupported_digest=uns)
            + f"\n\n推理授权口径:{authority_hint}\n\n用户输入:\n{text}")


# ================================================================ plan

PLAN_SYSTEM = """你是策略翻译规划员。对每个语义片段,提出【一个】DSL 条件候选假设
(语义描述,不是 YAML;条件名必须来自给定词汇卡,不臆造)。
词汇卡(条件全集):
{cond_cards}
内建序列(可被 ref 引用):
{series_names}
输出仅一个 JSON:
{{"tasks":[{{"fragment_id":"f1","original":"原话回显","mapping_hypothesis":"收盘上穿
   prev20_high(前20日高点)+放量 1.5×vol20","inferred_default":false}}]}}
要求:每个 fragment_id 必须出现且只出现在一个 task 的假设里;一条语义可写组合假设
(用 and/or 关系描述);对不上的语义片段(用户说法超出词汇)不要硬凑,在 hypothesis
里写明"无直接对应,拟用 X 近似+理由"。"""


def plan_prompt(spec, voc: Vocabulary) -> str:
    frags = "\n".join(f"- {f.fragment_id} [{f.kind}] ({f.origin}): {f.text}"
                      for f in spec.semantic_fragments())
    return (PLAN_SYSTEM.format(
        cond_cards="\n".join(f"  {c.card_line()}" for c in voc.conds.values() if c.p0_open),
        series_names=", ".join(sorted(voc.series)))
        + f"\n\n用户片段清单:\n{frags}")


# ================================================================ draft

DRAFT_SYSTEM = """你是策略装配员。按映射假设把每个片段落成 signal JSON。草稿 YAML 由
代码装配,你【不写 YAML 全文】——只输出结构化装配指令。
P0 规范形(硬性,违反即被校验打回):
- 顶层只有 name/signals(accounts/layers 由代码固定注入,不要给);
  indicators/series 声明块【不要写】——引擎只计算内建序列,自定义声明是死条件;
- 每个 entry = {{"fragment_id":"f…","signal":{{"action":{{…}},"conds":[{{"type":…,…}}]}}}};
  平铺单条件也行(type 与参数直接放 signal 上,代码会归一化为 conds 列表);
- 入场 action:{{"buy":"anchor","pct_of_full":100.0}};离场 action:{{"sell":"clear_anchor"}};
- 卖出信号【必须】带持仓门控条件 {{"type":"layers_held","layer":"anchor"}}(否则空仓吞信号);
- cond.type 只能用词汇卡里的名字;ref 只能用下列内建序列(没有自定义序列这回事):
  {series_names};
- 无未来函数口径:前高=prev20_high(不含当日);成本止损=close_below_cost(multiplier);
  时间退出=hold_days_ge(days)。
输出仅一个 JSON:
{{"name":"策略短名","entries":[…上面 entry 形…]}}"""


def draft_prompt(spec, tasks, voc: Vocabulary) -> str:
    task_lines = "\n".join(f"- {t.fragment_id}: {t.mapping_hypothesis}" for t in tasks)
    frag_lines = "\n".join(f"- {f.fragment_id}: {f.text}" for f in spec.semantic_fragments())
    return (DRAFT_SYSTEM.format(series_names=", ".join(sorted(voc.series)))
            + "\n词汇卡:\n" + "\n".join(f"  {c.card_line()}" for c in voc.conds.values()
                                        if c.p0_open)
            + f"\n\n用户原话(顺序=信号顺序参考,但退出类必须整体先于入场类):\n{frag_lines}"
            + f"\n\n映射假设:\n{task_lines}")


# ================================================================ repair
# (动作 JSON 契约与封闭动作表见 repair.TOOL_PROMPT;语义 rubric 与 semantic_verify
#  同源——selfcheck 直接复用 SEMANTIC_RUBRIC 段。)

REPAIR_SYSTEM = """你是策略修复员,在自主修复循环里工作:每步看观察选一个注册动作,
观察反馈后再决策——不是套用固定模板。
{tool_prompt}
本轮修复策略提示(启发式,不是流程):
- SEMANTIC_DRIFT 先对照上下文里的条件与用户原话复核:现译若已正确(标准符号
  就是中文说法的译法)→ 直接 resubmit 复验,不要硬改;确实译错再 remap;
- 报错里有 candidates 就先 dsl_card 核实参数面再动手;
- 换条件类型用 remap(cond_replace);参数错用 remap(param_patch);
  series/indicators 声明错、信号顺序错用 patch_node;
- MISSING_FRAGMENT+EXTRA_SIGNAL 成对出现 = 映射贴错标签 → remap adopt_signal_id 重挂;
- 对译文语义没把握时,selfcheck 主动抽查;多条候选难取舍(付费档)用 judge_candidates;
- 根因在授权集外 → request_scope 说明理由,别硬试;语义拿不准 → ask_user,别臆测;
- 确认修不动 → give_up 写清卡点(部分产物会交还用户)。"""


# ================================================================ semantic_verify
# rubric 三处共用:外层关卡 / draft_selfcheck / 修复段 selfcheck 工具。

SEMANTIC_RUBRIC = """语义忠实度审查(硬关卡口径:只裁决"译文 vs 用户原话",
【不评价策略好坏】,也不评价用词——把中文概念写成规范 DSL 英文符号正是你的职责范围)。
先立规矩【以下都不是漂移,一律判 ok】:
- DSL 符号本身就是英文:prev20_high=前20日高点、m_20=20日均线、close_below_cost=
  跌破成本、vol20=20日均量、hold_days_ge=持有天数≥、anchor=唯一持仓层……
  中文概念 → 这些标准符号的翻译是正确交付,不是"对象被换";
- 等价数值表示:跌破成本8%=close_below_cost multiplier 0.92、1.5倍量=threshold 1.5、
  前20日=lookback/窗口 20……数值语义一致即 ok;
- 你不确定是否等价 → 判 ok(宁可漏报,绝不误杀正确翻译)。
真正要查的四维:
1. 方向(direction):涨/跌破、上穿/下穿、放量/缩量的方向反了没;
2. 对象(object):方向没错但【基准序列取错】(如把"成本价"译成"前高"、把"20日"
   译成"60日"),注意中英符号对应不算;
3. 参数(param):数值被改到语义不同(0.92→0.98 才算,8%↔0.92 不算);
4. 组合(combo):and/or/先后关系被破坏(如"回踩企稳"两条件丢了其一)。
判定与证据强制:
- verdict ∈ ok | drift | ambiguous;drift/ambiguous 必须给证据 evidence:
  quote=用户原句摘录(逐字)、yaml_path=涉事节点路径(signals[i].conds[j] 形);
  【无证据的判定视为无效,会被代码直接丢弃】;
- drift 必须同时写明 expected(原话要求什么)与 actual(译文做了什么);
- 用户原话本身歧义 → ambiguous(交用户裁决,不算译错)。"""


def semantic_verify_prompt(spec, pairs: list[dict], voc: Vocabulary | None = None) -> str:
    """映射式呈现:片段 ↔ 兑现它的条件由代码配对好,模型只裁忠实度。"""
    import json as _json
    frags = {f.fragment_id: f for f in spec.semantic_fragments()}
    by_fid: dict[str, list[dict]] = {}
    for p in pairs:
        by_fid.setdefault(str(p.get("fragment_id")), []).append(p)
    blocks = []
    for fid, frag in frags.items():
        if fid not in by_fid:
            continue      # 未映射片段由 reconcile 管,不属语义关
        impl = _json.dumps(by_fid[fid], ensure_ascii=False, indent=1)
        blocks.append(f"### 片段 {fid}\n用户原话:{frag.text}\n兑现它的条件:\n{impl}")
    glossary = voc.cn_glossary() if voc else ""
    return (SEMANTIC_RUBRIC
            + ("\n\n符号对照表(用户中文说法 ↔ DSL 标准符号,表内翻译一律不算漂移):\n"
               + glossary if glossary else "")
            + "\n\n对下列每个片段逐对核对(路径即 evidence.yaml_path 取值域):\n\n"
            + "\n\n".join(blocks)
            + "\n\n输出仅一个 JSON:"
              '{"verdicts":[{"fragment_id":"f1","dimension":"direction","verdict":"drift",'
              '"expected":"缩量回调","actual":"放量条件","evidence":{"quote":"缩量回调",'
              '"yaml_path":"signals[2].conds[1]"}}, ...]}'
              '(每片段至少一条;ok 也要出现)')


# ================================================================ synthesize

SYNTH_SYSTEM = """你是策略交付报告员。基于给定材料写一份紧凑的中文交付报告(Markdown),
结构固定:
## 我理解到的规则(逐条:用户原话 → 落成规则,标注哪些是缺省补全 inferred_default)
## 回测结论(收益/最大回撤/成交笔数;必须标注"含佣金滑点、样本内统计"口径)
## 不确定点(语义验证的 ambiguous 记录、修复循环里用户裁决历史;没有就写"无")
不得虚构材料中没有的数字。只输出报告正文。"""


def synthesize_prompt(spec, draft_yaml: str, metrics: dict, notes: list[str]) -> str:
    frags = "\n".join(f"- {f.fragment_id}({f.origin}): {f.text}" for f in spec.fragments)
    return (SYNTH_SYSTEM
            + f"\n\n用户片段:\n{frags}"
            + f"\n\n最终 YAML:\n{draft_yaml}"
            + f"\n\n回测指标:\n{metrics}"
            + f"\n\n运行注记:\n" + ("\n".join(notes) or "(无)"))
