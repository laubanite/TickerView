"""盘中深入分析 · 确定性组件引擎(v2.0 工程化, M1'/M2')。

《交易系统设计》(v0.5) = 规则权威,本模块只实现规则、不定义规则:
- C5' 五级场景 + 布尔状态词(废止 v1.1 计分制)
- C4' 操作参数档位 + 语义校验五条 + 锚定价 + 保本线(动态档)
- C15 催化剂上下文(风险等级三档:正常/关注/升级)
- C16 持仓上下文(持仓卡,读 db holdings/account_meta)
- 建议类别映射(供盘后增量4 方向判定)

全部纯函数(个别读 db),输入 = intraday._load_facts 的快照 facts dict;
渲染/校验/归档由调用方(intraday.py)编排。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- 常量(初值,待 §10 校准)

SPREAD_TH = 0.5            # 均线粘合阈值:极差 < 0.5%(初值,倾向 0.4×ATR20)
VOL_UP_EQUIV = 1.5         # 放量阈值(成交金额口径,占位;外部用 vol_ratio)
PANIC_MAX_ATR_MULT = 1.5   # 恐慌跌幅下限系数
PANIC_MAX_PCT = 1.5        # 恐慌跌幅绝对下限 %
POS_MIN_ATR_MULT = 1.5      # 位置到位距离系数
POS_MIN_PCT = 1.5          # 位置到位距离绝对下限 %
RS_WORSE_PP = 3.0          # 相对强弱恶化阈值 pp

# 状态词白名单(v0.5 §1)
STATE_WORDS = {"区间震荡", "变盘前兆", "突破异动日", "左侧观望", "超跌试多",
               "蓄势", "右侧初现", "右侧确认", "破位退出"}
# 建议类别白名单(盘后增量4 方向映射键)
CATEGORIES = {"加仓", "买入", "试多", "减仓", "砍仓", "清仓", "持有", "观望", "等待确认"}
# 禁词(LLM 不得自造;单日截面不得输出"右侧确认"(跨日由盘后增量1 推进))
BANNED_WORDS = {"右侧确认", "极度超卖", "极度超买", "满仓干", "抄底"}

# 类别 → 预期方向(增量4)
CATEGORY_DIRECTION = {
    "加仓": "up", "买入": "up", "试多": "up",
    "减仓": "down", "砍仓": "down", "清仓": "down",
    "持有": None, "观望": None, "等待确认": None,
}


def _r3(v):
    return round(float(v), 3) if v is not None else None


# --------------------------------------------------------------------------- C5' 五级场景 + 布尔状态词

def _ma_spread(price, ma: dict) -> float | None:
    """M5/M10/M20 极差(相对现价 %)。数据不足返回 None。"""
    vals = [ma.get(p) for p in (5, 10, 20) if ma.get(p) is not None]
    if len(vals) < 3 or not price:
        return None
    return (max(vals) - min(vals)) / price * 100


def _left_qualify(etf: dict, idx: dict) -> dict:
    """超跌试多准入(交易系统设计 §4.2,按快照可得项实现)。

    条件:超跌量化 + 恐慌证据 + 位置到位 + 大盘非动能期。
    性质判定(§4.1)简化为:放量破位即趋势破坏 → 由上层先行判"破位"，不在此重复。
    """
    d = etf.get("daily") or {}
    mn = etf.get("minute") or {}
    price = mn.get("price")
    out = {"oversold": False, "panic": False, "pos_ok": False,
           "index_gate": False, "eligible": False, "missing": []}
    if price is None:
        out["missing"] = ["现价缺失"]
        return out
    # 1) 超跌量化:KDJ J<10 或 距60日高点回撤≥15%(MVP 用近250日 swing_dd 近似)
    j = (d.get("kdj") or {}).get("J")
    if j is not None and j < 10:
        out["oversold"] = True
    elif etf.get("swing_dd") is not None and etf["swing_dd"] >= 15:
        out["oversold"] = True
    else:
        out["missing"].append("未超跌(J≥10 且回撤<15%)")
    # 2) 恐慌证据:单日跌幅 ≥ max(1.5×ATR20, 1.5%) 且放量
    chg = mn.get("change_pct")
    vr = etf.get("vol_ratio")
    atr20 = (d.get("atr20") or 0) * 100 or 0
    panic_min = max(PANIC_MAX_ATR_MULT * atr20, PANIC_MAX_PCT)
    on_volume = vr is not None and vr >= VOL_UP_EQUIV
    if chg is not None and chg <= 0 and (-chg) >= panic_min and on_volume:
        out["panic"] = True
    else:
        out["missing"].append(f"无恐慌证据(需单日跌幅≥{panic_min:.1f}% 且放量)")
    # 3) 位置到位:现价≥近端强支撑,距离 ≤ max(1.5%, 1.5×ATR20)
    sup = d.get("swing_low") or d.get("recent_low")
    pos_min = max(POS_MIN_PCT, POS_MIN_ATR_MULT * atr20)
    if sup is not None and price >= sup and (price - sup) / price * 100 <= pos_min:
        out["pos_ok"] = True
    else:
        out["missing"].append(f"位置未到位(距强支撑 {0 if sup is None else round((price - sup) / price * 100, 2)}% > {pos_min:.1f}%)")
    # 4) 大盘非动能期:大盘放量跌破日线 M20 后的初期禁试多(MVP 简化:当前即禁)
    if idx:
        id_mn = idx.get("minute") or {}
        id_d = idx.get("daily") or {}
        id_price = id_mn.get("price")
        id_m20 = (id_d.get("ma") or {}).get(20)
        id_vr = idx.get("vol_ratio")
        if (id_price is not None and id_m20 is not None and id_price < id_m20
                and id_vr is not None and id_vr >= VOL_UP_EQUIV):
            out["index_gate"] = True
    out["eligible"] = out["oversold"] and out["panic"] and out["pos_ok"] and not out["index_gate"]
    return out


def signal_state(facts: dict) -> dict:
    """C5' 市场状态机:五级场景 + 布尔状态词(全部可复核,无计分)。

    判定顺序 = 交易系统设计 §8.1:破位 → 突破异动日 → 变盘前兆 → 区间震荡 → 趋势。
    """
    etf = facts.get("etf") or {}
    d = etf.get("daily") or {}
    mn = etf.get("minute") or {}
    price = mn.get("price")
    ma = d.get("ma") or {}
    vr = etf.get("vol_ratio")
    on_volume = vr is not None and vr >= VOL_UP_EQUIV
    rng_hi, rng_lo = d.get("recent_high"), d.get("recent_low")
    rp = etf.get("range_pos")
    m20 = ma.get(20)

    if price is None:
        return {"scenario": "数据不足", "state_word": "数据不足",
                "sub_state": None, "spread_pct": None, "on_volume": on_volume}
    # 1) 破位:放量跌破 M20 / 关键位 且性质=趋势破坏(放量破位即破坏,MVP 判定)
    if on_volume and m20 is not None and price < m20:
        return {"scenario": "破位", "state_word": "破位退出", "sub_state": None,
                "spread_pct": _r3(_ma_spread(price, ma)), "on_volume": True}
    spread = _ma_spread(price, ma)
    if spread is None:
        return {"scenario": "数据不足", "state_word": "数据不足",
                "sub_state": None, "spread_pct": None, "on_volume": on_volume}
    if spread < SPREAD_TH:                      # 均线粘合 = 区间震荡家族
        if on_volume:                           # 放量有效动作
            if rng_hi is not None and price >= rng_hi:
                return {"scenario": "突破异动日", "state_word": "右侧初现", "sub_state": "突破异动日",
                        "spread_pct": _r3(spread), "on_volume": True}
            return {"scenario": "变盘前兆", "state_word": "变盘前兆", "sub_state": "蓄势放量",
                    "spread_pct": _r3(spread), "on_volume": True}
        sub = "中部"
        if rp is not None:
            if rp >= 70:
                sub = "上沿收敛"
            elif rp <= 30:
                sub = "下沿测试"
        return {"scenario": "区间震荡", "state_word": "区间震荡", "sub_state": sub,
                "spread_pct": _r3(spread), "on_volume": False}
    # 5) 趋势(均线发散)
    if on_volume and rng_hi is not None and price >= rng_hi:
        return {"scenario": "趋势", "state_word": "右侧初现", "sub_state": "放量突破上沿",
                "spread_pct": _r3(spread), "on_volume": True}
    lq = _left_qualify(etf, facts.get("index"))
    if lq.get("eligible"):
        return {"scenario": "趋势", "state_word": "超跌试多", "sub_state": "超跌试多候选",
                "spread_pct": _r3(spread), "on_volume": on_volume, "left": lq}
    if lq.get("oversold"):
        return {"scenario": "趋势", "state_word": "左侧观望", "sub_state": "超跌待恐慌证据",
                "spread_pct": _r3(spread), "on_volume": on_volume, "left": lq}
    return {"scenario": "趋势", "state_word": "蓄势", "sub_state": None,
            "spread_pct": _r3(spread), "on_volume": on_volume}


# --------------------------------------------------------------------------- C4' v3 参数档位(身份 × 价位 × 位源)

# 校验⑦ 相关:三阈值(交易系统设计 §6.1.3,初值待 §10 校准)
SUPPORT_FAR_MULT = 3.0     # 上沿距现价 > 3×ATR20 → 无有效回踩结构
BAND_WIDTH_MULT = 1.5      # 下沿距上沿 ≤ 1.5×ATR20 才并带(超距 → 单点带)
BAND_TIGHT_MIN = 0.5       # 紧贴下限 %:上沿距现价 ≤ max(0.5%, 0.5×ATR20) → 已到位/带内
BAND_TIGHT_ATR = 0.5

# 位源白名单(交易系统设计 §6.1.2);排序:价格距离优先,类别见各表
_D_SUPPORT_MA = (5, 10, 20, 60)      # 日线均线(支撑类)
_M30_SUPPORT_MA = (5, 10, 20)        # 30分均线(支撑类)


def _classify_sources(d: dict, m: dict, mn: dict, price: float):
    """位源分类(§6.1.2):返回 (支撑类, 结构压力类, 均线压力带, 止损类, 全部下方, 价位→位源名)。

    支撑类:日线 M5/M10/M20/M60、30分 M5/M10/M20、30分近60根区间下沿、日内低点、共振带下沿(占位);
    结构压力类(突破档候选,§3.1 关键位口径):近20日区间上沿、前高平台、共振带上沿(占位)、阶段高点;
    均线压力带(非突破候选,只作关键价位分级表的压力带参考):日线均线上方;
    止损类:250日阶段低、前低平台(以阶段低近似)、近20日区间下沿。
    位源名按先到优先:支撑 > 结构压力 > 止损(同价位多源时,渲染取第一个)。
    """
    support, structure_p, resist_band, stop = [], [], [], []
    dma = d.get("ma") or {}
    mma = (m or {}).get("ma") or {}
    for p, v in dma.items():
        if v is None:
            continue
        if v < price and p in _D_SUPPORT_MA:
            support.append((v, f"日线M{p}"))
        elif v > price:
            resist_band.append((v, f"日线M{p}"))
    for p, v in mma.items():
        if v is None:
            continue
        # 30分均线只作支撑类(回踩带/企稳刻度,§2.1 执行刻度)
        if p in _M30_SUPPORT_MA and v < price:
            support.append((v, f"30分M{p}"))
    if m and m.get("range_low") is not None and m["range_low"] < price:
        support.append((m["range_low"], "30分区间低"))
    if mn.get("low") is not None and mn["low"] < price:
        support.append((mn["low"], "日内低"))
    # 结构压力(§3.1 突破关键位口径;突破档只从这些位产生,均线不作突破档)
    if d.get("recent_high") is not None and d["recent_high"] > price:
        structure_p.append((d["recent_high"], "近20日上沿"))
    if d.get("swing_high") is not None and d["swing_high"] > price:
        structure_p.append((d["swing_high"], "前高平台"))
    if d.get("recent_low") is not None and d["recent_low"] < price:
        stop.append((d["recent_low"], "近20日低"))
    if d.get("swing_low") is not None and d["swing_low"] < price:
        stop.append((d["swing_low"], "阶段低"))
    # 位源名:同价位取先到类别(支撑 > 结构压力 > 止损),保序去重
    labels: dict[float, str] = {}
    for lst in (support, structure_p, resist_band, stop):
        for v, label in lst:
            labels.setdefault(v, label)
    support_d = sorted({v for v, _ in support})
    struct_d = sorted({v for v, _ in structure_p})
    resist_d = sorted({v for v, _ in resist_band})
    stop_d = sorted({v for v, _ in stop})
    return support_d, struct_d, resist_d, stop_d, labels


def param_anchors(facts: dict, holding: dict | None = None) -> dict:
    """C4' v3 操作参数档位:身份 × 价位 × 位源(交易系统设计 §6.1)。

    输出:anchor_price + 四档(压力档/回踩带/减仓/清仓止损)+ 身份词 + 校验七条。
    相比 v2 的变化:
    - 位源扩展:30分均线/日内低/30分区间下沿/前高平台入库;
    - 回踩带区间化(upper/lower)+ 三阈值(3×ATR20 有效 / 1.5×ATR20 并带 / 0.5% 紧贴);
    - 空头/超距无回踩结构 → 只出三档 + no_pullback=True;
    - 清仓止损档 = 最下档,不再缺档。
    """
    etf = facts.get("etf") or {}
    d = etf.get("daily") or {}
    m = etf.get("m30") or {}
    mn = etf.get("minute") or {}
    price = mn.get("price")
    atr20 = d.get("atr20")
    if price is None:
        return {"anchor_price": None, "checks": {}, "ok": False, "reason": "现价缺失"}
    support, struct_p, resist_band, stop_src, labels = _classify_sources(d, m, mn, price)
    down_all = sorted(set(support) | set(stop_src))
    if (not struct_p and not resist_band) or not down_all:
        return {"anchor_price": _r3(price), "checks": {}, "ok": False,
                "reason": "档位候选不足(数据残缺)"}
    # 突破档:结构位优先(§3.1 关键位口径);上方无结构位 → fallback 最近均线(标记反弹压力位)
    if struct_p:
        breakout = struct_p[0]
        breakout_src = labels.get(breakout, "")
        breakout_is_ma = False
    else:
        breakout = resist_band[0]
        breakout_src = labels.get(breakout, "")
        breakout_is_ma = True
    # 回踩带(有结构分支)
    zone = None
    no_pullback = True
    if support:
        upper = support[-1]                                   # 距现价最近支撑 = 上沿
        far = price - upper
        if not (atr20 and far > SUPPORT_FAR_MULT * atr20):    # ≤3×ATR20 → 有效回踩结构
            no_pullback = False
            band_max = (BAND_WIDTH_MULT * atr20) if atr20 else price * 0.01
            lower_cands = [s for s in support if s < upper - 1e-9 and (upper - s) <= band_max]
            lower = max(lower_cands) if lower_cands else upper   # 单点带(下沿=上沿)
            tight_need = max(price * BAND_TIGHT_MIN / 100,
                             (BAND_TIGHT_ATR * atr20) if atr20 else 0)
            zone = {"upper": _r3(upper), "lower": _r3(lower),
                "upper_src": labels.get(upper, ""),
                "lower_src": labels.get(lower, ""),
                # 2026-08-29 分开表述(避免"已到位/带内"混一):带内 = 现价在 [lower,upper];
                # 已到位 = 现价距上沿 ≤ 阈值但尚未/未完全入带(上沿上方不远)。
                "in_zone": bool(lower <= price <= upper),
                "tight": bool(price - upper <= tight_need)}
    # 减仓档 / 清仓止损档
    if no_pullback:
        cut = max(stop_src) if stop_src else max(down_all)    # 无结构:止损类最近档
    else:
        cut = zone["lower"]
    cut_src = labels.get(cut, "")
    # 清仓止损档(v2.8,2026-08-29 用户拍板"止损=回踩带下沿以下第一个止损类"):
    # - 止损类定义(§6.1.2):仅 近20日低 / 阶段低(250日低,前低平台近似)——结构低点,
    #   不含均线/日内低/30分区间低(那些是支撑类,作回踩带候选,性质不同)。
    # - 主选:cut 以下第一个止损类(紧贴减仓档,止损有真实操作意义)。
    # - 防"cut 以下无止损类"(如 250日低 与 cut 之间夹着支撑类):
    #   距 cut 超 SHORTFALL_MULT×ATR20 或 cut 下方无止损类时,
    #   回退 cut 下方最近 down_all(止损∪支撑)作止损——保证止损不过远。
    stop_cands = [s for s in stop_src if s < cut - 1e-9]
    near_down = [v for v in down_all if v < cut - 1e-9]
    # 止损空间下限(校验⑤同款):cut 与止损至少隔 max(0.5%, 1×ATR20)
    real_space = max(price * 0.005, atr20 if atr20 else 0)
    if stop_cands:
        stop = stop_cands[-1]                                   # cut 以下最近止损类
        # 止损类过远(>3×ATR20)→ 回退 cut 下方最近且有真实空间的支撑类
        if atr20 and (cut - stop) > SUPPORT_FAR_MULT * atr20:
            fb = [v for v in near_down if (cut - v) >= real_space - 1e-9]
            stop = fb[-1] if fb else stop                       # 无有效兜底 → 保留止损类
    elif near_down:
        # 无止损类:取 cut 下方最近且有真实空间的位(cut 本身已在下方时跳过)
        fb = [v for v in near_down if (cut - v) >= real_space - 1e-9]
        stop = fb[-1] if fb else min(down_all)                  # 全无空间 → 最下档
    else:
        stop = min(down_all)                                    # 兜底:最下档
    stop_src_name = labels.get(stop, "")
    # 校验七条(交易系统设计 §6.1.4)
    checks = {
        1: bool(breakout > price),                                # 压力档 > 锚定价
        2: zone is None or zone["upper"] <= price,                # 回踩带上沿 ≤ 锚定价
        3: zone is None or zone["lower"] >= cut - 1e-9,           # 下沿 ≥ 减仓档(允许相等)
        4: bool(cut is not None and stop is not None and stop < cut - 1e-9),  # 止损 < 减仓
        5: bool(cut is not None and stop is not None
                and (cut - stop) >= max(price * 0.005, atr20 if atr20 else 0)),  # 止损有真实空间
        6: True,                                                  # 位源类别由构造保证(zone 仅来自支撑类)
        7: bool(breakout and cut and stop) and (not no_pullback or zone is None),  # 完整性按场景
    }
    ok = all(checks.values())
    reason = None if ok else "档位不完整/校验未过(见 checks)"
    # 保本线(动态档):持仓段给综合成本则取,否则 None;不参与静态校验
    breakeven = None
    if holding and holding.get("holdings"):
        sym = str(etf.get("code", "")).split(".")[0]
        for h in holding["holdings"]:
            if str(h.get("symbol", "")).split(".")[0] == sym:
                breakeven = _r3(float(h.get("cost", 0)) or None)
                break
    return {
        "anchor_price": _r3(price),
        "breakout_add": _r3(breakout),
        "breakout_src": breakout_src,
        "breakout_is_ma": breakout_is_ma,          # 突破档是否为均线 fallback(→反弹压力位身份)
        "resist_band": [_r3(v) for v in resist_band],   # 均线压力带(操作参数行的"反弹压力带"参考)
        "resist_band_src": {_r3(v): labels.get(v, "") for v in resist_band},
        "pullback_add": _r3(zone["upper"]) if zone else None,     # 兼容标量(上沿)
        "pullback_zone": zone,
        "no_pullback": no_pullback,
        "cut_loss": _r3(cut),
        "cut_src": cut_src,
        "stop_loss": _r3(stop),
        "stop_src": stop_src_name,
        "breakeven": breakeven,
        "checks": checks,
        "ok": ok,
        "reason": reason,
    }


# --------------------------------------------------------------------------- C15 催化剂上下文(风险等级)

def catalyst_context(facts: dict, state: dict | None = None, cfg=None) -> dict:
    """C15 风险等级三档(正常/关注/升级);只卡开仓/加仓,不碰持有与止损。

    输入:盘前催化状态(db catalyst_status 最近) + 盘中增量标记
    (相对强弱恶化 / 拥挤度警戒 / 破位场景)。MVP:消息/异动标记后置。
    """
    etf = facts.get("etf") or {}
    symbol = str(etf.get("code", "")).split(".")[0]
    idx = facts.get("index")
    flags: list[str] = []
    pre_status = None
    try:
        from ..db import connect, init_db
        conn = connect()
        init_db(conn)
        row = conn.execute(
            "SELECT trade_date, status FROM catalyst_status WHERE symbol=? "
            "ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()
        if row:
            pre_status = str(row["status"])
        srow = conn.execute(
            "SELECT ratio_pct, level FROM sector_turnover WHERE symbol=? "
            "ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()
        conn.close()
        if srow:
            ratio = srow["ratio_pct"] or 0
            if ratio >= 20:
                flags.append("拥挤度强警示")
            elif ratio >= 15:
                flags.append("拥挤度警戒")
    except Exception as exc:  # noqa: BLE001
        logger.warning("催化剂上下文读取失败: %s", exc)
    if pre_status == "减弱":
        flags.append("催化减弱")
    chg = (etf.get("minute") or {}).get("change_pct")
    idx_chg = (idx.get("minute") or {}).get("change_pct") if idx else None
    if chg is not None and idx_chg is not None and (chg - idx_chg) <= -RS_WORSE_PP:
        flags.append("相对强弱恶化")
    scenario = (state or {}).get("scenario")
    if scenario == "破位":
        risk = "升级"
    elif any(f in flags for f in ("拥挤度强警示",)) or scenario == "破位":
        risk = "升级"
    elif flags:
        risk = "关注"
    else:
        risk = "正常"
    return {"pre_status": pre_status, "flags": flags, "risk_level": risk}


# --------------------------------------------------------------------------- C16 持仓上下文

def holding_context(cfg=None) -> dict:
    """C16 持仓上下文:读 db holdings / account_meta。

    派生:综合成本(加权)、单只已占、剩余可用额度(满配=总资金1/3)——
    MVP 只提供 raw;额度/置换计算在调用方或后续 P 系列扩展。
    """
    try:
        from ..db import connect, init_db
        conn = connect()
        init_db(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT symbol, name, cost, quantity, status FROM holdings"
            " ORDER BY symbol").fetchall()]
        meta = {str(r["key"]): str(r["value"]) for r in conn.execute(
            "SELECT key, value FROM account_meta").fetchall()}
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("持仓上下文读取失败: %s", exc)
        rows, meta = [], {}
    return {"holdings": rows, "total_capital": meta.get("total_capital"),
            "cash": meta.get("cash")}


# --------------------------------------------------------------------------- 渲染(快照附加段,程序输出)

def render_state_md(facts: dict, state: dict, anchors: dict,
                    catalyst: dict | None = None, holding: dict | None = None) -> str:
    """数据快照尾部的「盘面状态(程序输出)」段:确定性,LLM 不得修改。"""
    price = (facts.get("etf") or {}).get("minute", {}).get("price")
    lines = ["", "## 盘面状态(程序输出,LLM 不得修改)", "",
             "| 项目 | 值 |", "|---|---|"]
    lines.append(f"| 场景 | {state.get('scenario', '-')} |")
    lines.append(f"| 状态词 | {state.get('state_word', '-')} |")
    sub = state.get("sub_state")
    if sub:
        lines.append(f"| 场景细节 | {sub} |")
    lines.append(f"| 均线极差(M5/M10/M20) | {state.get('spread_pct', '-')}% |")
    lines.append(f"| 锚定价(决策基准) | {_fmt_price(anchors.get('anchor_price'))} |")
    if anchors.get("ok"):
        scenario = state.get("scenario", "")
        no_pb = bool(anchors.get("no_pullback"))
        is_ma_resist = bool(anchors.get("breakout_is_ma"))
        # 身份词(C4' v3,§6.1.1):场景 × 档位类别 → 身份 + 动词白名单
        if scenario == "区间震荡":
            up_identity, pb_identity = "区间上沿", "区间下沿(支撑观察)"
        elif no_pb:
            up_identity, pb_identity = "反弹压力位", "回踩 无结构(空头排列/距支撑>3×ATR20)"
        elif is_ma_resist:
            up_identity, pb_identity = "反弹压力位", "回踩加仓位"
        else:
            up_identity, pb_identity = "突破加仓位", "回踩加仓位"
        parts = [f"{up_identity} {_fmt_price(anchors.get('breakout_add'))}"
                 f"({anchors.get('breakout_src') or '白名单'})"]
        # 反弹压力带(v2.4):突破档=结构位(近20日上沿等)时,日线均线压力带并列展示,
        # 给"短期反弹受限/减仓参考"留过渡位,消除"突破位太远"的突兀(v2.3 审阅)。
        rb = anchors.get("resist_band") or []
        rb_src = anchors.get("resist_band_src") or {}
        if rb:
            rb_txt = "、".join(f"{_fmt_price(v)}({rb_src.get(_r3(v)) or '日线均线'})"
                               for v in rb[:3])
            parts.append(f"反弹压力带 {rb_txt}")
        zone = anchors.get("pullback_zone")
        if zone is not None:
            upper, lower = zone.get("upper"), zone.get("lower")
            if lower is not None and upper is not None and abs(float(lower) - float(upper)) > 1e-9:
                pb_txt = (f"{_fmt_price(lower)}-{_fmt_price(upper)}"
                          f"({zone.get('lower_src') or ''}–{zone.get('upper_src') or ''})")
            else:
                pb_txt = f"{_fmt_price(upper)}({zone.get('upper_src') or '白名单'})"
            # 2026-08-29 分开表述:带内 / 已到位(距上沿 X%)分开,消除歧义
            if zone.get("in_zone"):
                pb_txt += "(带内)"
            elif zone.get("tight"):
                dist = max(float(price or 0) - float(upper or 0), 0)
                dist_pct = dist / float(price or 1) * 100
                pb_txt += f"(已到位,距上沿 {dist_pct:.2f}%)"
            parts.append(f"{pb_identity} {pb_txt}")
        else:
            parts.append(pb_identity)   # "回踩 无结构(...)"
        parts.append(f"减仓 跌破{_fmt_price(anchors.get('cut_loss'))}"
                     f"({anchors.get('cut_src') or '白名单'})")
        if anchors.get("stop_loss") is not None:
            parts.append(f"清仓止损 {_fmt_price(anchors.get('stop_loss'))}"
                         f"({anchors.get('stop_src') or '白名单'})")
        else:
            # 8.24 光伏:止损"-"被 LLM 写成"暂未设定"。缺档必须是显式状态,
            # 禁止 LLM 自造"止损位暂未设定"类自由措辞(校验链 #12 兜底)。
            parts.append("清仓止损 缺档(快照无更低白名单档)")
        lines.append("| 操作参数(程序锚点) | " + " / ".join(parts) + " |")
    else:
        lines.append(f"| 操作参数 | 档位不足: {anchors.get('reason', '数据残缺')} |")
    if anchors.get("breakeven") is not None:
        lines.append(f"| 保本线(动态) | {_fmt_price(anchors.get('breakeven'))} |")
    if catalyst:
        lines.append(f"| 风险等级 | {catalyst.get('risk_level', '正常')} |")
        if catalyst.get("pre_status"):
            lines.append(f"| 盘前催化 | {catalyst['pre_status']} |")
        if catalyst.get("flags"):
            lines.append(f"| 盘中增量标记 | {'、'.join(catalyst['flags'])} |")
    if holding is not None:
        sym = str((facts.get("etf") or {}).get("code", "")).split(".")[0]
        hrow = next((h for h in holding.get("holdings", [])
                     if str(h.get("symbol", "")).split(".")[0] == sym), None)
        if hrow:
            cost, qty = _fmt_price(hrow.get("cost")), hrow.get("quantity")
            chg = None
            price = (facts.get("etf") or {}).get("minute", {}).get("price")
            if price is not None and hrow.get("cost"):
                chg = round((price / float(hrow["cost"]) - 1) * 100, 2)
            line = f"{cost} × {qty} 股"
            if chg is not None:
                line += f"(盈亏 {chg:+.2f}%)"
            lines.append(f"| 持仓 | {line} |")
    return "\n".join(lines)


def _fmt_price(v) -> str:
    return f"{float(v):.3f}" if v is not None else "-"


# --------------------------------------------------------------------------- 校验链(sanitize v2, 三级处理)

def _rewrite_pass(text: str, snapshot_md: str, state: dict, anchors: dict) -> tuple[str, list[str]]:
    """步骤3(v2.10):删句 → 改写。对可确定性改写的违规句,先整句模板改写(信息保留),
    改写后句子必须本身合规(白名单/方位/禁词),未命中改写的原违规句仍由删句链兜底。

    返回 (改写后文本, 改写记录)。改写句不带白名单外价位。
    """
    rew: list[str] = []
    if not text:
        return text, rew
    etf_part = snapshot_md.split("## 大盘")[0]
    # 近20日区间位置档(从快照标注解析;v2.8:中轨区→中上轨/中下轨,不再以"区"结尾)
    band_txt = None
    m = re.search(r"现价位于区间 (\d+)%·(下轨区|上轨区|中上轨|中下轨)", etf_part)
    if m:
        band_txt = f"近20日区间 {m.group(1)}%({m.group(2)})"
    # 日内位置档
    intraday_txt = None
    m = re.search(r"日内分位 (\d+)%\((.+区)\)", etf_part)
    if m:
        intraday_txt = f"日内分位 {m.group(1)}%({m.group(2)})"
    # 突破档(白名单锚点,供"站上 X 加仓"改写)
    breakout = anchors.get("breakout_add")
    state_word = state.get("state_word") or ""

    def rewrite_line(line: str) -> str | None:
        """命中可确定性改写模式 → 返回替换句;否则 None(交给原删句链)。"""
        # 1) 试多越级(状态≠超跌试多 下的"可试多"语)
        if state_word != "超跌试多" and re.search(r"可择机试多|可考虑试多|可试多|择机试多", line):
            return "当前不满足 §4.2 试多准入(缺项见结论卡),以观察为主,不试多"
        # 2) 突破触发无确认词("站上/突破 X 加仓/买入")
        if breakout and re.search(r"(突破|站上)\s*" + re.escape(f"{float(breakout):.3f}")
                                   + r"[^，。；\n]{0,15}(加仓|买入)", line) \
                and not re.search(r"放量|收盘|次日", line):
            return f"放量+日线收盘站上 {float(breakout):.3f}(突破档)才谈右侧初现"
        # 3) 近20日区间位置模糊词
        if band_txt and re.search(r"近20日区间.{0,4}(下轨|上轨)附近|近20日区间.{0,4}底部", line):
            return f"现价位于{band_txt}"
        # 4) 日内高低点附近(无量化)
        if intraday_txt and "高低点附近" in line:
            return f"现价{intraday_txt}"
        # 5) 企稳信号自造 J 类表述
        if re.search(r"企稳", line) and re.search(r"J转负|J开口收窄|J值转负|KDJ转负|J跌破|J跌至", line):
            return ("企稳信号按 C8' 三选一:缩量十字星后放量阳 / 日线KDJ低位金叉(J<30 K上穿D) / "
                    "重新站回日线M20")
        return None

    out = []
    for line in text.splitlines():
        new = rewrite_line(line)
        if new:
            rew.append(f"{line.strip()[:24]} → {new}")
            out.append(new)
        else:
            out.append(line)
    return "\n".join(out), rew


def suggest_category(md: str, state: dict) -> str:
    """建议类别:优先解析报告「建议类别:」行;缺失按状态词映射(白名单)。"""
    m = re.search(r"建议类别[:：]\s*([\w/]+)", md)
    if m:
        raw = m.group(1).strip()
        for c in ("等待确认", "买入", "加仓", "减仓", "砍仓", "清仓", "试多", "观望", "持有"):
            if raw.startswith(c):
                return c
        return "观望"
    state_default = {
        "破位退出": "减仓",
        "超跌试多": "试多",
        "右侧初现": "加仓",
        "变盘前兆": "等待确认",
        "区间震荡": "观望",
        "左侧观望": "观望",
        "蓄势": "等待确认",
        "右侧确认": "加仓",
    }
    return state_default.get(state.get("state_word", ""), "观望")


def sanitize_v2(analysis: str, snapshot_md: str, state: dict,
                anchors: dict, catalyst: dict | None = None) -> tuple[str, bool, list[str]]:
    """M3' 校验链(8 条 → MVP 实现 6 条):三级处理。

    轻:自动替换参数段为程序锚点;违规:删句;致命(价位违规≥2/参数矛盾无法修)→降级。
    返回 (text, degraded, issues)。安全网:任何异常不阻断,返回原文+降级标志。
    """
    issues: list[str] = []
    degraded = False
    text = analysis or ""
    price = anchors.get("anchor_price")
    # 0) 删句 → 改写(步骤3,v2.10):可确定性改写的违规句先整句模板改写(信息保留)
    text, rew_over = _rewrite_pass(text, snapshot_md, state, anchors)
    if rew_over:
        issues += [f"改写(原句删除→模板句): {r}" for r in rew_over]
    # 1) 参数自动修正(轻):LLM 参数段若与程序锚点冲突 → 用锚点行替换
    if anchors.get("ok"):
        fixed = _fix_param_block(text, anchors)
        if fixed != text:
            issues.append("操作参数已替换为程序锚点")
            text = fixed
    # 2) 价位白名单:数字(3位小数)必须存在于快照或锚点档(含锚定价本身)
    valid = set(re.findall(r"\d+\.\d{3}(?!\d)", snapshot_md))
    for v in (anchors.get("anchor_price"), anchors.get("breakout_add"),
              anchors.get("pullback_add"), anchors.get("cut_loss"),
              anchors.get("stop_loss")):
        if v is not None:
            valid.add(f"{float(v):.3f}")
    bad = sorted(p for p in set(re.findall(r"\d+\.\d{3}(?!\d)", text)) if p not in valid)
    if bad:
        issues.append(f"价位不在白名单({'/'.join(bad)})")
        if len(bad) >= 2:
            degraded = True
        else:
            text = _drop_sentences_with(text, bad)
    # 3) 动词方向:上方只准突破/测试,下方只准回踩/跌破/企稳(对照锚定价)
    # v2.10:regex 收紧为"价位形数字"(含小数点的 0-100 区间),避免"回踩30分钟K"被当价位误删。
    if price is not None:
        for verb, pos in re.findall(r"(突破|回踩|跌破|企稳|站上|失守)\s*(\d+\.\d{2,3})", text):
            try:
                v = float(pos)
            except ValueError:
                continue
            if not (0 < v < 100):
                continue                     # 非价位数字(如 30 分钟/5 日窗口)→ 跳过
            bad_dir = ((verb in ("回踩", "跌破", "企稳") and v > price) or
                       (verb in ("突破", "站上", "失守") and v < price))
            if bad_dir:
                issues.append(f"动词方向矛盾:{verb} {pos}(锚定价 {price:.3f})")
                text = _drop_sentence(text, verb, pos)
    # 4) 状态词白名单:禁词 = 自造/越级
    for w in BANNED_WORDS:
        if w in text:
            issues.append(f"禁用状态词:{w}")
            text = _drop_sentences_with(text, [w])
    # 5) 风险等级白名单:只准 正常/关注/升级(风险上升语义,三段式 §4.3)
    for m in re.findall(r"风险等级[:：]\s*([^\n，。；]+)", text):
        rv = m.strip()
        if rv not in ("正常", "关注", "升级"):
            issues.append(f"风险等级越界:{rv}")
            text = _drop_sentences_with(text, [rv])
    # 7) 盘口外内盘结论一致性(8.24 光伏:程序"抛压占优(内盘大)",
    #    LLM 却写"外盘抛压占优但内盘承接尚可"——方向反转 + 自造结论)
    ob_match = re.search(r"外内盘\s*\|\s*(承接占优\(外盘大\)|抛压占优\(内盘大\))", snapshot_md)
    if ob_match:
        concl = ob_match.group(1)
        sell_heavy = "内盘大" in concl   # 抛压占优(内盘大 = 主动卖多于买)
        ban_re = (re.compile(r"外盘[^，。；\n]*?(抛压|占优)|承接[^，。；\n]*?尚可|内盘[^，。；\n]*?承接")
                  if sell_heavy else
                  re.compile(r"内盘[^，。；\n]*?抛压|外盘[^，。；\n]*?承接|承压"))
        kept = [l for l in text.splitlines() if not ban_re.search(l)]
        if len(kept) != len(text.splitlines()):
            issues.append(f"盘口结论与程序相反(程序:{concl}),已删句")
            text = "\n".join(kept)
    # 8) MACD 柱色一致性:LLM 提到的红柱/绿柱必须是快照中出现过的颜色
    #    (防 LLM 把 30分 绿柱写成"红柱/多头"一类的标签错配)
    macd_colors = set(re.findall(r"MACD[^\n]*?\((红柱|绿柱)", snapshot_md))
    if macd_colors:
        for color in set(re.findall(r"(红柱|绿柱)", text)) - macd_colors:
            issues.append(f"MACD 柱色与快照矛盾:快照无{color},已删句")
            text = _drop_sentences_with(text, [color])
    # 9) 止损缺档:LLM 不得以"暂未设定/未设定"敷衍 —— 替换为规则句
    #    (止损档 = 最下档,缺档只发生在快照无更低白名单位时,见 C4' v3 设计)
    if anchors.get("ok") and anchors.get("stop_loss") is None:
        cut = anchors.get("cut_loss")
        repl = (f"止损缺档(快照无更低白名单档);严格以砍仓档 {float(cut):.3f} 为最严防线,跌破即按档位递进退场"
                if cut is not None else "止损缺档(快照无更低白名单档)")
        for pat in (r"止损[^。\n]{0,12}?(暂未设定|未设定|暂无设置|未给出)",
                    r"止损位[^。\n]{0,12}?(暂未设定|未设定|暂无设置|未给出)"):
            m9 = re.search(pat, text)
            if m9:
                issues.append("止损缺档被写成'暂未设定' → 替换为规则句")
                text = re.sub(pat, repl, text)
    # 10) 身份×动词(§6.1.1,v2.1):无回踩结构/区间震荡/破位场景禁止右侧语
    #     (8.24 光伏:"突破 0.850 可考虑回踩"——反弹压力位身份下语义错位)
    no_pb = bool(anchors.get("no_pullback"))
    scenario = (state or {}).get("scenario", "")
    if no_pb or scenario in ("区间震荡", "破位"):
        id_pat = re.compile(
            r"回踩[^，。；\n]{0,20}加仓|突破[^，。；\n]{0,20}加仓"
            r"|突破[^，。；\n]*回踩|企稳[^，。；\n]{0,10}加仓")
        kept = [l for l in text.splitlines() if not id_pat.search(l)]
        if len(kept) != len(text.splitlines()):
            issues.append(f"身份×动词违规(场景={scenario},无回踩结构={no_pb}),已删句")
            text = "\n".join(kept)
    # 11) 大盘压制语境(8.24 光伏:大盘均线全压制仍写"托举作用")
    if "## 大盘" in snapshot_md:
        idx_part = snapshot_md.split("## 大盘", 1)[1]
        if "受制" in idx_part and "上方压力" in idx_part:
            kept = [l for l in text.splitlines()
                    if "托举" not in l and "托底" not in l and "跷跷板" not in l]
            if len(kept) != len(text.splitlines()):
                issues.append("大盘压制语境下使用'托举/托底/跷跷板',已删句")
                text = "\n".join(kept)
    # 12) 试多护栏(v2.2,kimi 8.25 审阅):① 状态词≠超跌试多时"可试多"类结论越级删句
    #     (左侧观望=未满足 §4.2 准入,结论只能是观察;合法写法是"评估试多候选+缺项");
    #     ② "跌破X试多"永远非法——跌破支撑=趋势破坏(§4.1),禁承接。
    state_word = (state or {}).get("state_word", "")
    kept12 = []
    changed12 = False
    for line in text.splitlines():
        if state_word != "超跌试多" and re.search(r"可试多|可择机试多|择机试多|可考虑试多", line):
            issues.append(f"状态词={state_word} 下出现'可试多'类结论,已删句")
            changed12 = True
            continue
        if re.search(r"跌破[^，。；\n]{0,12}(试多|抄底)", line):
            issues.append("跌破支撑位+试多 = 破位禁承接(§4.1),已删句")
            changed12 = True
            continue
        kept12.append(line)
    if changed12:
        text = "\n".join(kept12)
    # 14) 突破触发确认词(§3.1):"突破/站上 {突破档价} 加仓/买入"必须带 放量/收盘 确认
    #     (8.25 复跑:"若突破0.845,可加仓" 无确认词;空头里 M5 亦非突破档)
    bo = anchors.get("breakout_add")
    if anchors.get("ok") and bo is not None and price is not None:
        kept14 = []
        for line in text.splitlines():
            m14 = re.search(r"(突破|站上)\s*" + re.escape(f"{float(bo):.3f}") + r"[^，。；\n]{0,15}?(加仓|买入|追)", line)
            if m14 and not re.search(r"放量|收盘|次日|确认|站稳", line):
                issues.append(f"突破档 {float(bo):.3f} 触发缺少确认词(放量/收盘),已删句")
                continue
            kept14.append(line)
        text = "\n".join(kept14)
    # 15) 背离术语(8.25):"背离"只许指价格-指标背离(顶/底背离、创新高/创新低同句);
    #     多周期方向相反只能用"周期冲突/矛盾/对抗"
    kept15 = [l for l in text.splitlines()
              if not (re.search(r"(?<![顶底])背离", l)
                      and not re.search(r"创新高|创新低|顶背离|底背离", l))]
    if len(kept15) != len(text.splitlines()):
        issues.append("'背离'误用(多周期冲突应为'周期冲突/矛盾'),已删句")
        text = "\n".join(kept15)
    # 16) 位置模糊词与程序档位冲突(8.25:"近20日区间下轨附近"误读——46% 属中轨区)
    mpos = re.search(r"现价位于区间 (\d+)%", snapshot_md.split("## 大盘")[0])
    if mpos:
        pct = int(mpos.group(1))
        banned_pos = []
        if pct >= 30:
            banned_pos += ["下轨附近", "下轨区域", "底部附近", "下轨位置"]
        if pct < 70:
            banned_pos += ["上轨附近", "顶部附近", "上轨区域"]
        if pct < 30 or pct >= 70:
            banned_pos += ["中轨附近"]
        kept16 = [l for l in text.splitlines()
                  if not (any(t in l for t in banned_pos)
                          and ("近20日区间" in l or "日线区间" in l))]
        if len(kept16) != len(text.splitlines()):
            issues.append(f"位置模糊词与程序档位冲突(近20日区间 {pct}%),已删句")
            text = "\n".join(kept16)
    # 17) 日内模糊表述(8.25:"日内高低点附近"未量化;快照已给日内分位%)
    kept17 = [l for l in text.splitlines() if "高低点附近" not in l]
    if len(kept17) != len(text.splitlines()):
        issues.append("'日内高低点附近'无量化表述,已删句")
        text = "\n".join(kept17)
    # 18) 量比引用(8.25:"量比0.54%"单位错误+数值错配;快照量比 0.73 无量纲)
    mvr = re.search(r"量比 ([\d.]+)", snapshot_md.split("## 大盘")[0])
    if mvr:
        vr_ref = mvr.group(1)
        kept18 = []
        for line in text.splitlines():
            m18 = re.search(r"量比 ?(\d+\.?\d*)%?", line)
            if m18 and (m18.group(1) != vr_ref or m18.group(0).endswith("%")):
                issues.append(f"量比引用错误({m18.group(0)} vs 快照 {vr_ref}),已删句")
                continue
            kept18.append(line)
        text = "\n".join(kept18)
    # 19) 操作价位白名单收紧(8.25:"失效=0.835"——0.835 是盘口买4挂单价,非关键位):
    #     含操作动词(突破/回踩/失效/触发…)的句子,价位必须在 非盘口/非资金 的白名单内
    removed19 = set()
    for line in snapshot_md.splitlines():
        if line.startswith("| 盘口 |") or line.startswith("| 资金 |"):
            removed19 |= set(re.findall(r"\d+\.\d{3}(?!\d)", line))
    if removed19:
        op_verb_re = re.compile(r"失效|触发|试多|企稳|跌破|站上|突破|回踩|减仓|止损|清仓|加仓|买入")
        kept19 = [l for l in text.splitlines()
                  if not (op_verb_re.search(l)
                          and (set(re.findall(r"\d+\.\d{3}(?!\d)", l)) & removed19))]
        if len(kept19) != len(text.splitlines()):
            issues.append("操作价位命中盘口/资金行数值(非关键位,如五档挂单),已删句")
            text = "\n".join(kept19)
    # 20) 大盘两维事实冲突(v2.7 解耦:方向定性→趋势维度/动能维度两行)
    #     程序只报两维事实,LLM 综合;此处拦截与事实矛盾的定性句:
    #     动能 DEA 零下 → 禁"MACD 零上/双线零上/动能翻多";趋势跌破 M20 → 禁"站上M20/偏暖"。
    if "## 大盘" in snapshot_md:
        idx_part = snapshot_md.split("## 大盘", 1)[1]
        mt = re.search(r"趋势维度\(程序\)\s*\|\s*([^|]+)", idx_part)
        mk = re.search(r"动能维度\(程序\)\s*\|\s*([^|]+)", idx_part)
        trend_txt = mt.group(1).strip() if mt else ""
        mom_txt = mk.group(1).strip() if mk else ""
        if mom_txt and "DEA" in mom_txt:
            dea_z = re.search(r"DEA [\d.-]+ (零上|零下)", mom_txt)
            if dea_z and dea_z.group(1) == "零下":
                kept20 = [l for l in text.splitlines()
                          if not re.search(r"MACD零上|MACD 零上|双线零上|动能(全面)?翻多|动能已转多", l)]
                if len(kept20) != len(text.splitlines()):
                    issues.append("大盘动能 DEA 零下,但报告称'MACD零上/动能翻多',已删句")
                    text = "\n".join(kept20)
        if trend_txt and trend_txt.startswith("跌破"):
            kept20b = [l for l in text.splitlines()
                       if not re.search(r"站在?.?M20|站稳M20|偏暖|回暖|趋暖", l)]
            if len(kept20b) != len(text.splitlines()):
                issues.append("大盘趋势跌破 M20,但报告称'站上M20/偏暖',已删句")
                text = "\n".join(kept20b)
    # 21) ETF 内外盘:结论最多一句,且必须带"套利机制参考性有限"限定(8.25 复读)
    kept21, seen_concl, changed21 = [], 0, False
    for line in text.splitlines():
        is_concl = bool(re.search(r"抛压占优|承接占优", line))
        if is_concl:
            seen_concl += 1
            if seen_concl > 1:
                issues.append("内外盘结论重复,仅保留首句")
                changed21 = True
                continue
            if not re.search(r"参考性有限|套利|不构成多空", line):
                line = (line.replace("抛压占优", "抛压占优(ETF套利机制下参考性有限)", 1)
                            .replace("承接占优", "承接占优(ETF套利机制下参考性有限)", 1))
                changed21 = True
        kept21.append(line)
    if changed21:
        text = "\n".join(kept21)
    # 22) 企稳信号白名单(8.25:"J转负/开口收窄"作企稳=恐慌急跌不是企稳):
    #     企稳只许 C8' 三选一(缩量十字星后放量阳/日线KDJ低位金叉/重上日线M20)
    kept22 = [l for l in text.splitlines()
              if not (re.search(r"企稳", l)
                      and re.search(r"J转负|J开口收窄|J值转负|KDJ转负|J跌破|J跌至", l))]
    if len(kept22) != len(text.splitlines()):
        issues.append("企稳信号使用自造 J 类表述(应使用 C8' 白名单),已删句")
        text = "\n".join(kept22)
    # 23) 指标不得作"回踩/企稳"主语(8.25:"30分KDJ回踩0.836"病句;KDJ 是震荡指标非价格)
    kept23 = [l for l in text.splitlines()
              if not (re.search(r"(KDJ|MACD|J线|D线|K线|随机指标)[^，。；\n]{0,10}(回踩|企稳|站上|跌破)", l)
                      and not re.search(r"金叉|上穿|死叉", l))]
    if len(kept23) != len(text.splitlines()):
        issues.append("指标(KDJ/MACD)不得作'回踩/企稳'主语(病句),已删句")
        text = "\n".join(kept23)
    # 24) 相对强弱方向一致(8.26 光伏实盘硬伤:事实卡=跑输3.31pp,LLM 却写"跑赢")
    mrs = re.search(r"相对强弱[^\n]*?(跑赢|跑输|持平)", snapshot_md.split("## 大盘")[0])
    if mrs:
        card_rs = ("跑输" if "跑输" in mrs.group(1)
                   else "跑赢" if "跑赢" in mrs.group(1) else "持平")
        kept24 = [l for l in text.splitlines()
                  if not (("近5日" in l or "相对强弱" in l)
                          and ((card_rs == "跑输" and "跑赢" in l)
                               or (card_rs == "跑赢" and "跑输" in l)))]
        if len(kept24) != len(text.splitlines()):
            issues.append(f"相对强弱方向与事实卡矛盾(卡={card_rs}),已删句")
            text = "\n".join(kept24)
    # 25) KDJ 标签一致性(8.27 A/B 候选实测:快照"(中性·空排·三线收敛)",LLM 写"日线KDJ超卖(J=24.8)")
    kdj_states: dict[str, str] = {}
    for m25 in re.finditer(r"\|\s*(日线|30分)\s*\|\s*KDJ[^\n]*?\((超买|超卖|中性)[^)]*\)",
                           snapshot_md):
        kdj_states[m25.group(1)] = m25.group(2)
    kept25 = []
    for line in text.splitlines():
        if "KDJ" in line and re.search(r"超买|超卖", line):
            claimed = "超买" if "超买" in line else "超卖"
            bad = any(period in line and state != claimed
                      for period, state in kdj_states.items())
            if bad:
                issues.append("KDJ 标签与快照矛盾(快照为中性/相反),已删句")
                continue
        kept25.append(line)
    if len(kept25) != len(text.splitlines()):
        text = "\n".join(kept25)
    # 6) 建议类别:由 suggest_category 收敛,违规类别行删除
    cat = suggest_category(text, state)
    if cat not in CATEGORIES:
        issues.append(f"建议类别越界:{cat}")
    if issues:
        logger.warning("深入分析校验链: %s", "; ".join(issues))
    return text, degraded, issues


def _fix_param_block(text: str, anchors: dict) -> str:
    """把 LLM 输出中的「操作参数」行替换为程序锚点值(自动修正)。
    格式: "- 突破加仓位: X (标签)" 等;锚点缺失的行不生成。"""
    repl = {
        "突破加仓位": anchors.get("breakout_add"),
        "回踩加仓位": anchors.get("pullback_add"),
        "跌破砍仓位": anchors.get("cut_loss"),
        "止损认错位": anchors.get("stop_loss"),
    }
    out = text
    for field, v in repl.items():
        if v is None:
            continue
        pattern = rf"({field}[:：])\s*[\d.]+\s*\(([^)]*)\)"
        new = rf"\1 {float(v):.3f} (程序锚点)"
        out = re.sub(pattern, new, out)
    return out


def _drop_sentence(text: str, verb: str, pos: str) -> str:
    """删除含 (verb pos) 的行(违规句处理)。"""
    lines = text.splitlines()
    keep = [l for l in lines if not re.search(rf"{verb}\s*{pos}", l)]
    return "\n".join(keep)


def _drop_sentences_with(text: str, tokens: list[str]) -> str:
    return "\n".join(l for l in text.splitlines()
                     if not any(t in l for t in tokens if t))