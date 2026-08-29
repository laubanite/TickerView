# -*- coding: utf-8 -*-
"""深入分析 · 片段填充分层(2026-08-29,架构转向:LLM 一次长文 → 骨架+片段)。

三层:
1. 程序骨架段(纯确定性):六/七/建议类别/结论卡 由程序直接输出,零 LLM。
2. 骨架底线句(程序生成):每 LLM 段一个"事实+方向"底线句,LLM 只准在其上扩展
   (不得篡改数值/方向),失败回退底线句——输出永远完整可读。
3. LLM 填充片段(每段独立):注入全局基调(核心判断),小批量并发(≤3-5),
   每段独立校验(价位白名单/方向/禁词),违规段回退底线句。

内外盘过滤(2026-08-29):深入分析输入剔除"外内盘/资金·信号"行——程序已自标
"参考性弱",不再喂给 LLM 引发"一边说不可靠一边拿来分析"的自相矛盾;
数据快照卡仍保留全量(面向要看原始数据的用户)。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 并发上限(小批量,防限流)
SEG_CONCURRENCY = 3


# --------------------------------------------------------------------------- 输入过滤

def filter_analysis_input(snapshot_md: str) -> str:
    """从快照 markdown 剔除 深入分析 不消费的行:
    - 外内盘行(程序自标参考性弱,不喂 LLM)
    - 资金·信号 行(同理由)
    返回过滤后的快照文本(数据快照卡本体不动)。
    """
    kept = []
    for line in snapshot_md.splitlines():
        if re.match(r"\| 盘口 \| 外内盘 \|", line):
            continue
        if re.match(r"\| 资金 \| 信号 \|", line):
            continue
        kept.append(line)
    return "\n".join(kept)


# --------------------------------------------------------------------------- 结构化分段 facts(2026-08-29)
# 每段只给本段消费的最小事实集(JSON),避免 LLM 从全量快照挑错段数据。

def _j(d: dict) -> str:
    import json
    return json.dumps(d, ensure_ascii=False)


def segment_facts(facts: dict, state: dict, anchors: dict,
                  seg_name: str) -> str:
    """按段提取 结构化 facts(JSON 字符串)。每段只含该段相关的最小字段。"""
    etf = facts.get("etf") or {}
    d = etf.get("daily") or {}
    m = etf.get("m30") or {}
    mn = etf.get("minute") or {}
    ff = etf.get("fund_flow") or {}
    idx = facts.get("index") or {}
    id_mn = idx.get("minute") or {}
    id_d = idx.get("daily") or {}
    id_ma = id_d.get("ma") or {}
    id_macd = id_d.get("macd") or {}
    zone = anchors.get("pullback_zone") or {}

    seg_data = {
        "一、大盘环境": {
            "大盘趋势": (id_mn.get("price"), id_ma.get(20)),
            "大盘动能": (id_macd.get("dif"), id_macd.get("dea")),
            "标的相对强弱(近5日)": facts.get("rs_5d"),
        },
        "二、标的·日线": {
            "现价": mn.get("price"),
            "阶段高点(近250日)": d.get("swing_high"),
            "距阶段高点回撤%": etf.get("swing_dd"),
            "近20日区间": (d.get("recent_low"), d.get("recent_high")),
            "现价区间位置%": etf.get("range_pos"),
            "区间档": ("下轨" if (etf.get("range_pos") or 50) < 30
                       else "上轨" if (etf.get("range_pos") or 50) >= 70
                       else "中上轨" if (etf.get("range_pos") or 50) >= 50 else "中下轨"),
            "日线均线": d.get("ma"),
            "日线KDJ": d.get("kdj"),
            "日线MACD": d.get("macd"),
        },
        "三、标的·30分": {
            "30分均线": m.get("ma"),
            "近60根区间": (m.get("range_low"), m.get("range_high")),
            "30分KDJ": m.get("kdj"),
            "30分MACD": m.get("macd"),
            "周期优先级提示": "日线定方向,30分定节奏(不允许30分单独转向)",
        },
        "四、分时盘口": {
            "现价": mn.get("price"),
            "日内高低": (mn.get("high"), mn.get("low")),
            "距日内最高回撤%": mn.get("intraday_dd"),
            "量比": etf.get("vol_ratio"),
            "换手%": (etf.get("orderbook") or {}).get("turnover_pct"),
            "尾盘量能": mn.get("tail_vol"),
            "折溢价%": (etf.get("orderbook") or {}).get("premium_pct"),
        },
        "六、操作建议与风险提示": {
            "锚定价": anchors.get("anchor_price"),
            "状态词": state.get("state_word"),
            "场景": state.get("scenario"),
            "突破档(关键价位表)": (anchors.get("breakout_add"), anchors.get("breakout_src")),
            "反弹压力带": anchors.get("resist_band"),
            "回踩带": (zone.get("lower"), zone.get("upper")) if zone else None,
            "减仓档": anchors.get("cut_loss"),
            "清仓止损": anchors.get("stop_loss"),
            "风险等级": (facts.get("catalyst") or {}).get("risk_level", "正常"),
            "状态词含义": state.get("sub_state"),
        },
        "八、资金面": {
            "份额(亿份)": ff.get("share"),
            "份额日期": ff.get("date"),
            "较上期(亿份)": ff.get("share_chg"),
            "估算规模(亿元)": ff.get("scale_est"),
            "折溢价%": (etf.get("orderbook") or {}).get("premium_pct"),
            "净值关系提示": "估算规模=份额×净值,由此可反推净值≈规模/份额;折溢价%已由程序给出,禁止用份额与规模量纲比较推断折溢价",
        },
    }.get(seg_name)
    return _j(seg_data) if seg_data is not None else "{}"


# --------------------------------------------------------------------------- 每段解释范围(只准写这些,禁写其他)

_SEG_SCOPE: dict[str, str] = {
    "一、大盘环境": "只解释大盘趋势(M20)与动能(DIF/DEA)两维,及对标的的相对压制/托举;不要展开标的自身技术面。",
    "二、标的·日线": "只解释日线均线关系与位置档(近20日区间位置),以及日线 KDJ/MACD 的方向含义;不涉及30分与盘口。",
    "三、标的·30分": "只解释30分均线/区间/KDJ/MACD 的节奏含义,以及与日线的共振或矛盾;不单独给方向。",
    "四、分时盘口": "只解释量比/换手/折溢价/尾盘量能的盘面含义;禁止分析内外盘。",
    "六、操作建议与风险提示": "只写一段完整自然语言:①当前状态(状态词+锚定价)②'什么情况做什么'(引用关键价位表,触发带确认刻度)③风险提示(缺项/资金面/折溢价)④企稳信号 C8' 三选一。不重复罗列价位。",
    "八、资金面": "只解释份额变化/估算规模(净值关系)/折溢价三者的资金含义;禁止内外盘结论。",
}

_SEG_FORBIDDEN: dict[str, list[str]] = {
    "一、大盘环境": ["偏暖(站上M20·MACD零上)", "系统性压制(均线空头排列·MACD零下)"],
    "二、标的·日线": ["超卖", "超买"],
    "三、标的·30分": [],
    "四、分时盘口": ["内盘", "外盘", "抛压占优", "承接占优"],
    "六、操作建议与风险提示": ["抄底", "满仓", "立即加仓", "立即减仓"],
    "八、资金面": ["内盘", "外盘"],
}

_SEG_EXTRA_RULES: dict[str, str] = {
    "二、标的·日线": ";MACD 零下红柱 = 空头环境中的弱反弹动能,不是趋势转多",
    "三、标的·30分": ";30分KDJ 超买(J>80)约束短线反弹高度,超卖(J<20)在日线零下时不是买点(可能下跌中继);30分 MACD DIF/DEA 接近零轴时,用'短期动能接近消失、方向不明确'表述,禁止'历史差值与前期差值持平'类自造措辞",
    "六、操作建议与风险提示": ";状态词=左侧观望/蓄势时结论须是'不试多、观察',写'试多'前必须先列 §4.2 准入链缺项;企稳只许 C8' 三选一",
    "八、资金面": ";禁止用'份额'与'规模'量纲比大小推断折溢价(亿份 vs 亿元);折溢价 ±% 由程序给出,直接引用",
}


# --------------------------------------------------------------------------- 片段 prompt(6 要素,2026-08-29)

def _fmt_p(v):
    from .intraday import _fmt_price
    return _fmt_price(v)


def skeleton_baselines(facts: dict, state: dict, anchors: dict,
                       catalyst: dict | None = None) -> dict[str, str]:
    """每 LLM 段生成 程序底线句:事实准确 + 方向判定,LLM 只能扩展不能改。

    v2.9(2026-08-29 系统性修复#①):给 LLM 「确定性结论词」而非原始数字关系——
    如直接说"大盘站上 M20",禁止 LLM 自行比较现价 vs M20 大小(会看反)。
    """
    etf = facts.get("etf") or {}
    d = etf.get("daily") or {}
    m = etf.get("m30") or {}
    mn = etf.get("minute") or {}
    ob = etf.get("orderbook") or {}
    ff = etf.get("fund_flow") or {}
    price = mn.get("price")
    anchor = anchors.get("anchor_price")
    base: dict[str, str] = {}

    # 一、大盘环境:结论词直达,不给"现价X vs M20 Y"让 LLM 自行比较
    idx = facts.get("index")
    if idx:
        id_mn = idx.get("minute") or {}
        id_d = idx.get("daily") or {}
        id_ma = id_d.get("ma") or {}
        id_price = id_mn.get("price")
        id_macd = id_d.get("macd") or {}
        m20 = id_ma.get(20)
        rel_txt = ("站上" if id_price is not None and m20 is not None and id_price >= m20
                   else "跌破" if id_price is not None and m20 is not None else "数据缺失")
        dif, dea = id_macd.get("dif"), id_macd.get("dea")
        if dif is not None and dea is not None:
            # 结论词(DIF/DEA 与零轴关系)——不重复报数,直接给语义
            if dif >= 0 and dea >= 0:
                mom_word = "DIF/DEA 均在零轴上(动能偏多)"
            elif dif >= 0 > dea:
                mom_word = "DIF 上零轴、DEA 仍零下(动能修复中,零下金叉穿越阶段,尚未完全转强)"
            elif dif < 0 and dea < 0:
                mom_word = "DIF/DEA 均在零下(动能偏空)"
            else:
                mom_word = "动能维度数据冲突,以快照为准"
        else:
            mom_word = "MACD 缺失"
        base["一、大盘环境"] = (
            f"程序结论:大盘{rel_txt} M20;动能:{mom_word}。"
            + (f"标的近5日相对大盘:跑输 {abs(float(facts['rs_5d'])):.2f}pp"
               if facts.get("rs_5d") is not None and facts["rs_5d"] < 0
               else f"标的近5日相对大盘:跑赢 {float(facts['rs_5d']):.2f}pp"
               if facts.get("rs_5d") is not None else "标的相对强弱数据缺失")
            + "。本段只能沿用上述结论词,禁止自行重述现价与 M20 的大小比较、"
            "禁止自造'多头加速/势能强'等未给出结论。")
    else:
        base["一、大盘环境"] = "程序事实:大盘数据缺失,本段分析受限。"

    # 二、标的·日线:结论词(现价 vs M20 是压制还是支撑)给死
    ma = d.get("ma") or {}
    m20 = ma.get(20)
    m60 = ma.get(60)
    rel = []
    if m20 is not None:
        rel.append("M20 之上有支撑" if price is not None and price >= m20 else "M20 之上方压制")
    if m60 is not None:
        rel.append("M60 为中期分界" if m60 is not None else "")
    swing_hi = d.get("swing_high")
    swing_dd = etf.get("swing_dd")
    m20_rel = ("为现价下方支撑(现价站上 M20)"
               if m20 is not None and price is not None and price >= m20
               else "为现价上方压制(现价低于 M20)")
    base["二、标的·日线"] = (
        f"程序结论:现价 {_fmt_p(price)},距阶段高点(近250日) {_fmt_p(swing_hi)} 回撤 {swing_dd:.2f}%"
        if swing_hi and swing_dd else f"程序结论:现价 {_fmt_p(price)}") + (
        f";近20日区间 {_fmt_p(d.get('recent_low'))}-{_fmt_p(d.get('recent_high'))},"
        f"现价位于区间 {etf.get('range_pos')}%;日线方向:M20({_fmt_p(m20)}){m20_rel};"
        "KDJ/MACD 从快照标注沿用。不得改上述方向关系。")

    # 三、标的·30分
    m30_ma = m.get("ma") or {}
    m30_20 = m30_ma.get(20)
    m30_5 = m30_ma.get(5)
    base["三、标的·30分"] = (
        f"程序事实:30分 M5={_fmt_p(m30_5)} / M20={_fmt_p(m30_20)},"
        f"近60根区间 {_fmt_p(m.get('range_low'))}-{_fmt_p(m.get('range_high'))};"
        f"30分 KDJ/MACD 见快照标注。30分只作执行刻度与节奏感知,"
        f"不单独生成方向;与日线冲突时必须给出优先级判定。")

    # 四、分时盘口(过滤内外盘,只留量能/五档/折溢价)
    vol_ratio = etf.get("vol_ratio")
    turn = ob.get("turnover_pct")
    tail = mn.get("tail_vol")
    parts = []
    if vol_ratio is not None:
        label = "放量" if vol_ratio >= 1.5 else "缩量" if vol_ratio <= 0.8 else "平量"
        parts.append(f"量比 {vol_ratio}({label})")
    if turn is not None:
        parts.append(f"换手 {turn}%")
    if tail:
        parts.append(tail)
    prem = ob.get("premium_pct")
    # 日内分位(2026-08-29 修复:明确周期,不写裸"中位"引发歧义)
    day_pos = (etf.get("range_pos") or 50)
    hi, lo = mn.get("high"), mn.get("low")
    intraday_pos = dd_zone = None
    if hi and lo and hi > lo:
        intraday_pos = int(round((price - lo) / (hi - lo) * 100))
        dd_zone = ("低位区" if intraday_pos < 30
                   else "高位区" if intraday_pos >= 70 else "中位区")
    pos_txt = (f"日内分位 {intraday_pos}%({dd_zone})" if intraday_pos is not None
               else f"日内高低 {hi}-{lo}")
    # 折溢价定义(2026-08-29 系统修复#②:ETF 无"正股",折溢价是二级价 vs IOPV)
    prem_txt = ""
    if prem is not None:
        prem_txt = (f";折溢价 {prem:+.2f}%(=二级市场价格 vs 基金 IOPV 参考净值的差,"
                    f"ETF 无'正股价'概念)")
    base["四、分时盘口"] = (
        f"程序事实:现价 {_fmt_p(price)},{pos_txt}"
        f";近20日区间位置 {day_pos}%"
        f";{'; '.join(parts) if parts else '量能数据缺失'}"
        + prem_txt
        + "。ETF 内外盘参考性弱(套利机制),本段不做内外盘多空结论。")

    # 五、关键价位分级表 → 程序表格(零 LLM,由 build 直接渲染,不生成 LLM baseline)

    # 六、操作建议与风险提示(合并原八+九;2026-08-29)
    bo = anchors.get("breakout_add")
    bo_src = anchors.get("breakout_src") or "白名单"
    cut = anchors.get("cut_loss")
    stop = anchors.get("stop_loss")
    zone = anchors.get("pullback_zone")
    rb = anchors.get("resist_band") or []
    ztxt = ""
    if zone:
        ztxt = f"回踩带 {_fmt_p(zone.get('lower'))}-{_fmt_p(zone.get('upper'))}"
    base["六、操作建议与风险提示"] = (
        f"程序锚点:锚定价 {_fmt_p(anchor)};关键价位表见前段(突破 {_fmt_p(bo)}({bo_src}) / "
        f"{ztxt or '无回踩结构'} / 减仓 跌破{_fmt_p(cut)} / 清仓止损 {_fmt_p(stop)})。"
        f"状态词:{state.get('state_word','-')}({state.get('sub_state') or '无细节'})。"
        "本段只写:①当前状态一句话;②'什么情况做什么'(触发必须带确认刻度:"
        "放量/日线收盘/30分K线收盘,**'30分K线收盘确认'必须写清确认对象,"
        "如'30分K线收盘站上 {具体价位}或 MACD 金叉确认'**,不得模糊写作'30分K收盘确认时');"
        "③风险提示(缺项/资金面/折溢价);④企稳信号用自然语言三选一"
        "(缩量十字星后放量阳线/日线KDJ低位金叉(J<30 K上穿D)/重新站回日线M20),"
        "**禁止输出内部编号'(如 C8')**。不重复罗列价位表。")

    # 八、资金面(2026-08-29:净值关系由程序给出,禁 LLM 量纲误判 + 滞后标注)
    share = ff.get("share")
    share_chg = ff.get("share_chg")
    scale = ff.get("scale_est")
    nav_note = ""
    if share and scale and float(share) > 0:
        nav = float(scale) / float(share)
        nav_note = (f";净值关系:估算规模 {scale}亿元 / 份额 {share}亿份 ≈ 净值 {nav:.3f}"
                    f"(现价 {_fmt_p(price)} 对照,折溢价 {prem:+.2f}% 表示价格与净值接近)")
    share_date = ff.get("date") or "-"
    base["八、资金面"] = (
        f"程序事实:份额 {share}亿份({share_date} 数据,非最新,资金流判断仅供参考)"
        + (f",较上期 {share_chg:+.2f}亿份" if share_chg is not None else "")
        + (f";估算规模 {scale}亿元" if scale else "")
        + (f";折溢价 {prem:+.2f}%(二级价 vs IOPV)" if prem is not None else "")
        + nav_note
        + "。资金面主信号=份额变化+折溢价(参考 IOPV,非'正股');禁止用'份额'与'规模'"
        "量纲直接比大小推断折溢价(单位不同:亿份 vs 亿元)。")

    return base


# --------------------------------------------------------------------------- 片段 prompt

SEG_GLOBAL_TONE = (
    "本报告核心判断(全局基调):{state_word}(场景 {scenario})。"
    "所有段落解释必须围绕这一基调展开,不得给出追多或追空建议,"
    "不得与「盘面状态」段的确定性结论矛盾。"
)


def segment_prompt(seg_name: str, baseline: str, snapshot_md_filtered: str,
                   facts: dict, state: dict, anchors: dict,
                   conclusion_card_md: str) -> str:
    """单段填充 prompt —— 六要素结构(2026-08-29 用户要求):

    ① 本段结构化 facts(JSON,最小集);
    ② 骨架底线句(不可改数字/方向);
    ③ 允许的解释范围(只写本段主题,禁串段);
    ④ 禁止事项(禁改数字/禁白名单外价位/禁模糊词/禁越级结论);
    ⑤ 全局基调(核心判断,防跨段风格分裂);
    ⑥ 输出长度限制(3-5 句)。
    """
    seg_facts = segment_facts(facts, state, anchors, seg_name)
    scope = _SEG_SCOPE.get(seg_name, "围绕本段主题展开。")
    forbidden = "、".join(_SEG_FORBIDDEN.get(seg_name, [])) or "无"
    extra = _SEG_EXTRA_RULES.get(seg_name, "")
    tone = SEG_GLOBAL_TONE.format(state_word=state.get("state_word", "-"),
                                  scenario=state.get("scenario", "-"))
    return (
        "你是资深 A股中长线 ETF 技术分析研究员。只依据下方【本段事实】输出本段解释,"
        "禁止引用事实之外的任何数字。\n\n"
        "【① 本段事实(JSON,程序精确计算)】\n"
        f"{seg_facts}\n\n"
        f"【② 骨架底线句(数字与方向是确定性事实,禁止修改或自行重算,只准扩展)】\n{baseline}\n\n"
        f"【③ 本段解释范围(只准写这些)】\n{scope}\n\n"
        f"【④ 禁止事项】\n"
        "- 禁止修改/重算底线句中的任何数字与方向,禁止出现本段事实之外的 3 位小数价位;\n"
        f"- 禁止使用: {forbidden}\n"
        "- 禁止模糊词:下轨附近/上轨附近/底部附近/高低点附近/动能积蓄(未标注时);\n"
        f"- 禁止越级结论与指令词(可考虑加仓/立即买入/抄底/满仓){extra};\n"
        f"- 禁止把本 prompt 规则原文写进报告。\n\n"
        f"【⑤ 全局基调(所有解释围绕此展开)】\n{tone}\n\n"
        "【⑥ 输出要求】\n"
        "- 只输出本段正文,3-5 句,可读、有操作含义;不要标题、不要解释行为;\n"
        "- 先复述本段事实(用一句),再给分析和结论。\n"
    )


def assemble_report(segments: dict[str, str], baselines: dict[str, str],
                    program_sections: dict[str, str],
                    category: str) -> str:
    """组装:标题 + LLM 片段(失败段用底线句)+ 纯程序段 + 建议类别。

    2026-08-29 结构优化(用户反馈 #1):五=程序关键价位表(零 LLM),
    六=操作建议与风险提示(LLM 单段,合并原八+九),消除价位重复。
    结论卡不在本函数插入(避免重复)——由 build_tech_analysis 收尾统一插入一次。
    """
    order = ["一、大盘环境", "二、标的·日线", "三、标的·30分", "四、分时盘口",
             "五、关键价位表", "六、操作建议与风险提示", "七、市场状态",
             "八、资金面"]
    lines = ["# 盘中深入分析"]
    for seg in order:
        lines.append(f"## {seg}")
        if seg in segments and segments[seg].strip():
            lines.append(segments[seg].strip())
        elif seg in baselines and baselines[seg].strip():
            lines.append(baselines[seg].strip())
        elif seg in program_sections:
            lines.append(program_sections[seg].strip())
    lines += ["", f"## 建议类别: {category}"]
    return "\n".join(lines)