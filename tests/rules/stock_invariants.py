# -*- coding: utf-8 -*-
r"""个股规则不变量断言(合成矩阵用)——镜像 tests/rules/invariants.py 架构。

不变量来源:
- 《交易系统设计》校验七条思想(单调性/白名单/距离约束);
- 2026-09-04 用户拍板 v3 方案(自适应层) + 用户硬约束(代号 R\d 不进前端)。
任何违反 = 规则漏洞,须补洞(发现问题-补漏洞-又发现问题 循环的断路器)。
"""
from __future__ import annotations

import re

VALID_LEVELS = {"正常", "关注", "风险", "严重"}
VALID_PANEL = {"正常", "关注", "升级"}
# 用户可见输出(一句话/快照/分析/事件/观察点)一律不得出现规则代号。
# \b 词边界防误伤:ATR20/RR 之类不算;ATR20 中 R 前是 T(词字符),不构成边界
CODE_RE = re.compile(r"\bR\d{1,2}\b")


def check_level_invariants(ctx: dict, result: dict) -> list[str]:
    """档位合法性:白名单/面板映射/规则档位合法。"""
    out = []
    lv = result.get("level")
    if lv not in VALID_LEVELS:
        out.append(f"档位 '{lv}' 不在白名单 {sorted(VALID_LEVELS)}")
    pl = result.get("risk_level_panel")
    if pl not in VALID_PANEL:
        out.append(f"面板档 '{pl}' 不在映射 {sorted(VALID_PANEL)}")
    for r in result.get("rules", []):
        if r.get("level") not in VALID_LEVELS:
            out.append(f"规则 {r.get('id')} 档位 '{r.get('level')}' 非法")
    # 面板映射一致性
    expect = {"正常": "正常", "关注": "关注", "风险": "升级", "严重": "升级"}
    if lv in expect and pl != expect[lv]:
        out.append(f"面板映射不一致: level={lv} 应得 {expect[lv]},实得 {pl}")
    return out


def check_hard_rule_levels(ctx: dict, result: dict) -> list[str]:
    """已验证硬规则的档位契约(口径不可漂移)。停牌/无行情守卫优先。"""
    out = []
    if ctx.get("suspended"):
        # 停牌守卫是合法的"跳过判定",不做漏报检查
        return out
    ids = {r["id"]: r for r in result.get("rules", [])}
    price = ctx.get("price")
    if price is None:
        return out
    low20 = ctx.get("low20_close")
    if low20 is not None and price < low20:
        if "R3" not in ids:
            out.append("价格跌破近20日最低收盘但 R3 未触发(漏报)")
        elif ids["R3"]["level"] != "风险":
            out.append("R3 档位应为 风险")
    else:
        if "R3" in ids and low20 is not None:
            out.append("价格未破 20 日低但 R3 触发(误报)")
    low250 = ctx.get("low250_close")
    if low250 is not None and price < low250:
        if "R4" not in ids:
            out.append("价格跌破近250日最低收盘但 R4 未触发(漏报)")
        elif ids["R4"]["level"] != "严重":
            out.append("R4 档位应为 严重")
        if result.get("level") != "严重":
            out.append("R4 命中但最高档不是 严重")
    cost = ctx.get("cost")
    if cost and price <= cost * 0.92 and "R5" not in ids:
        out.append("亏损达 -8% 阈值但 R5 未触发(漏报)")
    vr = ctx.get("vol_ratio")
    m20 = ctx.get("m20")
    if m20 and price < m20 and vr is not None and vr >= 1.5 and "R2" not in ids:
        out.append("破位+量比≥1.5 但 R2 未触发(漏报)")
    wc = ctx.get("weak_count")
    if wc is not None:
        if wc >= 3 and ("R10" not in ids or ids["R10"]["level"] != "风险"):
            out.append(f"走弱计数 {wc}≥3 但 R10 不是风险档")
        if wc == 2 and ("R10" not in ids or ids["R10"]["level"] != "关注"):
            out.append(f"走弱计数 2 但 R10 不是关注档")
        if wc < 2 and "R10" in ids:
            out.append(f"走弱计数 {wc}<2 但 R10 触发(误报)")
    apct = ctx.get("atr_pctile")
    if apct is not None:
        if apct >= 90 and "R11" not in ids:
            out.append(f"ATR 分位 {apct}≥90 但 R11 未触发(漏报)")
        if apct < 90 and ctx.get("atr20_pct") is not None and "R11" in ids:
            out.append(f"ATR 分位 {apct}<90 且有分位基准但 R11 触发(误报)")
    if ctx.get("suspended") and (result.get("rules") or result.get("level") != "正常"):
        out.append("停牌状态下仍触发风险规则(陈旧价判定,漏洞)")
    if ctx.get("price") is None:
        return out
    if ctx.get("at_limit_down") and "R9" not in ids:
        out.append("触及跌停但 R9 未触发(漏报)")
    return out


def check_no_codes_frontend(ctx: dict, result: dict, markdown: str | None = None) -> list[str]:
    r"""用户硬约束:一句话/事件行/快照卡 中不得出现 R\d 代号。"""
    out = []
    from alphaprism.planner.stock_risk import render_stock_lines
    line1, line2 = render_stock_lines(ctx, result)
    for label, text in (("一句话", line1), ("事件行", line2)):
        m = CODE_RE.search(text or "")
        if m:
            out.append(f"{label} 出现代号 '{m.group(0)}': {text[:80]}")
    if markdown:
        m = CODE_RE.search(markdown)
        if m:
            out.append(f"快照卡出现代号 '{m.group(0)}'")
        if line1 and line1.strip("*") in markdown:
            out.append("快照卡重复了一句话结论(应只出现在 L1 卡)")
    for r in result.get("rules", []):
        m = CODE_RE.search(r.get("text") or "")
        if m:
            out.append(f"规则文本出现代号 '{m.group(0)}': {r['text'][:60]}")
    return out


def check_observation_invariants(ctx: dict) -> list[str]:
    """观察点:≤3 条、优先级序(距离升序)、守住/收复语义与位置一致。"""
    from alphaprism.planner.stock_risk import _observation_points
    out = []
    obs = _observation_points(ctx)
    if len(obs) > 3:
        out.append(f"观察点 {len(obs)} 条超过上限 3")
    dists = []
    for o in obs:
        m = re.search(r"缓冲|需\+(\d+(?:\.\d+)?)%|缓冲(\d+(?:\.\d+)?)%", o)
        num = re.search(r"(\d+(?:\.\d+)?)%*\)", o)
        d = re.search(r"(?:缓冲|需\+)(\d+(?:\.\d+)?)%", o)
        if not d:
            out.append(f"观察点缺距离标注: {o}")
        else:
            dists.append(float(d.group(1)))
        if o.startswith("首要") is False and obs.index(o) == 0:
            out.append("首条未标'首要'")
    if dists != sorted(dists):
        out.append(f"观察点距离非升序: {dists}(违反优先级排序)")
    # 收复语义方向检查:文本含「收复X」时 X 必须在现价上方
    price = ctx.get("price")
    if price:
        for label, val in (("近20日最低收盘", ctx.get("low20_close")),
                           ("M20", ctx.get("m20")),
                           ("M60", ctx.get("m60"))):
            if val is None:
                continue
            for o in obs:
                if f"收复{label}" in o and price > val:
                    out.append(f"现价已高于{label},观察点却写'收复'(方向错误): {o}")
    return out


def check_state_monotonic(ctx: dict, evaluate) -> list[str]:
    """单调性:其他字段不变,走弱计数升高 → 档位不得下降。"""
    out = []
    prev = None
    for wc in range(0, 5):
        c = dict(ctx)
        c["weak_count"] = wc
        r = evaluate(c)
        rank = {"正常": 0, "关注": 1, "风险": 2, "严重": 3}[r["level"]]
        if prev is not None and rank < prev:
            out.append(f"走弱计数 {wc-1}→{wc} 档位反而下降({prev}→{rank})")
        prev = rank
    return out


def check_all(ctx: dict, result: dict, markdown: str | None = None) -> list[str]:
    out = check_level_invariants(ctx, result)
    out += check_hard_rule_levels(ctx, result)
    out += check_no_codes_frontend(ctx, result, markdown)
    out += check_observation_invariants(ctx)
    return out
