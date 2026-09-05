# -*- coding: utf-8 -*-
"""个股风险状态机 · 盘中个股模式 P2/P3/P4(2026-09-04;v5 2026-09-05 动作放行)。

方案:docs/盘中个股模式方案-2026-09.md(已获批:量比 1.5/描述性阈值认可/后端放行)。
v5.2 定位(2026-09-05 用户定调):风控工具,只管回撤不管买点 —— 档位→动作
正常持有/关注警惕/风险减仓/严重清仓;「回补」是条件动作,仅当昨日处于风险/严重档
(仓位确被减过)且今日风险全解除时出现一天,不是常规买入指导。回测依据(方案 §14):
双向映射组合MDD改善 29%(§14.1/§14.2);"只减不补"对照臂(§14.6)MDD改善 53%
但收益 +1.1% ≈ 归零——回补建议只在该出现的一天出现,常规建议词表中无买点。

规则与阈值来源标注(方案 §1,改 [已验证] 阈值须先过项目裁决书):
  [已验证] R2 量比 1.5(C5'/v1 break_m20 口径) · R3 近20日低(cut_level) ·
           R4 近250日低(stop_level) · R5 成本 -8%(anchor_stop) ·
           R8 恐慌证据(anchor_entry 恐慌条件,复用为描述) · R9 限价状态(引擎适配层)
  [描述性] R1 M60 参考线 · R5 保本线 0 · R6 深回撤 30% · R7 ATR20% 6%
口径声明:均线/量比基准基于**已收盘 K 线**(缓存),盘中不含今日;量比 = 今日累计量
/20 日均量,盘后为满口径,盘中为累计口径(偏保守),收盘定格后与回测口径一致。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..config import PROJECT_ROOT

logger = logging.getLogger(__name__)

# ---- 阈值常量(方案 §1 定稿;[已验证] 项勿动) ----
R2_VOL_RATIO = 1.5        # [已验证]
R5_COST_STOP = 0.92       # [已验证] -8%
R5_COST_BREAKEVEN = 0.999999  # [描述性] 下穿保本(浮点容差)
R6_DRAWDOWN_PCT = 30.0    # [描述性]
R7_ATR_PCT = 6.0          # [描述性]
R8_PANIC_DROP_PCT = 3.0   # [已验证](恐慌证据口径)
R8_PANIC_VOL = 1.5        # [已验证]

# ---- v3(2026-09-04 用户拍板):自适应层,替代固定阈值 R6/R7 ----
# 出处:唐奇安通道(Turtle/TA-Lib Donchian)、ATR 自身分位体制(Wilder ATR +
# 波动体制常规做法);自归一化=股票只跟自己的历史比,跨价格量纲可比。
R10_WEAK_WATCH = 2        # 近10日 20日通道新破位 ≥2 次 → 关注
R10_WEAK_RISK = 3         # ≥3 次 → 风险
R11_ATR_PCTILE = 90       # ATR20% 自身近一年分位 ≥90 → 高波动体制
R12_P250_MAX = 0.25       # 空头排列 且 现价处250日区间下四分位 → 趋势结构走弱

_LEVELS = ("正常", "关注", "风险", "严重")
_LEVEL_RANK = {"正常": 0, "关注": 1, "风险": 2, "严重": 3}
# 面板三段色语义映射(C15 risk_level 正常/关注/升级)
_PANEL_LEVEL = {"正常": "正常", "关注": "关注", "风险": "升级", "严重": "升级"}

DISCLAIMER = ("个股模式为风险监控与状态提示,不构成收益承诺。"
              "行情可能延迟,以交易所为准;指标口径:前复权。")

# ---------------------------------------------------------------- v5 直接建议(2026-09-05 回测放行)
# 预注册回测(scripts/backtest_stock_risk*.py,方案 §14/§14.1/§14.2):
# c1 组合MDD改善 29%≥15% [过];c2 卖出事件 2443≥30 [过];c3 收益差 -137pp [不过]
# → 2/3 放行;确认臂 v1b 弱势弃用。动作与回测状态机同源,禁止发明映射之外的动作。
# v5.2:正常档的常驻词改为「持有」;「回补」仅在昨日处于风险/严重档(仓位确被减过)
# 且今日风险全解除时出现——与回测映射"半仓→满仓"的条件语义一致,避免健康股
# 每天喊回补污染验证日历(_advice_word)。
_ADVICE_ACTIONS = {
    "正常": ("持有", "正常档,继续持有"),
    "关注": ("警惕", "关注档,保持警惕,映射无动作"),
    "风险": ("减仓", "风险档触发,按映射减仓至半仓"),
    "严重": ("清仓", "大位失守(严重档),按映射清仓"),
}


def _advice_word(level: str, prev_level: str | None) -> tuple[str, str]:
    """档位(+昨日档位)→ 动作。回补是条件动作:仅当昨日被减过仓。
    恢复目标与回测仓位簿同构:昨日风险(应在半仓)→回补至满仓;
    昨日严重(应在空仓)→回补至半仓。"""
    if level == "正常" and prev_level in ("风险", "严重"):
        return (("回补", "风险全解除,按映射回补至满仓") if prev_level == "风险"
                else ("回补", "风险全解除,按映射回补至半仓"))
    return _ADVICE_ACTIONS.get(level, ("观望", ""))

# 规则中文名(2026-09-04 用户硬约束:代号 R\d 只存在于后端/文档,
# 所有用户可见输出一句话/快照/分析/事件一律中文名,见 tests/rules/stock_invariants)
_RULE_NAMES = {"R2": "破位预警", "R3": "减仓位失守", "R4": "大位失守",
               "R5": "成本风控", "R6": "深回撤", "R7": "波动异常",
               "R8": "恐慌证据", "R9": "市场结构",
               "R10": "持续走弱", "R11": "高波动体制", "R12": "趋势结构走弱"}


def _fmt_ts(t) -> str:
    """qt 时戳 20260904161444 → 「09月04日 16:14」(2026-09-04 用户反馈#4)。"""
    s = str(t or "")
    if len(s) >= 12 and s.isdigit():
        return f"{int(s[4:6])}月{int(s[6:8])}日 {s[8:10]}:{s[10:12]}"
    return s


def _fmt_hhmm(t) -> str:
    """qt 时戳 20260904161444 → 「16:14」(仅时间,无日期)。
    2026-09-05 用户要求:刷新快照按钮的时间戳与 ETF 模式(data_at=HH:MM)同格式同样式,
    快照卡内"数据时点"行仍用 _fmt_ts 带日期,两者分工不同。"""
    s = str(t or "")
    if len(s) >= 12 and s.isdigit():
        return f"{s[8:10]}:{s[10:12]}"
    return ""


def _observation_points(ctx: dict) -> list[str]:
    """观察点(程序计算,2026-09-04 用户反馈#10):按距离升序标优先级。
    价位在现价下方 → 「守住(缓冲 x%)」;在上方 → 「收复(需 +x%)」。"""
    price = ctx.get("price")
    if not price:
        return []
    cands = []
    for label, val in (("近20日最低收盘", ctx.get("low20_close")),
                       ("M20", ctx.get("m20")),
                       ("M60", ctx.get("m60")),
                       ("近250日最低收盘", ctx.get("low250_close"))):
        if not val:
            continue
        pct = (val / price - 1) * 100
        if abs(pct) > 15:  # 太远的不算观察点,防噪音
            continue
        if pct < 0:
            cands.append((abs(pct), f"守住{label}({val:.2f},缓冲{abs(pct):.1f}%)"))
        else:
            cands.append((pct, f"收复{label}({val:.2f},需+{pct:.1f}%)"))
    cands.sort(key=lambda x: x[0])
    marks = ["首要", "次要", "再次"]
    return [f"{marks[i]}:{t}" for i, (_, t) in enumerate(cands[:3])]


# ---------------------------------------------------------------- 纯函数(可单测)
def evaluate_stock_risk(ctx: dict) -> dict:
    """v3 分层风险状态机纯函数(2026-09-04 用户拍板方案 §二)。

    层次:L1 趋势结构(排列+250日分位)/L2 持续走弱(唐奇安破位计数)/
    L3 波动体制(ATR 自身分位)/L4 量价事件(R2/R8 已验证内核)/
    L5 成本(R5)/R3/R4 切位(回测口径,不变)。
    代号只存在于本返回值(后端/归档);用户可见文本一律 _RULE_NAMES 中文名。
    ctx 见 build_stock_context;零 IO、零依赖、确定性。
    """
    price = ctx.get("price")
    rules: list[dict] = []          # 命中规则 {id, level, text}
    events: list[str] = []          # 事件(不定档,文本无代号)
    level = "正常"
    if price is None or price <= 0:
        return {"level": "正常", "rules": [], "events": ["无有效行情"],
                "risk_level_panel": "正常"}
    if ctx.get("suspended"):
        # 停牌守卫:量额为零时现价是陈旧价,一切趋势/切位判定无意义
        return {"level": "正常", "rules": [],
                "events": ["疑似停牌(量额为零),暂停风险判定"],
                "risk_level_panel": "正常"}

    def _hit(rid: str, lv: str, text: str, event_only: bool = False) -> None:
        nonlocal level
        if event_only:
            events.append(text)     # v3:事件文本不带代号(用户约束)
            return
        rules.append({"id": rid, "level": lv, "text": text})
        if _LEVEL_RANK[lv] > _LEVEL_RANK[level]:
            level = lv

    m20, m60 = ctx.get("m20"), ctx.get("m60")
    vol_ratio = ctx.get("vol_ratio")
    # R2 [已验证] 破位:价格 < M20 且 累计量比 ≥1.5
    if m20 and price < m20 and (vol_ratio or 0) >= R2_VOL_RATIO:
        _hit("R2", "风险", f"破位M20({m20:.2f})且量比{vol_ratio:.2f}")
    # R3 [已验证] 减仓位失守:价格 < 近20日最低收盘
    if ctx.get("low20_close") and price < ctx["low20_close"]:
        _hit("R3", "风险", f"跌破近20日最低收盘({ctx['low20_close']:.2f})")
    # R4 [已验证] 大位失守:价格 < 近250日最低收盘
    if ctx.get("low250_close") and price < ctx["low250_close"]:
        _hit("R4", "严重", f"跌破近250日最低收盘({ctx['low250_close']:.2f})")
    # R5 [已验证-8%]/[描述性-保本] 成本风控
    cost = ctx.get("cost")
    if cost:
        if price <= cost * R5_COST_STOP:
            _hit("R5", "风险", f"现价较成本{cost:.2f}亏 {((price / cost) - 1) * 100:.1f}%")
        elif price <= cost * R5_COST_BREAKEVEN:
            _hit("R5", "关注", "下穿保本线")
    # R10 [新增-自适应] 持续走弱:近10日 20日通道新破位计数(唐奇安)
    wc = int(ctx.get("weak_count") or 0)
    if wc >= R10_WEAK_RISK:
        _hit("R10", "风险", f"近10日20日通道新破位 {wc} 次(持续走弱)")
    elif wc >= R10_WEAK_WATCH:
        _hit("R10", "关注", f"近10日20日通道新破位 {wc} 次(持续走弱)")
    # R11 [新增-自适应] 高波动体制:ATR20% 处自身近一年分位 ≥90(替代固定 6%)
    atr, apct = ctx.get("atr20_pct"), ctx.get("atr_pctile")
    if atr:
        if apct is not None and apct >= R11_ATR_PCTILE:
            _hit("R11", "关注", f"ATR20% {atr:.1f}%,处自身近一年 {apct:.0f}% 分位"
                                "(高波动体制)")
        elif apct is None and atr >= R7_ATR_PCT:
            _hit("R11", "关注", f"ATR20% {atr:.1f}%(历史分位不足,按固定阈值兜底)")
    # R12 [新增-自适应] 趋势结构走弱:空头排列 且 现价处250日区间下四分位
    m5 = ctx.get("m5")
    p250 = ctx.get("p250_pos")
    if m5 and m20 and m60 and m5 < m20 < m60 and price < m20 \
            and p250 is not None and p250 <= R12_P250_MAX:
        _hit("R12", "关注", f"空头排列且处250日区间下{p250 * 100:.0f}%分位"
                            "(趋势结构走弱)")
    # R8 [已验证-口径] 恐慌证据(事件流)
    if (ctx.get("consec_down") or 0) >= 2:
        _hit("R8", "", f"连{ctx['consec_down']}阴", event_only=True)
    if (ctx.get("pct_chg") or 0) <= -R8_PANIC_DROP_PCT and (vol_ratio or 0) >= R8_PANIC_VOL:
        _hit("R8", "", f"放量长阴({ctx['pct_chg']:.1f}%,量比{vol_ratio:.2f})", event_only=True)
    # R9 [已验证-数据层] 市场结构状态
    if ctx.get("at_limit_down"):
        _hit("R9", "风险", "触及跌停价")
    if ctx.get("at_limit_up"):
        _hit("R9", "", "触及涨停价(注意开板风险)", event_only=True)

    return {"level": level, "rules": rules, "events": events,
            "risk_level_panel": _PANEL_LEVEL.get(level, "正常")}


def _skeleton(ctx: dict) -> dict:
    """确定性骨架结论(v3 数据快照卡的正文;深入分析卡的 LLM 输入)。

    每个模块都是"程序算好的判断句",LLM 只转译不发明——复用 ETF 深入分析
    的骨架-填充架构。纯函数,零 IO。
    """
    sk: dict = {}
    price = ctx.get("price")
    m5, m20, m60 = ctx.get("m5"), ctx.get("m20"), ctx.get("m60")
    # 1) 均线系统
    if m5 and m20 and m60 and price:
        if m5 >= m20 >= m60:
            arrange = "多头排列"
        elif m5 < m20 < m60:
            arrange = "空头排列"
        else:
            arrange = "均线纠缠"
        dist = f"现价{'高于' if price >= m20 else '低于'}20日均线 {abs((price / m20 - 1) * 100):.1f}%"
        sk["ma_system"] = (f"{arrange}(5日 {m5:.2f},20日 {m20:.2f},"
                           f"60日 {m60:.2f}),{dist}")
    # 2) 位置结构(250日区间分位 + 切位缓冲)
    p250 = ctx.get("p250_pos")
    if p250 is not None:
        zone = ("底部区" if p250 <= 0.25 else
                "中低区" if p250 <= 0.5 else
                "中高区" if p250 <= 0.75 else "顶部区")
        sk["position"] = f"现价处近250日收盘区间{p250 * 100:.0f}%分位({zone})"
        if ctx.get("rebound_pct") is not None:
            sk["position"] += f";自区间低点已反弹 {ctx['rebound_pct']:.1f}%"
    low20 = ctx.get("low20_close")
    if low20 and price:
        buf = (low20 / price - 1) * 100
        sk["position"] += (f";距减仓位参考(近20日最低收盘 {low20:.2f})"
                           f"{'缓冲' if buf < 0 else '上方'} {abs(buf):.1f}%")
    # 3) 波动体制
    atr, apct = ctx.get("atr20_pct"), ctx.get("atr_pctile")
    if atr:
        if apct is not None:
            band = ("高波动体制" if apct >= R11_ATR_PCTILE else
                    "低波动体制" if apct <= 25 else "常态波动")
            sk["volatility"] = (f"ATR20% {atr:.1f}%,处自身近一年 {apct:.0f}% 分位"
                                f"({band})")
        else:
            sk["volatility"] = f"ATR20% {atr:.1f}%(历史不足,无分位基准)"
    # 4) 量价状态(四象限,量比1.0为界)
    vr, chg = ctx.get("vol_ratio"), ctx.get("pct_chg")
    if vr is not None and chg is not None:
        quad = (("放量上涨" if chg >= 0 else "放量下跌") if vr >= 1.0
                else ("缩量上涨" if chg >= 0 else "缩量阴跌"))
        sk["volume_price"] = f"{quad}:量比 {vr:.2f},今日 {chg:+.2f}%"
        cd = ctx.get("consec_down") or 0
        if cd >= 2:
            sk["volume_price"] += f";此前连跌 {cd} 日"
        r5 = ctx.get("recent5") or []
        if r5:
            tail_txt = " ".join(f"{d['date'][5:]}收{d['close']}({d['pct_chg']:+.2f}%)"
                                for d in r5)
            sk["volume_price"] += f";近5日已收盘:{tail_txt}"
    # 5) 持续走弱计数(唐奇安事件)
    wc = ctx.get("weak_count")
    if wc is not None:
        sk["weak"] = (f"近10日20日通道新破位 {wc} 次"
                      + ("(持续走弱)" if wc >= R10_WEAK_WATCH else ""))
    return sk


def render_stock_lines(ctx: dict, result: dict) -> tuple[str, str]:
    """一句话(个股版,v5.6.3 两行样式,2026-09-05 用户拍板,对齐 ETF 一句话信号):
    line1 = 名字 现价 · 状态:档位(规则名)——建议由 build_stock_risk_advice 统一追加;
    line2 = 细节链(涨跌幅/均线位置/量比/距成本/事件)。
    去掉【风险监控】前缀;代号不进前端,命中规则用中文名(硬约束不变)。"""
    name = ctx.get("name") or ctx.get("symbol", "")
    price, pct = ctx.get("price"), ctx.get("pct_chg")
    if price is None:
        # 退化形态:保持两行格式且足够独特(不能是"无有效行情"裸词——
        # 那是事件行原文,会撞"快照卡不得重复一句话"守卫,矩阵 F3)
        base = f"{name} " if name else ""
        return f"{base}无有效行情 · 状态:正常", ""
    # line1:状态(+规则名)
    names = "/".join(filter(None, (_RULE_NAMES.get(r["id"]) for r in result["rules"])))
    line1 = f"{name} {price} · 状态:{result['level']}" + (f"({names})" if names else "")
    # line2:细节链
    det: list[str] = []
    if pct is not None:
        det.append(f"{pct:+.2f}%")
    m20 = ctx.get("m20")
    if m20:
        det.append(f"M20{'下' if price < m20 else '上'} {abs((price / m20 - 1) * 100):.1f}%")
    if ctx.get("m60"):  # 反馈#6:M60 与 M20 同格式,都带幅度
        det.append(f"M60{'下' if price < ctx['m60'] else '上'} {abs((price / ctx['m60'] - 1) * 100):.1f}%")
    if ctx.get("vol_ratio") is not None:
        det.append(f"量比{ctx['vol_ratio']:.2f}")
    if ctx.get("cost"):
        det.append(f"距成本{((price / ctx['cost']) - 1) * 100:+.1f}%")
    if result["events"]:
        det.append("事件: " + " / ".join(result["events"]))
    line2 = " · ".join(det)
    return line1, line2


# ---------------------------------------------------------------- 数据上下文
def _engine_cache_path(code: str) -> Path:
    return PROJECT_ROOT / "data" / "kline_cache" / f"{code}_daily.csv"


def _ensure_cache(code: str) -> None:
    """按需补拉(个股 MVP 的引擎数据层复用;alphaprism → backtest_engine 单向依赖)。"""
    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from backtest_engine.stock_fetch import ensure_stock_cache
        ensure_stock_cache(code, start="2019-07-01")
    except Exception as exc:  # noqa: BLE001
        logger.warning("个股缓存补拉失败 %s: %s", code, exc)


def build_stock_context(code: str, snap: dict | None, cost: float | None) -> dict:
    """qt 实时快照 + 已收盘日线指标 → 状态机 ctx(方案 §2 白名单字段)。"""
    ctx: dict = {"symbol": code, "name": (snap or {}).get("name"), "cost": cost}
    if snap:
        for k in ("price", "prev_close", "open", "high", "low", "pct_chg",
                  "volume_hand", "limit_up", "limit_down", "suspended",
                  "at_limit_up", "at_limit_down", "time", "turnover_pct"):
            ctx[k] = snap.get(k)
    # 快照交易日与收盘判定(2026-09-04 反馈#2:指标必须与快照同一交易日,
    # 不能用电脑日期猜——盘后 16:14 快照是今日收盘价,指标却算到昨收,输出自相矛盾)
    t = str(ctx.get("time") or "")
    if len(t) >= 8 and t[:8].isdigit():
        snap_day = f"{t[0:4]}-{t[4:6]}-{t[6:8]}"
        snap_hhmm = t[8:12] if len(t) >= 12 and t[8:12].isdigit() else None
    else:
        snap_day, snap_hhmm = datetime.now().strftime("%Y-%m-%d"), None
    closed = bool(snap_hhmm and snap_hhmm >= "1500")  # 15:00 后 = 当日已收盘
    try:
        path = _engine_cache_path(code)
        if not path.exists():
            _ensure_cache(code)
        df = pd.read_csv(path) if path.exists() else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("个股日线读取失败 %s: %s", code, exc)
        df = None
    if df is not None and closed:
        # 2026-09-04 反馈#2 根治:已收盘但缓存缺快照当日行(引擎"文件在就跳过",
        # 补拉不生效)→ 用 qt 快照的 OHLCV 直接合成当日 K 线(收益价 = 官方收盘价)。
        # 同时停牌日(快照量额为零)绝不合成,保持无当日行的诚实口径。
        try:
            last_day = str(df["trade_date"].iloc[-1])[:10]
            has_today = "trade_date" in df.columns and last_day == snap_day
            live = (not ctx.get("suspended")) and (ctx.get("volume_hand") or 0) > 0
            if not has_today and live and snap_day > last_day:
                row = pd.DataFrame([{
                    "trade_date": snap_day,
                    "open": float(ctx["open"] or ctx["price"]),
                    "high": float(ctx["high"] or ctx["price"]),
                    "low": float(ctx["low"] or ctx["price"]),
                    "close": float(ctx["price"]),
                    "volume": float(ctx["volume_hand"]),
                }])
                df = pd.concat([df[["trade_date", "open", "high", "low", "close", "volume"]],
                                row], ignore_index=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("合成当日K线失败 %s: %s", code, exc)
    if df is not None and len(df) >= 6:
        # 已收盘口径:盘中(未收盘)剔除当日未走完行;已收盘则保留当日行
        # (该行已是完整收盘K线,剔除反而导致指标与快照差一个交易日)
        if ("trade_date" in df.columns and len(df)
                and str(df["trade_date"].iloc[-1]).startswith(snap_day) and not closed):
            df = df.iloc[:-1]
        # 切位基准(R3/R4 低点、250日峰值)口径 = 截至"上一已收盘交易日",
        # 与回测引擎 cut_level 一致:今天收盘与昨收低点比较才有意义,
        # 窗口含当日会让"跌破"自己跟自己比。均线/ATR 仍用含今日的全窗口。
        base = df
        if closed and "trade_date" in df.columns and len(df) \
                and str(df["trade_date"].iloc[-1])[:10] == snap_day:
            base = df.iloc[:-1]
        base_close = base["close"].astype(float)
        close = df["close"].astype(float)
        vol = df["volume"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([df["high"].astype(float) - df["low"].astype(float),
                        (df["high"].astype(float) - prev_close).abs(),
                        (df["low"].astype(float) - prev_close).abs()], axis=1).max(axis=1)
        n = len(df)
        ctx["kline_through"] = str(df["trade_date"].iloc[-1])[:10] \
            if "trade_date" in df.columns else None
        ctx.update({
            "m5": round(close.tail(5).mean(), 3),
            "m20": round(close.tail(20).mean(), 3) if n >= 20 else None,
            "m60": round(close.tail(60).mean(), 3) if n >= 60 else None,
            # R2 量比分母 = 20日均量,窗口截至昨收(今日量 ÷ 昨收窗口均量,口径稳定)
            "vol20_hand": float(base["volume"].astype(float).tail(20).mean())
            if len(base) >= 5 else None,
            # 切位基准窗口 = 截至"上一已收盘交易日"(与回测引擎 cut_level 一致)
            "low20_close": float(base_close.tail(20).min()) if len(base) >= 20 else None,
            "low250_close": float(base_close.tail(250).min()),
            "peak250": float(base_close.tail(250).max()),
            "atr20_pct": round(float(tr.tail(20).mean()) / float(close.iloc[-1]) * 100, 2)
            if n >= 21 else None,
            "consec_down": int(0),
        })
        cd = 0
        for i in range(n - 1, 0, -1):
            if close.iloc[i] < close.iloc[i - 1]:
                cd += 1
            else:
                break
        ctx["consec_down"] = cd
        if ctx.get("vol20_hand") and ctx.get("volume_hand") is not None:
            ctx["vol_ratio"] = round(ctx["volume_hand"] / ctx["vol20_hand"], 2)
        # ---- v3 自适应层(用户拍板 2026-09-04) ----
        # L2 持续走弱:近10个交易日内,收盘首次跌破"此前20日最低收盘"的事件数
        # (唐奇安通道破位事件计数,封顶4;每起=从通道上方向下方穿越,不重复计同一段)
        try:
            roll20 = close.rolling(20).min().shift(1)
            breach = (close < roll20) & (close.shift(1) >= roll20.shift(1))
            ctx["weak_count"] = int(min(4, breach.tail(10).sum()))
        except Exception:
            ctx["weak_count"] = 0
        # L3 波动体制:当前 ATR20% 在自身近一年(250交易日)分布中的分位(0-100)
        try:
            atr_ser = (tr.rolling(20).mean() / close * 100).dropna()
            if len(atr_ser) >= 30:
                ctx["atr_pctile"] = round(
                    float((atr_ser.tail(250) < atr_ser.iloc[-1]).mean() * 100), 0)
            else:
                ctx["atr_pctile"] = None
        except Exception:
            ctx["atr_pctile"] = None
        # L1 位置结构:现价在近250日收盘区间 [最低,最高] 的分位(0-1)
        _lo250, _hi250 = float(base_close.tail(250).min()), float(base_close.tail(250).max())
        _px = ctx.get("price")
        ctx["p250_pos"] = round((_px - _lo250) / (_hi250 - _lo250), 3) \
            if _px and _hi250 > _lo250 else None
        # 恢复力:自近250日最低收盘的反弹幅度(%,描述性)
        ctx["rebound_pct"] = round((_px / _lo250 - 1) * 100, 1) \
            if _px and _lo250 else None
        # 近 5 个已收盘交易日(风险解读 LLM 的时间线素材;口径与指标一致)
        tail = df.tail(5)
        _pc = close.pct_change() * 100
        ctx["recent5"] = [
            {"date": str(d)[:10], "close": float(c), "pct_chg": round(float(p), 2)}
            for d, c, p in zip(tail["trade_date"], tail["close"], _pc.tail(5))
        ] if "trade_date" in df.columns else []
        if closed and len(recent5 := ctx.get("recent5") or []) >= 2:
            # 快照收盘日 ≠ K线最后一日 → 快照可能停牌/停发,诚实标注防混用
            if ctx["recent5"][-1]["date"] != snap_day:
                ctx["snap_lag_note"] = (
                    f"行情快照为 {snap_day} 数据,K 线最新为 {ctx['recent5'][-1]['date']}"
                    "(可能停牌或数据源延迟)")
    return ctx


# ---------------------------------------------------------------- 编排(快照入口)
def fetch_holdings_cost(code: str) -> float | None:
    """持仓成本(方案 §8-Q2:db holdings 表与 ETF 同源,C16 同款懒导入模式)。"""
    from ..db import connect, init_db

    try:
        conn = connect()
        init_db(conn)
        try:
            row = conn.execute("SELECT cost FROM holdings WHERE symbol=?",
                               (code,)).fetchone()
        finally:
            conn.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("持仓成本读取失败 %s: %s", code, exc)
        return None


def _prev_risk_state(code: str, today: str) -> dict | None:
    """读上一交易日归档的风险状态(signal_log kind='stock_risk')。
    返回 {date, level, names:[中文名]};None = 首次监控/无历史。"""
    from ..db import connect, init_db

    try:
        conn = connect()
        init_db(conn)
        try:
            row = conn.execute(
                "SELECT trade_date, signal, state FROM signal_log "
                "WHERE symbol=? AND kind='stock_risk' AND trade_date<? "
                "ORDER BY trade_date DESC LIMIT 1", (code, today)).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读上一日风险归档失败 %s: %s", code, exc)
        return None
    if not row:
        return None
    ids = [s.strip() for s in str(row[1] or "").split(",")
           if s.strip() and s.strip() != "R0"]
    names = [n for n in (_RULE_NAMES.get(i) for i in ids) if n]
    return {"date": str(row[0]), "level": str(row[2] or "正常"), "names": names}


def _trajectory_md(ctx: dict, result: dict, prev: dict | None) -> str:
    """状态轨迹行:昨日档位(中文名) → 今日持平/恶化/好转。"""
    if not prev:
        return "**状态轨迹**: 无前日归档,今日为首个监控日"
    d = prev["date"]
    try:
        _y, _m, _d2 = d.split("-")
        d_short = f"{int(_m)}/{int(_d2)}"
    except Exception:
        d_short = d
    names = "/".join(prev["names"]) or "无规则"
    _rank_y = _LEVEL_RANK.get(prev["level"], 0)
    _rank_t = _LEVEL_RANK.get(result["level"], 0)
    cmp = "持平" if _rank_y == _rank_t else ("恶化" if _rank_t > _rank_y else "好转")
    return (f"**状态轨迹**: 昨日({d_short}):{prev['level']}({names})"
            f" → 今日:{cmp}")


def build_stock_risk_advice(code: str, cfg=None,
                            now: datetime | None = None) -> dict:
    """个股快照总入口。返回与 build_deterministic_advice 同构的 adv dict,
    供 app.py 路由分支以相同 JSON 形状返回(方案 §2 同构原则,前端零改动)。
    v5(2026-09-05 放行):档位→动作(正常回补/关注观望/风险减仓/严重清仓)。"""
    from ..fetchers.stock_qt import fetch_stock_snapshot

    now = now or datetime.now()
    snap = fetch_stock_snapshot([code]).get(code)
    cost = fetch_holdings_cost(code)
    ctx = build_stock_context(code, snap, cost)
    result = evaluate_stock_risk(ctx)
    line1, line2 = render_stock_lines(ctx, result)
    # v3 快照卡:免责 + 状态轨迹 + 骨架(不再重复一句话状态行——L1 卡已有)
    sk = _skeleton(ctx)
    # v5.4:昨日档位按"数据交易日"(qt 时戳日期)查,周六刷新时昨日=周五,
    # 与归档归属(trade_date=数据交易日)同一口径。
    t0 = str(ctx.get("time") or "")
    today_key = (f"{t0[:4]}-{t0[4:6]}-{t0[6:8]}"
                 if len(t0) >= 8 and t0.isdigit() else now.strftime("%Y-%m-%d"))
    prev = _prev_risk_state(code, today_key)
    traj = _trajectory_md(ctx, result, prev)
    md = (f"> {DISCLAIMER}\n\n{traj}\n\n" + _facts_table(ctx, result, sk))
    state = {"state_word": result["level"], "scenario": "个股风险监控"}
    # v5 直接建议:档位→动作;「回补」仅在昨日被减过仓且今日风险全解除时出现。
    # v5.6.3:建议 ALWAYS 上行1(与状态同行,用户拍板的两行样式);持有/警惕也显示。
    action, action_why = _advice_word(result["level"], (prev or {}).get("level"))
    directional = action in ("减仓", "清仓", "回补")
    line1 = f"{line1} · 建议:{action}"
    # 悬浮面板同构载荷(markerWord/markerTitle/markerCls 消费 status_word/risk/anchors;
    # 个股模式结构性无操作价位档,anchors 仅在方向性动作时挂当日锚定价)。
    hhmm = _fmt_hhmm(ctx.get("time")) or now.strftime("%H:%M")
    panel = {"status_word": result["level"], "risk": result["risk_level_panel"],
             "updated_at": hhmm,
             "anchors": ({"anchor_price": ctx.get("price")} if directional else {})}
    return {
        "facts": {**ctx, "now": _fmt_hhmm(ctx.get("time"))
                  or now.strftime("%H:%M")},
        "state": state,
        "catalyst": {"risk_level": result["risk_level_panel"]},
        "category": action,
        "advice_md": (f"## 建议类别: {action}({result['level']}档)\n\n{action_why}。"
                      f"\n\n> {DISCLAIMER}\n"),
        "degraded": False,
        # v5.6.3 两行样式:one_sentence 用「line1。line2」组装,前端 splitSentence
        # 按。拆分 → 第一行=状态+建议,第二行=细节链(与 ETF 一句话信号同构)。
        "card": {"one_sentence": (f"{line1}。{line2}" if line2 else line1),
                 "signal_type": "risk_monitor",
                 "trigger": {}, "invalidation": {}},
        "one_sentence": f"{line1}。{line2}" if line2 else line1,
        "one_sentence_event": line2,   # v5.6.3:第二行 = 细节链(事件并入)
        "signal_type": "risk_monitor",
        "risk": result,
        "panel": panel,
        "markdown": md,
    }


def _facts_table(ctx: dict, result: dict, skeleton: dict | None = None) -> str:
    r"""快照卡 v3(2026-09-04 用户拍板):数据快照服务于分析。

    - 删:与一句话结论重复的状态行;与左侧行情表重复的 现价/昨收/今开/高低/
      换手/涨跌停/累计量——基础数据只留行情界面看不到的指标层;
    - 加:确定性骨架(均线系统/位置结构/波动体制/量价状态/持续走弱)——
      这是"分析性增量",表格上没有;
    - 代号 R\d 一律不出现(用户硬约束),命中规则用中文名。
    """
    if skeleton is None:
        skeleton = _skeleton(ctx)
    lines: list[str] = []
    if ctx.get("snap_lag_note"):
        lines += [f"> {ctx['snap_lag_note']}", ""]
    # ---- 骨架(分析性增量,每个模块一句程序结论) ----
    sections = (
        ("趋势结构", skeleton.get("ma_system")),
        ("位置结构", skeleton.get("position")),
        ("波动体制", skeleton.get("volatility")),
        ("量价状态", skeleton.get("volume_price")),
        ("持续走弱", skeleton.get("weak")),
    )
    for title, body in sections:
        if body:
            lines.append(f"- **{title}**: {body}")
    obs = _observation_points(ctx)
    if obs:
        # 观察点程序计算、按距离升序标优先级(确定性,LLM 不得重排)
        lines.append("\n观察点(程序计算,按优先级):")
        lines += [f"- {o}" for o in obs]
    if result["rules"]:
        lines.append("\n命中规则(取最高档):")
        lines += [f"- **{_RULE_NAMES.get(r['id'], r['id'])}**[{r['level']}] {r['text']}"
                  for r in result["rules"]]
    if result["events"]:
        lines.append("\n当日事件:")
        lines += [f"- {e}" for e in result["events"]]
    # ---- 基础数据(只留行情界面没有的字段) ----
    lines += ["", "**基础数据**\n", "| 字段 | 值 |", "|---|---|"]
    base_rows = [
        ("5日/20日/60日均线", " / ".join(str(ctx.get(k) or "-") for k in ("m5", "m20", "m60"))),
        ("近20日最低收盘(减仓位参考)", f"{ctx.get('low20_close') or '-'}"),
        ("近250日最低/最高收盘", f"{ctx.get('low250_close') or '-'} / {ctx.get('peak250') or '-'}"),
        ("ATR20%(近20日日均波动幅度)",
         f"{ctx.get('atr20_pct') or '-'}" + (f"(近一年{ctx['atr_pctile']:.0f}%分位)"
                                             if ctx.get("atr_pctile") is not None else "")),
        ("量比(今日累计量÷20日均量)",
         f"{ctx.get('vol_ratio') if ctx.get('vol_ratio') is not None else '-'}"),
        ("持仓成本", f"{ctx.get('cost') or '未录入(在「设置→持仓卡」录入后开启止损提醒)'}"),
        ("数据时点", _fmt_ts(ctx.get("time")) or "-"),
        ("指标截至", ctx.get("kline_through") or "-"),
    ]
    lines += [f"| {k} | {v} |" for k, v in base_rows]
    return "\n".join(lines)


# ---------------------------------------------------------------- v4 深入分析增量层(2026-09-04 深夜 II,用户拍板:复用 ETF 深入分析架构)
# 现状问题:深入分析卡=快照卡文字化,零增量。复刻 ETF 架构的三件套:
# 多周期矛盾定性(日线×30分) + 证据天平(程序枚举双侧证据,LLM 只做权衡)
# + 情景推演映射(if-then 前瞻条件,程序算好)。LLM 从"转译骨架"升级为"推演"。

def _classify_day_path(open_gap: float | None, close_vs_open: float,
                       fade: float, recover: float, rng_pct: float) -> str:
    """今日盘中路径分类(纯函数)。输入均为百分比:
    open_gap=开盘相对昨收;close_vs_open=收盘相对开盘;fade=收盘相对日内高点
    (负值=回落幅度);recover=收盘相对日内低点(正值=回升幅度);rng_pct=日内振幅。
    判别核心:开盘缺口区分"单边"与"V形";单边先行判定,再判冲高/探底形态。"""
    if close_vs_open >= 1.5 and fade >= -0.5 and (open_gap is None or open_gap >= -0.3):
        return "单边走高"
    if close_vs_open <= -1.5 and recover <= 0.5 and (open_gap is None or open_gap <= 0.3):
        return "单边走低"
    if close_vs_open >= 0.5 and recover >= 1.5:
        return "探底回升"
    if close_vs_open <= -0.5 and fade <= -1.5:
        return "冲高回落"
    return "宽幅震荡" if rng_pct >= 3.0 else "窄幅整理"


def _conflict_verdict(daily_kind: str, m30_bull: bool | None) -> tuple[str, str, list[str]]:
    """多周期矛盾定性(纯函数)。返回 (类型, 判断句, 候选短语池[供 LLM 选定展开])。
    ETF 深入分析"开篇给一句有判断力的结论"的个股版——候选句程序生成,
    弱模型只准选定展开,不得自由发明。"""
    if m30_bull is None:
        return ("数据不足", "30分数据不可用,以下按日线单周期推演", [])
    if daily_kind == "bear":
        if m30_bull:
            t = "反抽段"
            return (t, "日线结构仍弱,30分出现修复迹象;反弹能否升级取决于30分能否"
                       "站稳其20单位均线", [f"弱反弹后再回踩、有新低风险",
                                        "反抽确认段,站稳30分20单位线前反弹视为修复",
                                        "日线弱势下的超跌反抽,升级需放量确认"])
        t = "共振走弱"
        return (t, "日线与30分同向走弱,多周期共振下行", ["多周期共振走弱,反弹均视为出货机会",
                                                    "弱势共振,关键位失守风险大于修复机会"])
    if daily_kind == "bull":
        if m30_bull:
            t = "共振走强"
            return (t, "日线与30分同向上行,多周期共振向上", ["多周期共振向上,回调均视为良性整理"])
        t = "回调段"
        return (t, "日线结构向好,30分回调;30分收复其20单位均线前回调未结束",
                ["上升趋势中的技术性回调,30分收复20单位线则回调结束",
                 "日线强、30分弱,短期节奏放缓不改结构"])
    if m30_bull:
        return ("周期纠缠", "日线纠缠,30分偏强;以关键位得失为准", ["方向纠缠,20日低得失定短期方向"])
    return ("周期纠缠", "日线纠缠,30分偏弱;以关键位得失为准", ["方向纠缠,20日低得失定短期方向"])


def _evidence_balance(ctx: dict, m30: dict | None) -> tuple[list[str], list[str]]:
    """证据天平(纯函数):程序枚举走弱侧/企稳侧证据清单。LLM 的任务=权衡两侧,
    不是复述——每条证据的数字全部来自 ctx/m30,可溯源。"""
    weak: list[str] = []
    firm: list[str] = []
    price = ctx.get("price")
    m5, m20, m60 = ctx.get("m5"), ctx.get("m20"), ctx.get("m60")
    vr, chg = ctx.get("vol_ratio"), ctx.get("pct_chg")
    wc = ctx.get("weak_count") or 0
    if wc:
        weak.append(f"近10日20日通道新破位{wc}次(持续走弱计数)")
    if m5 and m20 and m60 and m5 < m20 < m60:
        weak.append("日线空头排列(5日<20日<60日)")
    if m20 and price and price < m20:
        weak.append(f"价格低于M20 {abs((price / m20 - 1) * 100):.1f}%")
    if vr is not None and chg is not None and vr < 1 and chg >= 0:
        weak.append(f"缩量上涨(量比{vr:.2f}<1,反弹无量)")
    if (ctx.get("consec_down") or 0) >= 2:
        weak.append(f"此前连跌{ctx['consec_down']}日")
    apct = ctx.get("atr_pctile")
    if apct is not None and apct >= R11_ATR_PCTILE:
        weak.append(f"高波动体制(ATR自身分位{apct:.0f}%)")
    if chg is not None and chg >= 0:
        firm.append(f"今日止跌({chg:+.2f}%)")
    low20 = ctx.get("low20_close")
    if low20 and price and price > low20:
        firm.append(f"仍站在近20日最低收盘{low20}上方,缓冲{abs((low20 / price - 1) * 100):.1f}%")
    low250 = ctx.get("low250_close")
    if low250 and price and price > low250:
        firm.append(f"远离近250日最低收盘{low250}({abs((price / low250 - 1) * 100):.0f}%),大位未失守")
    if vr is not None and chg is not None and vr < 1 and chg < 0:
        firm.append("缩量阴跌(抛压边际减轻)")
    if m30:
        if m30.get("above_m20"):
            firm.append(f"30分价格站上其20单位均线{m30['m20']}(短周期修复)")
        else:
            weak.append(f"30分价格在其20单位均线{m30['m20']}下方(短周期仍弱)")
        if m30.get("path") == "冲高回落":
            weak.append("日内冲高回落(上方抛压可见)")
        if m30.get("path") == "探底回升":
            firm.append("日内探底回升(低位有承接)")
        if m30.get("tail") == "走弱":
            weak.append("尾盘30分走弱")
        elif m30.get("tail") == "回升":
            firm.append("尾盘30分回升")
    return weak, firm


def _scenarios(ctx: dict, m30: dict | None) -> list[str]:
    """情景推演映射(纯函数):前瞻 if-then 条件 → 状态/档位/动作如何变化。
    全部用已验证规则与价位;动作词与 §14 预注册回测映射同源。"""
    out: list[str] = []
    price = ctx.get("price")
    low20, low250 = ctx.get("low20_close"), ctx.get("low250_close")
    m20 = ctx.get("m20")
    if low20 and price:
        if price > low20:
            out.append(f"若收盘跌破 {low20}(近20日最低收盘):确认「减仓位失守」,档位升至风险档,"
                       "按映射减仓至半仓;走弱计数同步累积")
        else:
            out.append(f"若收盘收复 {low20}(需+{abs((low20 / price - 1) * 100):.1f}%)"
                       f"且收复 M20、走弱计数降至3以下:风险全解除,按映射回补至满仓")
    if low250 and price and price > low250:
        out.append(f"若收盘跌破 {low250}(近250日最低收盘):触发「大位失守」,进入严重档,"
                   "按映射清仓")
    if m20 and price and price < m20:
        out.append(f"若收盘收复 {m20}(M20,需+{abs((m20 / price - 1) * 100):.1f}%):"
                   "价格回到20日线上方,空头排列与走弱计数难再累积")
    cost = ctx.get("cost")
    if cost and price and price > cost * R5_COST_STOP:
        out.append(f"若收盘跌破 {round(cost * R5_COST_STOP, 2)}(成本-8%):触发「成本风控」风险档")
    if m30:
        if m30.get("above_m20"):
            out.append(f"若30分跌破其20单位均线 {m30['m20']}:日内修复迹象失效,"
                       "反抽定性降级")
        else:
            out.append(f"若30分收复其20单位均线 {m30['m20']}:短周期修复启动,"
                       "需观察能否延续至收盘")
    return out


def _m30_context(code: str) -> dict | None:
    """30 分钟多周期层(ETF 深入分析架构;失败返回 None,不阻塞分析)。"""
    try:
        from ..fetchers.etf_kline import fetch_30m
        df = fetch_30m(code, count=80)
    except Exception as exc:  # noqa: BLE001
        logger.warning("30分钟线拉取失败 %s: %s", code, exc)
        return None
    if df is None or df.empty or len(df) < 12:
        return None
    try:
        close = df["close"].astype(float)
        last = float(close.iloc[-1])
        m5_30 = round(float(close.tail(5).mean()), 2)
        m20_30 = round(float(close.tail(20).mean()), 2)
        day = str(df["ts"].iloc[-1])[:10]
        today = df[df["ts"].astype(str).str[:10] == day]
        prev = df[df["ts"].astype(str).str[:10] < day]
        prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
        day_open = float(today["open"].iloc[0])
        day_high = float(today["high"].max())
        day_low = float(today["low"].min())
        day_close = float(today["close"].iloc[-1])
        open_gap = (day_open / prev_close - 1) * 100 if prev_close else None
        fade = (day_close / day_high - 1) * 100          # 收盘距日内高点(负=回落)
        recover = (day_close / day_low - 1) * 100        # 收盘距日内低点(正=回升)
        rng = (day_high - day_low) / prev_close * 100 if prev_close else 0.0
        path = _classify_day_path(open_gap, (day_close / day_open - 1) * 100,
                                  fade, recover, rng)
        tail = "走平"
        if len(today) >= 3:
            tail_chg = (float(today["close"].iloc[-1]) / float(today["close"].iloc[-3]) - 1) * 100
            tail = "走弱" if tail_chg <= -0.5 else ("回升" if tail_chg >= 0.5 else "走平")
        return {"day": day, "last": round(last, 2), "m5": m5_30, "m20": m20_30,
                "above_m20": last >= m20_30, "path": path, "tail": tail,
                "open_gap": round(open_gap, 2) if open_gap is not None else None,
                "fade": round(fade, 2), "recover": round(recover, 2),
                "day_high": day_high, "day_low": day_low, "day_open": day_open}
    except Exception as exc:  # noqa: BLE001
        logger.warning("30分钟层计算失败 %s: %s", code, exc)
        return None


def _deep_skeleton(ctx: dict, result: dict, m30: dict | None) -> str:
    """深入分析骨架(v4):快照骨架(现状) + 多周期定性 + 证据天平 + 情景推演。
    与 ETF 深入分析同构:程序给确定性结论,LLM 只做权衡与推演,不发明事实。"""
    price = ctx.get("price")
    m5, m20, m60 = ctx.get("m5"), ctx.get("m20"), ctx.get("m60")
    daily_kind = ("bear" if (m5 and m20 and m60 and m5 < m20 < m60)
                  else "bull" if (m5 and m20 and m60 and m5 >= m20 >= m60) else "mixed")
    vtype, verdict, cands = _conflict_verdict(daily_kind, m30["above_m20"] if m30 else None)
    weak, firm = _evidence_balance(ctx, m30)
    scen = _scenarios(ctx, m30)
    lines: list[str] = []
    lines.append("## 一、多周期矛盾定性(程序判定,候选句供选定)")
    lines.append(f"- 定性类型:{vtype}")
    lines.append(f"- 判断句:{verdict}")
    if cands:
        lines.append("- 候选短语(选一条展开):" + " / ".join(cands))
    if m30:
        lines.append(f"- 30分层({m30['day']}):今日路径「{m30['path']}」"
                     f"(开盘{'高开' if (m30['open_gap'] or 0) >= 0 else '低开'}"
                     f"{abs(m30['open_gap'] or 0):.1f}%,收盘距日内高点{m30['fade']:+.1f}%,"
                     f"距日内低点{m30['recover']:+.1f}%),尾盘{m30['tail']};"
                     f"30分5单位均线 {m30['m5']},20单位均线 {m30['m20']},"
                     f"现价{'上方' if m30['above_m20'] else '下方'}")
    else:
        lines.append("- 30分层数据不可用,按日线单周期推演")
    lines.append("")
    lines.append("## 二、证据天平(程序枚举,全部可溯源)")
    lines.append("- 走弱侧:" + (";".join(weak) if weak else "(无)"))
    lines.append("- 企稳侧:" + (";".join(firm) if firm else "(无)"))
    lines.append("")
    lines.append("## 三、情景推演映射(程序生成的前瞻条件,if-then)")
    if scen:
        lines += [f"- {s}" for s in scen]
    else:
        lines.append("- (关键位数据不足,无映射)")
    lines.append("")
    obs = _observation_points(ctx)
    lines.append("## 四、核心观察点(唯一,程序按距离排序取首要)")
    lines.append(f"- {obs[0] if obs else '(无)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- A 档:风险解读型深入分析(2026-09-04,用户拍板)
# 机制复用深入分析 pipeline(LLM 层/校验-打回/降级链),骨架换个股风险语义。
# v5(2026-09-05 回测放行):风险/严重档的动作词(减仓/清仓)转为"必须出现且不得发明"——
# 校验从"禁操作词"反转为"动作词必达+其余操作词仍禁"(幻觉校验保留)。

_FORBIDDEN_ADVICE = ("加仓", "买入", "卖出", "建仓", "补仓", "目标价",
                     "止盈", "止损价", "抄底", "追高")
# 「建议」只在建议性句式里违规;免责语境(不构成…建议)与程序动作表述(建议:减仓)放行
_SUGGEST_RE = r"(?<!不构成)(?<!不包含)(?<!不提供)(?<!不含有)建议(?:你|您|可|考虑|立即|在|逢|买|卖|加|建|是|关注)"
_PRICE_TOL = 0.005          # 0.5% 容差:LLM 常写 89.21 vs facts 89.20
_PCT_TOL = 0.15             # 百分比绝对值容差


def _numbers_in_text(text: str) -> list[float]:
    """提取"价格级"数字(小数且绝对值 ≥3;带 % 的百分比走独立通道)。
    整数(M20/250日/近5日 等周期词)不检——它们不是价位;
    2026-09-04 补丁:1 位小数也检(此前 894.9 类"编造换算值"漏网)。"""
    import re
    nums = []
    for m in re.finditer(r"\d+\.\d{1,}", text):
        if text[m.end():m.end() + 1] == "%":
            continue  # 百分比在 _validate_stock_analysis 里单独抽取校验
        v = float(m.group(0))
        if abs(v) >= 3.0:
            nums.append(v)
    return nums


def _pct_numbers_in_text(text: str) -> list[float]:
    """抽取带符号百分比(-4.29%);同时矩阵断言池侧已收绝对值形式。"""
    import re
    return [float(m.group(1)) for m in re.finditer(r"(-?\d+(?:\.\d{1,})?)\s*%", text)]


def _stock_analysis_prompt(ctx: dict, result: dict, line1: str) -> str:
    """深入分析 prompt(v4):骨架化快照 + 深入骨架(多周期/证据天平/情景推演)。
    LLM 任务=权衡与推演,不是复述——复用 ETF 深入分析"骨架-填充"架构。"""
    sk = _skeleton(ctx)
    fact_rows = _facts_table(ctx, result, sk)
    m30 = ctx.get("_m30")
    # 位置关系直接给结论(LLM 沿用,不得反向):程序算好,防解读方向幻觉
    price = ctx.get("price") or 0
    pos = []
    if ctx.get("m20"):
        pos.append(f"现价{'高于' if price > ctx['m20'] else '低于'}M20({ctx['m20']})")
    if ctx.get("m60"):
        pos.append(f"{'高于' if price > ctx['m60'] else '低于'}M60({ctx['m60']})")
    if ctx.get("peak250"):
        pos.append(f"距250日高{ctx['peak250']}回撤{(1 - price / ctx['peak250']) * 100:.1f}%")
    pos_line = (";".join(pos) + "。") if pos else "(无行情)"
    prev = _prev_risk_state(ctx.get("symbol") or "", datetime.now().strftime("%Y-%m-%d"))
    traj = _trajectory_md(ctx, result, prev).replace("**", "")
    m30 = ctx.get("_m30")
    deep = _deep_skeleton(ctx, result, m30)
    m30_line = (f"30分今收 {m30['last']},20单位均线 {m30['m20']},现价在其"
                f"{'上方' if m30['above_m20'] else '下方'},日内路径「{m30['path']}」,尾盘{m30['tail']}。"
                if m30 else "30分数据不可用,按日线单周期推演。")
    # v5:动作指令由程序给定(档位+昨日档位→动作映射),LLM 只做转译与依据阐述
    action, action_why = _advice_word(result["level"], (prev or {}).get("level"))
    directional = action in ("减仓", "清仓", "回补")
    action_line = (f"今日档位「{result['level']}」,程序动作:「{action}」——{action_why}。"
                   f"报告结论必须原样包含一行动作结论:「程序建议:{action}」"
                   f"(这是校验锚点,逐字一致,不得改写),并给出依据(引骨架证据)。"
                   if directional else
                   f"今日档位「{result['level']}」,程序动作:「{action}」——{action_why}。"
                   f"报告结论须说明当前为观察档、映射无动作。")
    return (
        "你是 A 股个股多周期推演员(ETF 深入分析架构的个股版)。基于下方"
        "【结构化结论】与【深入分析骨架】(程序计算的确定性结论,事实部分不可更改)"
        "输出一份深入分析报告。你的任务不是复述骨架,而是做四件骨架做不到的事:"
        "①从【深入分析骨架·二】两侧证据中做权衡——判断哪侧占优、为何占优"
        "(必须两侧都提,只引骨架已有数字);②把【深入分析骨架·一】的定性类型与"
        "候选短语选定一条展开成有判断力的开篇结论(不得自造第四种说法);"
        "③按【深入分析骨架·三】把每条 if-then 推演成连贯段落(条件与结论照骨架,"
        "只补逻辑连接,不得新增价位或条件);④转译【动作指令】——动作词由程序给定,"
        "你只解释其依据与失效条件,严禁更改动作词或发明映射之外的动作。\n"
        "定位:档位→动作映射经历史回测放行(2026-09 方案§14),建议词由程序给出,"
        "这是产品机制,不是措辞偏好。\n"
        "输出结构(纯 markdown,五节;小节标题用三级标题,依次为:"
        "多周期定性 / 证据权衡 / 情景推演 / 核心观察点 / 结论;只写标题词本身,"
        "严禁把本条说明或破折号后的解释文字抄进报告):\n"
        "- 多周期定性:开篇一句有判断力的结论(从候选短语选一展开),"
        "然后写日线与30分各自状态及两者的关系;\n"
        "- 证据权衡:走弱侧与企稳侧各自最有力的证据是什么、天平倒向哪边、为什么"
        "(数字只准引骨架,两侧都必须出现);\n"
        "- 情景推演:把骨架第三节的条件逐条转成连贯推演(若…则…),"
        "条件价位与结论严禁改动;\n"
        "- 核心观察点:开篇第一句照抄【深入分析骨架·四】的核心观察点原文"
        "(价位与距离严禁改动),随后展开它为何是当前最关键的位置;\n"
        "- 结论:第一行原样给出动作结论锚点「程序建议:{action}」"
        "(校验锚点,逐字一致,不得改写),随后一句核心依据(引骨架证据)"
        "与一句失效条件(出现什么信号时本结论作废、应重新评估);\n"
        "统一用词(写作时遵守,但本行不要出现在报告里):"
        "近20日最低收盘、近250日最低收盘、M20(20日均线)、M60(60日均线)、"
        "量比(今日累计量÷20日均量)、30分20单位均线、高波动体制、减仓位失守、"
        "大位失守、成本风控、持续走弱。\n"
        "硬纪律:\n"
        f"- 动作词唯一性:全文只允许出现程序动作「{action}」;禁止出现:"
        "加仓/买入/卖出/建仓/补仓/目标价/止盈/抄底/追高 等任何其他操作词,"
        f"不得建议「{action}」以外的任何动作;\n"
        "- 禁止自造数字:报告中的价格与百分比必须能在【结构化结论】或"
        "【深入分析骨架】中找到(允许 0.5% 四舍五入差);\n"
        "- 位置关系必须与程序判定同向(见【位置关系】),「收复/站稳/跌破」"
        "等动词只允许用于与当前实际位置一致的方向,假设句(若跌破/若收复)除外;\n"
        "- 不得更改程序判定的风险档位、规则触发与否与动作映射;情景推演是前瞻假设,不是预测;\n"
        "- 严禁对价位做加法或换算出新价格;距离只用百分比表述;\n"
        "- 不预测明日涨跌,不编造公告/消息/资金面(骨架里没有的一律不写)。\n\n"
        f"【标的名】{ctx.get('name')}\n"
        f"【动作指令(程序判定,必须转译,严禁更改)】{action_line}\n"
        f"【确定性一句话】{line1}\n"
        f"【状态轨迹(程序判定)】{traj}\n"
        f"【位置关系(程序判定,必须沿用,严禁反向表述)】{pos_line}\n"
        f"【30分层(程序判定)】{m30_line}\n\n"
        f"【今日行情】{datetime.now().strftime('%Y-%m-%d')}:"
        f"现价 {ctx.get('price')}({(ctx.get('pct_chg') or 0):+.2f}%),"
        f"当日高低 {ctx.get('high')}/{ctx.get('low')},已收盘。\n\n"
        f"【结构化结论(程序判定,骨架)】\n{fact_rows}\n\n"
        f"【深入分析骨架(程序判定;事实唯一来源)】\n{deep}\n"
    )


def _validate_direction(text: str, ctx: dict) -> tuple[bool, str]:
    """位置方向一致性:与程序判定的均线/峰值位置关系反向即拒。"""
    import re
    price = ctx.get("price")
    if not price:
        return True, ""
    for name, val in (("M20", ctx.get("m20")), ("M60", ctx.get("m60"))):
        if not val:
            continue
        above = price > val
        up_pat = rf"{name}\s*(?:均线)?\s*(?:上方|之上)"
        dn_pat = rf"{name}\s*(?:均线)?\s*(?:下方|之下)"
        if above and re.search(dn_pat, text):
            return False, f"现价高于{name},报告却称位于其下方(方向与程序判定相反)"
        if not above and re.search(up_pat, text):
            return False, f"现价低于{name},报告却称位于其上方(方向与程序判定相反)"
        # 「收复」语义 = 当前在其下方后回升;价格已在上方时用它属方向错误
        if above and re.search(rf"收复\s*{name}", text):
            return False, f"现价已高于{name},「收复」表述方向相反"
        # 「跌破」作已发生陈述仅当价格确在其下方;假设句(若跌破)不检
    if ctx.get("low250_close") and price and price > ctx["low250_close"] \
            and re.search(r"跌破.{0,8}(?:250日|年线)低|已(?:经)?跌破近?\s*250\s*日低", text):
        return False, "现价未跌破 250 日低,报告作已跌破陈述"
    # 低收盘切位:「收复」语义 = 当前在下方;现价在其上方时用「收复」即方向错误
    for label, val in (("近20日最低收盘", ctx.get("low20_close")),
                       ("近250日最低收盘", ctx.get("low250_close"))):
        if val and price > val and re.search(rf"收复[^。]{{0,12}}{label}", text):
            return False, f"现价已高于{label},「收复」表述方向相反"
    # v4 30分层:30分20单位均线的位置关系反向即拒
    m30 = ctx.get("_m30")
    if m30 and price:
        above = price > m30["m20"]
        near = r"30分[^。]{0,12}20单位均线[^。]{0,4}"
        if above and re.search(near + r"(?:下方|之下)", text):
            return False, "现价高于30分20单位均线,报告却称位于其下方(方向相反)"
        if not above and re.search(near + r"(?:上方|之上)", text):
            return False, "现价低于30分20单位均线,报告却称位于其上方(方向相反)"
    return True, ""


def _validate_stock_analysis(text: str, ctx: dict, result: dict) -> tuple[bool, str]:
    """校验(v5 反转):①操作词白名单(只允许程序动作词;方向性档位动作词必达)
    ②编造价位检测(数字须可溯源到 facts) ③方向一致性
    ④prompt 泄漏(结构说明/术语表被原样抄进输出)。返回 (是否通过, 原因)。
    不通过由调用方打回一次,再败降级。"""
    # 规则中文名("减仓位失守")与程序术语("减仓位参考")里的"减仓"是专有名词,
    # 先摘除再检操作动词——只拦真正的操作性用法
    stripped = (text.replace("减仓位失守", "")
                .replace("减仓位参考", "").replace("减仓位", ""))
    level = (result or {}).get("level") if result else None
    # v5.2:动作词与 prompt 同源(档位+昨日档位);正常档昨日被减过 → 回补,
    # 否则持有。无 prev(单测/无归档)时自然退化为常驻词。
    try:
        _prev = _prev_risk_state(str(ctx.get("symbol") or ""),
                                 datetime.now().strftime("%Y-%m-%d"))
    except Exception:  # noqa: BLE001
        _prev = None
    action, _why = _advice_word(level, (_prev or {}).get("level"))
    for w in _FORBIDDEN_ADVICE:
        if w in stripped:
            return False, f"出现动作映射之外的操作词「{w}」"
    # 动作结论用结构锚点「程序建议:X」检查:情景推演正文里合法地会出现下一档的
    # "若跌破…清仓"字样,裸词检查会误杀;锚点形式由 prompt 强制、校验器把关。
    if action in ("减仓", "清仓", "回补") and f"程序建议:{action}" not in text:
        return False, f"缺少程序动作结论「程序建议:{action}」"
    for w in ("减仓", "清仓", "回补"):
        if w != action and f"程序建议:{w}" in text:
            return False, f"出现动作映射之外的动作结论「程序建议:{w}」"
    import re
    m = re.search(_SUGGEST_RE, text)
    if m:
        return False, f"出现建议性表述「…{text[max(0, m.start() - 4):m.end() + 6]}…」"
    ok, why = _validate_direction(text, ctx)
    if not ok:
        return False, why
    # ④ prompt 泄漏:输出里不得再现 prompt 的说明性原文
    for leak in ("术语表(统一用词", "不得改动优先级", "以程序判定为准,不得升级",
                 "直接引用", "今日行情只认", "严禁反向表述", "复述最高风险档",
                 "候选短语(选一条展开", "程序判定,候选句供选定", "任务不是复述骨架",
                 "程序枚举,全部可溯源", "程序生成的前瞻条件",
                 "动作词由程序给定", "你只解释其依据与失效条件",
                 "这是产品机制,不是措辞偏好"):
        if leak in text:
            return False, f"输出泄漏了 prompt 说明文字「{leak}…」"
    # 编造价位检测:报告中出现的价格级数字(≥3位数或带两位小数)须能在 facts 表溯源
    facts_nums = []
    for v in (ctx.get("price"), ctx.get("prev_close"), ctx.get("open"),
              ctx.get("high"), ctx.get("low"), ctx.get("m20"), ctx.get("m60"),
              ctx.get("m5"), ctx.get("low20_close"), ctx.get("low250_close"),
              ctx.get("peak250"), ctx.get("cost"),
              ctx.get("limit_up"), ctx.get("limit_down")):
        if v:
            facts_nums.append(float(v))
    for d in ctx.get("recent5", []):
        facts_nums.append(float(d["close"]))
    pct_pool = {(ctx.get("pct_chg") or 0), (ctx.get("atr20_pct") or 0),
                (ctx.get("vol_ratio") or 0)}
    for d in ctx.get("recent5", []):
        pct_pool.add(d["pct_chg"])
    # 派生百分比(距均线/成本/峰值/切位的距离):LLM 合法引用的算术结果,允许出现。
    # 回撤天然写作正数,而 (price/base-1) 为负 → 正负两种形式都进池;
    # 骨架以 .0f 显示时模型会照抄舍入值(14% → "14.0%"),整数舍入形式也进池
    price = ctx.get("price")
    if price:
        for base in (ctx.get("m20"), ctx.get("m60"), ctx.get("peak250"),
                     ctx.get("cost"), ctx.get("low20_close"), ctx.get("low250_close")):
            if base:
                derived = round((price / base - 1) * 100, 2)
                pct_pool.update({derived, abs(derived),
                                 float(round(derived)), float(round(abs(derived)))})
        if ctx.get("prev_close"):
            pct_pool.add(float(ctx.get("pct_chg") or 0))
    # v3 骨架新增的百分比量:分位/体制/反弹幅度(LLM 引用它们是转译,不是编造)
    if ctx.get("p250_pos") is not None:
        pct_pool.update({round(ctx["p250_pos"] * 100, 1), round(100 - ctx["p250_pos"] * 100, 1)})
    if ctx.get("atr_pctile") is not None:
        pct_pool.update({round(ctx["atr_pctile"], 1), round(100 - ctx["atr_pctile"], 1)})
    if ctx.get("rebound_pct") is not None:
        pct_pool.add(float(ctx["rebound_pct"]))
    # v4 30分多周期层的数字与百分比(LLM 引用它们是转译骨架,不是编造)
    m30 = ctx.get("_m30")
    if m30:
        facts_nums += [m30.get("last"), m30.get("m5"), m30.get("m20"),
                       m30.get("day_high"), m30.get("day_low"), m30.get("day_open")]
        pct_pool.update({m30[k] for k in ("open_gap", "fade", "recover")
                         if m30.get(k) is not None})
    # 观察点里的"需 +x% / 缓冲 x%"是程序算好的合法百分比
    for o in _observation_points(ctx):
        pct_pool.update(_pct_numbers_in_text(o))
    # LLM 常把负涨幅写成口语正数("跌了 4.29%"),池同时收绝对值形式
    pct_pool |= {round(abs(q), 2) for q in pct_pool if q is not None}
    for num in _numbers_in_text(text):
        ok_price = any(abs(num / p - 1) <= _PRICE_TOL for p in facts_nums if p)
        ok_pct = any(abs(num - q) <= _PCT_TOL for q in pct_pool if q is not None)
        if not (ok_price or ok_pct):
            return False, f"数字 {num} 无法溯源到事实卡片(疑自造)"
    for q in _pct_numbers_in_text(text):
        ok_pct = any(abs(q - p) <= _PCT_TOL for p in pct_pool if p is not None)
        if not ok_pct:
            return False, f"百分比 {q}% 无法溯源到事实卡片(疑自造)"
    return True, ""


def _llm_stock_analysis(ctx: dict, result: dict, line1: str, cfg) -> tuple[str | None, str]:
    """LLM 风险解读(复用深入分析 pipeline 的 LLM 层)。返回 (md, degrade原因)。
    30分多周期层在 build_stock_analysis 统一拉取,挂 ctx['_m30'] 传给 prompt。"""
    prompt = _stock_analysis_prompt(ctx, result, line1)
    why = ""
    for _ in (1, 2):  # 一次不过打回重生成,再败降级
        out = _llm_chat_safe(prompt, cfg)
        if out is None:
            return None, "LLM 调用失败"
        ok, why = _validate_stock_analysis(out, ctx, result)
        if ok:
            return out, ""
        prompt = (prompt + f"\n\n【上一次输出被拒】原因:{why}。"
                  "请重写:动作词只用程序给定的那一个,其余数字只使用骨架中已有的。")
    return None, f"校验未通过({why});已打回一次仍失败"


def _llm_chat_safe(prompt: str, cfg) -> str | None:
    try:
        from .intraday import _llm_chat
        return _llm_chat(prompt, cfg, max_tokens=1800, temperature=0.35)
    except Exception as exc:  # noqa: BLE001
        logger.warning("个股风险解读 LLM 失败: %s", exc)
        return None


def _stock_analysis_fallback(ctx: dict, result: dict, line1: str, why: str) -> str:
    """降级链(v4):LLM 不可用/校验不过 → 深入分析骨架直出
    (多周期定性/证据天平/情景推演都是确定性内容,降级不丢深度)。
    v5.1:结尾「边界」改为「结论」——直接给出动作结论与失效条件,不留空话。
    v5.2:动作词与 build_stock_risk_advice 同源(_advice_word,含回补条件)。"""
    try:
        prev = _prev_risk_state(str(ctx.get("symbol") or ""),
                                datetime.now().strftime("%Y-%m-%d"))
    except Exception:  # noqa: BLE001
        prev = None
    action, action_why = _advice_word(result["level"], (prev or {}).get("level"))
    if action in ("减仓", "清仓"):
        concl = (f"程序建议:{action}({action_why})。\n"
                 f"失效条件:风险全解除(收复近20日最低收盘与 M20、走弱计数<3)"
                 f"或触发更严重档位时,以程序下一次判定为准。")
    elif action == "回补":
        concl = ("程序建议:回补(风险全解除,按映射回补至满仓)。\n"
                 "失效条件:再度触发风险档或更严重档位时,以程序下一次判定为准。")
    elif action == "持有":
        concl = ("程序建议:持有(正常档,继续持有)。\n"
                 "失效条件:升档至关注/风险/严重档时,以程序下一次判定为准。")
    else:
        concl = (f"程序建议:{action}({action_why})。\n"
                 "失效条件:档位升降至风险/严重或风险证据全部消退时,"
                 "以程序下一次判定为准。")
    md = (f"> 深入分析降级为确定性骨架(原因:{why})。"
          f"档位→动作映射经回测放行,建议词由程序给出。\n\n"
          f"**{line1}**\n\n"
          + _deep_skeleton(ctx, result, ctx.get("_m30"))
          + "\n\n### 结论\n" + concl
          + "\n本卡由程序计算,零 LLM;LLM 推演恢复后自动切换。")
    return md


def build_stock_analysis(code: str, cfg=None,
                         now: datetime | None = None) -> tuple[str, bool, str]:
    """个股深入分析入口(A 档风险解读)。返回 (md, degraded, data_at)。
    确定性层(快照/状态机)先跑完,LLM 只做解读;失败/违规降级确定性风险卡。"""
    from ..fetchers.stock_qt import fetch_stock_snapshot

    now = now or datetime.now()
    snap = fetch_stock_snapshot([code]).get(code)
    cost = fetch_holdings_cost(code)
    ctx = build_stock_context(code, snap, cost)
    result = evaluate_stock_risk(ctx)
    line1, _ = render_stock_lines(ctx, result)
    # v4:30分多周期层统一拉取一次(网络 IO 只发生在这里,prompt/校验/降级共用)
    ctx["_m30"] = _m30_context(code)
    md, why = _llm_stock_analysis(ctx, result, line1, cfg)
    # data_at 与 ETF 同格式(HH:MM,仅时间;刷新按钮/深入分析卡头共用渲染)
    data_at = _fmt_hhmm(ctx.get("time")) or now.strftime("%H:%M")
    if md is None:
        return (_stock_analysis_fallback(ctx, result, line1, why), True, data_at)
    return (md, False, data_at)


# ---------------------------------------------------------------- P4 归档
def archive_stock_risk(code: str, adv: dict, now: datetime | None = None) -> None:
    """风险状态归档 → signal_log(kind='stock_risk',每日每标的一条,最新为准)。

    v5.2(2026-09-05 用户要求个股在验证日历可见):**每日留痕** —— 所有档位写
    advice_archive 一行(与 ETF 每日一档同构);方向性档位(减仓/清仓/回补)带
    anchor_price 走验证(anchor_price=当日快照价,N日应验由同一验证器回填),
    持有/观望行无方向,日历按"观望·不验证"灰虚线留痕展示(复用 ETF 语义)。
    v5.4 归属修正(2026-09-05 用户规则):trade_date = **快照数据所属交易日**
    (qt 时戳的日期部分),不是墙钟日期——与 ETF(facts.date)同构:周六刷新
    → 数据时戳是周五 16:14 → 归入周五;同日多次刷新 DELETE+INSERT 最后赢。
    """
    from ..db import connect, init_db

    now = now or datetime.now()
    result = adv["risk"]
    t = str((adv.get("facts") or {}).get("time") or "")
    if len(t) >= 8 and t.isdigit():
        trade_date = f"{t[:4]}-{t[4:6]}-{t[6:8]}"     # qt 时戳日期 = 数据交易日
    else:
        trade_date = now.strftime("%Y-%m-%d")         # 兜底:无时戳才用墙钟
    conn = connect()
    init_db(conn)
    try:
        conn.execute(
            "DELETE FROM signal_log WHERE trade_date=? AND symbol=? AND kind='stock_risk'",
            (trade_date, code))
        conn.execute(
            "INSERT INTO signal_log (trade_date, symbol, kind, signal, state, price, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (trade_date, code, "stock_risk",
             ",".join(r["id"] for r in result["rules"]) or "R0",
             result["level"], adv["facts"].get("price"), adv["one_sentence"]))
        conn.execute(
            "DELETE FROM advice_archive WHERE trade_date=? AND symbol=?",
            (trade_date, code))
        conn.execute(
            "INSERT INTO advice_archive (trade_date, symbol, created_at,"
            " anchor_price, scenario, state_word, risk_level, category,"
            " advice_md, snapshot_md, degraded) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (trade_date, code, now.strftime("%H:%M"),
             (adv.get("panel") or {}).get("anchors", {}).get("anchor_price"),
             "个股风险监控", result["level"],
             (adv.get("catalyst") or {}).get("risk_level"), adv.get("category"),
             adv.get("advice_md"), adv.get("markdown") or "", 0))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("个股风险归档失败 %s: %s", code, exc)
    finally:
        conn.close()
