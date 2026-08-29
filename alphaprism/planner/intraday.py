"""盘中技术快照(2026-08-22):两张卡——数据快照 + 深入分析。

与作战地图解耦:只读实时 K 线/分时/盘口,不引用地图的买区/红线价位
(地图周级更新存在滞后,盘中读盘应由实时数据驱动)。

两张卡完全独立:
- 数据快照(build_tech_facts):确定性渲染,不经过 LLM,毫秒级返回;
  程序计算 MA/KDJ/MACD/量比/换手/盘口五档/折溢价,数字精确可复核。
- 深入分析(build_tech_analysis):LLM 以数据快照为唯一输入,做多周期矛盾
  推演(日线/30分/分时信号冲突)、唯一核心观察点 + 止损锚点、量价盘口博弈
  解读。LLM 失败 → 降级确定性信号段,不影响数据快照卡。

数据快照 markdown 同时是 LLM 的输入文本——"用户看到的 = 模型看到的",
LLM 幻觉可被肉眼复核。输出 markdown,供 Web 盘中 tab / 推送共用。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from ..config import Config
from ..fetchers import fuyao
from ..fetchers.etf_kline import (fetch_30m, fetch_daily, fetch_etf_fund_flow,
                                  fetch_index_daily, fetch_minute, fetch_name,
                                  fetch_orderbook)
from . import indicators as ind

logger = logging.getLogger(__name__)

INDEX_THS = "000001.SH"        # 上证指数(同花顺 thscode)
INDEX_TSYM = "sh000001"        # 上证指数(腾讯符号,不走 _tx_symbol)
INDEX_NAME = "上证指数"
DAILY_START = "2023-01-01"     # M250 需约 250 根,留余量
DAILY_END = "2030-12-31"
M30_COUNT = 320                # 30分 K 线约 8 根/交易日,320 根 ≈ 40 交易日

# 分时采样时间点(供 LLM 看冲高回落/高点下移形态)
_SAMPLE_TIMES = ("09:30", "10:00", "10:30", "11:00", "11:30",
                 "13:00", "13:30", "14:00", "14:30", "15:00")

_MA_PERIODS = (5, 10, 20, 60, 100, 120, 250)     # 日线均线(含 Kimi 用到的 M100/M120)
_M30_MA = (5, 10, 20, 60, 250)                    # 30分均线(Kimi 用到 M250)


def _r3(v) -> float | None:
    return round(float(v), 3) if v is not None else None


def _fmt_price(v, d: int = 3) -> str:
    return f"{v:.{d}f}" if v is not None else "-"


def _fmt_chg(v) -> str:
    return f"{'+' if v and v > 0 else ''}{v:.2f}%" if v is not None else "-"


def _fmt_ma(ma: dict) -> str:
    return " ".join(f"M{p}={v:.3f}" for p, v in ma.items() if v is not None)


def _fmt_kdj(kdj: dict) -> str:
    """KDJ 文本 + 程序标注(超买/超卖/中性 + 三线结构:排向 + J 开口)。

    v2.3:按《kdj指标macd指标分析》增加确定性三线标注——K>D 多排 / K<D 空排;
    J−K ≥15 上翘(短线过热)/ ≤−15 深低于K(倒喇叭,超卖修复动能积蓄)/ 其余三线收敛。
    三线共振(均>80 / 均<20)单独标注。LLM 只许照抄,不得自造开口描述。
    """
    if kdj.get("K") is None:
        return "-"
    j = kdj.get("J")
    state = ("超买" if j is not None and j > 80
             else "超卖" if j is not None and j < 20 else "中性")
    K, D = kdj.get("K"), kdj.get("D")
    extra = ""
    if K is not None and D is not None and j is not None:
        pair = "多排" if K >= D else "空排"
        diff = round(j - K, 1)
        if K > 80 and D > 80 and j > 80:
            mouth = "三线共振超买"
        elif K < 20 and D < 20 and j < 20:
            mouth = "三线共振超卖"
        elif diff >= 15:
            # 区间语境(v2.4,解读词典):J 上翘在超买区=过热回调风险,低位=修复动能
            mouth = "J上翘" + ("·超买过热·回调风险" if j > 80
                               else "·低位·修复动能" if j < 30 else "")
        elif diff <= -15:
            # J 深低于K:超卖区=倒喇叭反弹修复积蓄;高位=背离式走弱
            mouth = ("J深低于K" + ("·反弹修复积蓄" if j < 30
                                   else "·高位背离式走弱" if j > 80 else ""))
        else:
            mouth = "三线收敛"
        extra = f"·{pair}·{mouth}"
    return f"K={K} D={D} J={j}({state}{extra})"


def _fmt_macd(m: dict) -> str:
    if m.get("dif") is None:
        return "-"
    return (f"DIF={m['dif']} DEA={m['dea']} 柱={m['hist']}"
            f"({ind.macd_state(m)})")


def _w(v) -> str:
    """手数 → 万手/手(对齐行情软件口径)。"""
    if v is None:
        return "-"
    return f"{v / 10000:.2f}万手" if v >= 10000 else f"{int(v)}手"


# --------------------------------------------------------------------------- 指标事实

def _daily_facts(df) -> dict:
    """日线事实:MA5-250 / KDJ / MACD / 近20日高低 / 阶段高点(近250日) / 阶段低点 / ATR20 / 5·10日均量。"""
    if df is None or df.empty:
        return {}
    closes = [float(x) for x in df["close"].tolist()]
    highs = [float(x) for x in df["high"].tolist()]
    lows = [float(x) for x in df["low"].tolist()]
    vols = [float(x) for x in df["volume"].tolist()]
    recent = df.tail(20)
    swing = df.tail(250)
    hi_pos = int(swing["high"].idxmax()) if len(swing) else None
    lo_pos = int(swing["low"].idxmin()) if len(swing) else None
    return {
        "ma": {p: _r3(ind.ma(closes, p)) for p in _MA_PERIODS},
        "kdj": ind.kdj(highs, lows, closes),
        "macd": ind.macd(closes),
        "recent_high": _r3(float(recent["high"].max())) if len(recent) else None,
        "recent_low": _r3(float(recent["low"].min())) if len(recent) else None,
        "swing_high": _r3(float(swing["high"].max())) if len(swing) else None,
        "swing_high_date": str(df["trade_date"].iloc[hi_pos]) if hi_pos is not None else None,
        "swing_low": _r3(float(swing["low"].min())) if len(swing) else None,
        "swing_low_date": str(df["trade_date"].iloc[lo_pos]) if lo_pos is not None else None,
        "atr20": ind.atr(highs, lows, closes),
        "avg5_vol": round(float(pd_sum(vols[-5:])) / 5, 0) if len(vols) >= 5 else None,
        "avg10_vol": round(float(pd_sum(vols[-10:])) / 10, 0) if len(vols) >= 10 else None,
        "last_close": _r3(closes[-1]),
    }


def pd_sum(xs) -> float:
    """list[float] 求和(容忍 None/NaN)。"""
    return sum(float(x) for x in xs if x is not None)


def _m30_facts(df) -> dict:
    """30分钟事实:MA5-250 / KDJ / MACD / 近60根区间高低。"""
    if df is None or df.empty:
        return {}
    closes = [float(x) for x in df["close"].tolist()]
    highs = [float(x) for x in df["high"].tolist()]
    lows = [float(x) for x in df["low"].tolist()]
    win = df.tail(60)
    return {
        "ma": {p: _r3(ind.ma(closes, p)) for p in _M30_MA},
        "kdj": ind.kdj(highs, lows, closes),
        "macd": ind.macd(closes),
        "range_high": _r3(float(win["high"].max())) if len(win) else None,
        "range_low": _r3(float(win["low"].min())) if len(win) else None,
    }


def _minute_series(df) -> str:
    """分时采样序列:"09:30 0.869 / 10:00 0.872 / ..."(每采样点取该时点后首个点)。"""
    if df is None or df.empty:
        return ""
    pts = []
    for t in _SAMPLE_TIMES:
        row = df[df["ts"] >= t]
        if not row.empty:
            r = row.iloc[0]
            pts.append(f"{str(r['ts'])[:5]} {float(r['price']):.3f}")
    return " / ".join(pts)


def _tail_vol_label(df) -> str:
    """最新30根(约30分钟)量能 vs 全天单根均量:尾盘放量/缩量/平量。"""
    if df is None or len(df) < 31:
        return ""
    vols = [float(x) for x in df["vol"].tolist()]
    overall = sum(vols) / len(vols)
    tail = sum(vols[-30:]) / 30
    if overall <= 0:
        return ""
    r = tail / overall
    return "尾盘放量" if r >= 1.2 else "尾盘缩量" if r <= 0.8 else "尾盘平量"


def _minute_facts(df) -> dict:
    """分时事实:现价/均价/日内高低/现价vs均价/距日内最高回撤/采样序列/尾盘量能。

    日内高低先取分时采样极值,随后由 _asset_facts 用盘口(交易所)高低覆盖
    (分时 1 分钟采样会漏掉盘中最高/最低点)。回撤口径:距日内最高回撤 =
    (最高-现价)/最高,以峰值为基准(标准口径)。
    """
    if df is None or df.empty:
        return {}
    price = float(df["price"].iloc[-1])
    prev = df["prev_close"].iloc[-1]
    avg = df["avg_price"].iloc[-1]
    high = float(df["price"].max())
    low = float(df["price"].min())
    hi_pos = int(df["price"].idxmax())
    return {
        "price": _r3(price),
        "change_pct": round((price / float(prev) - 1) * 100, 2) if prev else None,
        "avg": _r3(avg) if avg is not None else None,
        "high": _r3(high),
        "high_time": str(df["ts"].iloc[hi_pos])[:5],
        "low": _r3(low),
        "vs_avg": round((price / float(avg) - 1) * 100, 2) if avg else None,
        "intraday_dd": round((1 - price / high) * 100, 2) if price else None,
        "series": _minute_series(df),
        "tail_vol": _tail_vol_label(df),
    }


def _override_day_hl(mn: dict, ob: dict) -> None:
    """日内最高/最低以盘口(交易所)为准——分时采样会漏极值点。

    盘口值(字段33/34)与分时采样值不同时,以盘口为准并清空时间
    (无法从盘口得知交易所极值发生时刻);随后重算"距日内最高回撤"。
    """
    if not ob:
        return
    for key, ob_key in (("high", "high"), ("low", "low")):
        v = ob.get(ob_key)
        if v is not None and mn.get(key) is not None and abs(float(v) - float(mn[key])) > 1e-9:
            mn[key] = round(float(v), 4)
            if key + "_time" in mn:
                mn[key + "_time"] = None
    price = mn.get("price")
    if price is not None and mn.get("high"):
        mn["intraday_dd"] = round((1 - price / mn["high"]) * 100, 2)


def _vol_ratio_daily(daily, now: datetime) -> float | None:
    """日线时间调整量比(盘口量比拿不到时的兜底):今日量 / (5日均量 × 已过分钟/240)。"""
    from ..monitor import TRADING_MINUTES, _trading_minutes

    if daily is None or len(daily) < 6:
        return None
    try:
        today_vol = float(daily["volume"].iloc[-1]) or 0
        avg5 = float(daily["volume"].iloc[-6:-1].mean()) or 0
    except (TypeError, ValueError):
        return None
    if today_vol <= 0 or avg5 <= 0:
        return None
    minutes = _trading_minutes(now)
    if minutes <= 0:
        return round(today_vol / avg5, 2) if avg5 > 0 else None   # 收盘后全天量比
    expected = avg5 * minutes / TRADING_MINUTES
    return round(today_vol / expected, 2) if expected > 0 else None


def _asset_facts(code: str, name: str, daily, m30, minute, now: datetime,
                 orderbook: dict | None = None,
                 fund_flow: dict | None = None) -> dict:
    """单只资产(ETF 或指数)的事实合集。各数据源失败时留空,不阻断。

    fund_flow:仅 ETF 有(天天基金季度规模),指数传 None。
    """
    mn = _minute_facts(minute)
    _override_day_hl(mn, orderbook or {})   # 日内高低以盘口(交易所)为准
    d = _daily_facts(daily)
    last_date = None
    if daily is not None and not daily.empty:
        last_date = str(daily["trade_date"].iloc[-1])
    # 位置指标:距阶段高点回撤 + 近20日区间位置%(现价在区间中的百分位)
    swing_dd = range_pos = None
    price = mn.get("price")
    if price is not None:
        if d.get("swing_high"):
            # 回撤以峰值为基准(标准口径):(峰值-现价)/峰值
            swing_dd = round((1 - price / d["swing_high"]) * 100, 2)
        if d.get("recent_high") and d.get("recent_low") and d["recent_high"] > d["recent_low"]:
            range_pos = round((price - d["recent_low"]) / (d["recent_high"] - d["recent_low"]) * 100)
    # 量比:优先盘口标准量比,兜底日线时间调整量比
    vr = None
    if orderbook and orderbook.get("vol_ratio") is not None:
        vr = round(float(orderbook["vol_ratio"]), 2)
    else:
        vr = _vol_ratio_daily(daily, now)
    # 盘中已过交易分钟数(量能行"今日量 vs 均量"时间折算用;收盘后=0 → 全天口径)
    minutes_passed = 0
    try:
        from ..monitor import _trading_minutes
        minutes_passed = _trading_minutes(now) or 0
    except Exception:  # noqa: BLE001
        minutes_passed = 0
    return {
        "code": code, "name": name, "date": last_date,
        "price": price, "change_pct": mn.get("change_pct"),
        "vol_ratio": vr,
        "daily": d,
        "m30": _m30_facts(m30),
        "minute": mn,
        "orderbook": orderbook or {},
        "fund_flow": fund_flow or {},
        "swing_dd": swing_dd, "range_pos": range_pos,
        "minutes_passed": minutes_passed,
        "chg5d": _chg5d(daily),
    }


def _chg5d(daily) -> float | None:
    """近5日累计涨跌幅 %(C7 相对强弱分母;数据不足返回 None)。"""
    if daily is None or len(daily) < 6:
        return None
    try:
        closes = [float(x) for x in daily["close"].tolist()]
        base = closes[-6]
        return round((closes[-1] / base - 1) * 100, 2) if base else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 取数

def _fetch_etf(code: str, now: datetime) -> dict:
    daily = fetch_daily(code, DAILY_START, DAILY_END)
    m30 = fetch_30m(code, count=M30_COUNT)
    minute = fetch_minute(code)
    orderbook = fetch_orderbook(code)
    # 估算规模 = 最新份额 × 净值(净值用盘口 IOPV 兜底)
    nav = (orderbook or {}).get("iopv")
    fund_flow = fetch_etf_fund_flow(code, nav)
    return _asset_facts(code, "", daily, m30, minute, now, orderbook, fund_flow)


def _fetch_index(now: datetime) -> dict | None:
    """上证指数事实(腾讯符号直连)。全部失败返回 None。"""
    try:
        daily = fetch_index_daily(INDEX_THS, DAILY_START, DAILY_END)
        m30 = fetch_30m(INDEX_TSYM, count=M30_COUNT, tsym=INDEX_TSYM)
        minute = fetch_minute(INDEX_TSYM, tsym=INDEX_TSYM)
        orderbook = fetch_orderbook(INDEX_TSYM, tsym=INDEX_TSYM)
    except Exception as exc:  # noqa: BLE001
        logger.warning("上证指数数据获取失败: %s", exc)
        return None
    if daily is None or daily.empty:
        return None
    return _asset_facts(INDEX_THS, INDEX_NAME, daily, m30, minute, now, orderbook)


def _name_of(cfg, code: str) -> str:
    for w in cfg.watchlist:
        if str(w.get("symbol", "")) == code:
            return str(w.get("name", "")) or code
    return fetch_name(code) or code


# --------------------------------------------------------------------------- 事实文本

def _ob_rows(ob: dict) -> list[tuple[str, str]]:
    """盘口 → 逐行指标文本(五档 / 委差 / 外内盘 / IOPV / 折溢价)。

    2026-08-29:原"一行通吃"拆成每指标一行,盘中盘口区逐个扫读。
    外内盘结论词打头(程序标注,防 LLM 把不等式看反):先声明谁大。
    """
    if not ob:
        return []
    rows: list[tuple[str, str]] = []
    if ob.get("buy") and any(b.get("price") is not None for b in ob["buy"]):
        # 腾讯档位字段可能为 0/空(停牌、收盘后、指数无五档)→ price=None,
        # 跳过该档不渲染,绝不把 None 丢进 f-string 格式符(会抛
        # "unsupported format string passed to NoneType").
        buy = " ".join(f"买{i + 1} {_fmt_price(b.get('price'))}/{_w(b.get('vol'))}"
                       for i, b in enumerate(ob["buy"]) if b.get("price") is not None)
        sell = " ".join(f"卖{i + 1} {_fmt_price(s.get('price'))}/{_w(s.get('vol'))}"
                        for i, s in enumerate(ob["sell"]) if s.get("price") is not None)
        rows.append(("五档", f"买1-5【{buy}】卖1-5【{sell}】"))
    if ob.get("weicha") is not None:
        rows.append(("委差", f"{ob['weicha']}"))
    if ob.get("waipan") is not None and ob.get("neipan") is not None:
        wp, np = float(ob["waipan"]), float(ob["neipan"])
        side = "承接占优(外盘大)" if wp > np else "抛压占优(内盘大)" if np > wp else "外内盘相当"
        rows.append(("外内盘", f"{side}: 外盘 {_w(wp)} 内盘 {_w(np)}"))
    if ob.get("iopv") is not None:
        rows.append(("IOPV", f"{ob['iopv']}"))
    if ob.get("premium_pct") is not None:
        rows.append(("折溢价", f"{ob['premium_pct']:+.2f}%"))
    return rows


def _ma_relation(price: float | None, ma: dict) -> str:
    """现价 vs 各均线:站上(下方支撑) / 受制(上方压力)。

    确定性分类,防 LLM 把均线性质写反(如把现价下方的 M20 写成压力)。
    """
    if price is None:
        return ""
    below = [p for p, v in ma.items() if v is not None and price >= v]
    above = [p for p, v in ma.items() if v is not None and price < v]
    parts = []
    if below:
        parts.append(f"站上 M{'/M'.join(map(str, below))}(下方支撑)")
    if above:
        parts.append(f"受制 M{'/M'.join(map(str, above))}(上方压力)")
    return "; ".join(parts)


def _asset_rows(a: dict, is_index: bool = False) -> list[str]:
    """单只资产的事实表行(标的或大盘),供数据快照卡渲染。

    三列结构:周期 / 指标 / 数值(固定前两列宽度,数值列弹性)。
    周期分组:分时 / 日线 / 30分 / 量能 / 盘口 / 资金。
    is_index:指数无真实"每股价格",分时均价/VWAP 与五档盘口均无意义,跳过。
    """
    d, m, mn = a["daily"], a["m30"], a["minute"]
    ob = a.get("orderbook") or {}
    rows: list[tuple[str, str, str]] = []
    price = mn.get("price")

    def add(period: str, k: str, v: str) -> None:
        rows.append((period, k, v))

    add("分时", "现价", f"{_fmt_price(price)} ({_fmt_chg(mn.get('change_pct'))})")
    if not is_index and mn.get("avg") is not None:
        add("分时", "均价", f"{_fmt_price(mn.get('avg'))} (现价 vs 均价 "
                            f"{_fmt_price(mn.get('vs_avg'), 2)}%,基准=均价)")
    if mn.get("high") is not None:
        # 日内分位(程序标注):(现价−日内低)/(日内高−日内低);低位区<30,高位区≥70
        intraday_extra = ""
        lo, hi = mn.get("low"), mn.get("high")
        if hi and lo and price is not None and hi > lo:
            pos = round((price - lo) / (hi - lo) * 100)
            zone = "高位区" if pos >= 70 else "低位区" if pos < 30 else "中位区"
            intraday_extra = f"·日内分位 {pos}%({zone})"
        add("分时", "日内高低",
            f"{_fmt_price(mn.get('high'))}@{mn.get('high_time') or '-'} / "
            f"{_fmt_price(mn.get('low'))} (现价距日内最高回撤 "
            f"{_fmt_price(mn.get('intraday_dd'), 2)}%{intraday_extra})")
    if mn.get("series"):
        add("分时", "序列", mn["series"])
    if a.get("swing_dd") is not None and d.get("swing_high") is not None:
        # 近20日区间位置:保留连续数值,只给朴素定位词(2026-08-29 用户拍板:
        # 不搞多档分档——<30 下轨 / ≥70 上轨 / 中轨内以 50 为界分 中下·中上,
        # 64% 直接显示数值,用户自己看得懂)。
        band = ""
        rp = a.get("range_pos")
        if rp is not None:
            band = "·" + ("下轨区" if rp < 30
                          else "上轨区" if rp >= 70
                          else "中上轨" if rp >= 50 else "中下轨")
        add("日线", "位置",
            f"距阶段高点(近250日) {d['swing_high']:.3f}@{d.get('swing_high_date') or '-'} "
            f"回撤 {a['swing_dd']:.2f}%;近20日区间 {_fmt_price(d.get('recent_low'))}-"
            f"{_fmt_price(d.get('recent_high'))},现价位于区间 {a.get('range_pos')}%{band}")
    vol_parts = []
    if a.get("vol_ratio") is not None:
        vr = a["vol_ratio"]
        label = "放量" if vr >= 1.5 else "缩量" if vr <= 0.8 else "平量"
        vol_parts.append(f"量比 {vr}({label})")
    if ob.get("turnover_pct") is not None:
        # 2026-08-29 口径标注:换手率来自腾讯盘口(腾讯流通份额口径,实时),
        # 与「资金·份额」行的季报份额(如 2026-06-30)不同源——标注防复算误解。
        vol_parts.append(f"换手 {ob['turnover_pct']}%(腾讯流通份额口径)")
    tv = _today_vol(a)
    if tv and tv > 0 and d.get("avg5_vol") is not None:
        # 2026-08-25 修复(notes_0825 #6):快照"今日量"= 外盘+内盘累计(盘中未收盘),
        # 直接与全天均量对比会系统性偏低。盘中时段把均量按已过分钟折算后再比;
        # 收盘后(已过分钟=0)保持全天口径(与 _vol_ratio_daily 同一语义)。
        mp = a.get("minutes_passed") or 0
        from ..monitor import TRADING_MINUTES
        if 0 < mp < TRADING_MINUTES:
            exp5 = d["avg5_vol"] * mp / TRADING_MINUTES
            exp10 = (d["avg10_vol"] * mp / TRADING_MINUTES) if d.get("avg10_vol") else None
            tail = f"今日量 {_w(tv)}(盘中{mp}min累计) vs 5日均量折算 {_w(exp5)}"
            if exp10:
                tail += f" / 10日均量折算 {_w(exp10)}"
            vol_parts.append(tail)
        else:
            vol_parts.append(f"今日量 {_w(tv)} vs 5日均量 {_w(d['avg5_vol'])} "
                             f"/ 10日均量 {_w(d['avg10_vol'])}")
    if mn.get("tail_vol"):
        vol_parts.append(mn["tail_vol"])
    if vol_parts:
        add("量能", "量能", "; ".join(vol_parts))
    add("日线", "均线", _fmt_ma(d["ma"]))
    rel = _ma_relation(price, d["ma"])
    if rel:
        add("日线", "均线性质", rel)
    if d.get("kdj", {}).get("K") is not None:
        add("日线", "KDJ", _fmt_kdj(d["kdj"]))
    if is_index and (d.get("ma") and price is not None):
        # 大盘维度解耦(v2.7,2026-08-29):废"偏暖/系统性压制"打包标签。
        # 拆为两个独立事实行——趋势维度(价 vs M20)与动能维度(DIF/DEA vs 零轴),
        # 程序只报事实不下结论,综合定性交给 LLM(防"MACD零上"把 DEA 零下藏进标签)。
        macd = d.get("macd") or {}
        dif, dea = macd.get("dif"), macd.get("dea")
        m20 = d["ma"].get(20)
        if m20 is not None:
            trend = "站上" if price >= m20 else "跌破"
            add("大盘", "趋势维度(程序)",
                f"{trend} M20(现价 {price:.3f} vs M20 {m20:.3f})")
        if dif is not None and dea is not None:
            add("大盘", "动能维度(程序)",
                f"DIF {dif:.3f} {'零上' if dif >= 0 else '零下'} · "
                f"DEA {dea:.3f} {'零上' if dea >= 0 else '零下'}"
                f"({ind.macd_state(macd)})")
    if d.get("macd", {}).get("dif") is not None:
        add("日线", "MACD", _fmt_macd(d["macd"]))
    if m:
        # 2026-08-29:合并行拆分——均线/KDJ/MACD 各自一行(原"均线/KDJ/MACD"一行通吃)
        add("30分", "均线", _fmt_ma(m["ma"]))
    if m and m.get("kdj", {}).get("K") is not None:
        add("30分", "KDJ", _fmt_kdj(m["kdj"]))
    if m and m.get("macd", {}).get("dif") is not None:
        add("30分", "MACD", _fmt_macd(m["macd"]))
    if m and m.get("range_low") is not None:
        add("30分", "区间",
            f"近60根区间 {_fmt_price(m.get('range_low'))}-{_fmt_price(m.get('range_high'))}")
    if not is_index:
        # ETF 资金信号:份额变化为主(基金公司披露,比季报规模及时),
        # 内外盘/大单参考性弱(套利机制)。估算规模 = 份额 × 净值。
        ff = a.get("fund_flow") or {}
        if ff.get("share") is not None:
            parts = [f"{ff['share']}亿份({ff.get('date') or '-'})"]
            if ff.get("share_chg") is not None:
                parts.append(f"较上期 {ff['share_chg']:+.2f}亿份"
                             f"({ff.get('share_chg_pct') or '-'}%)")
            add("资金", "份额", "; ".join(parts))
            if ff.get("scale_est") is not None:
                add("资金", "估算规模", f"{ff['scale_est']}亿元(份额×净值)")
        ob_rows = _ob_rows(ob)
        if ob_rows:
            # 2026-08-29:盘口逐行展示——五档/委差/外内盘各一行(+IOPV/折溢价)
            # 内外盘固定腾讯源(不同平台口径不一致,标注来源)
            for ob_k, ob_v in ob_rows:
                src = "(来源:腾讯)" if ob_k == "外内盘" else ""
                add("盘口", ob_k, ob_v + src)
        add("资金", "信号", "ETF 内外盘/大单参考性弱,以份额变化+折溢价为主")
    return [f"| {p} | {k} | {v} |" for p, k, v in rows]


def _today_vol(a: dict) -> float | None:
    """今日累计成交量(手):优先盘口外盘+内盘,兜底分时末点累计量。"""
    ob = a.get("orderbook") or {}
    if ob.get("waipan") is not None and ob.get("neipan") is not None:
        return float(ob["waipan"]) + float(ob["neipan"])
    return None


def _rs_label(rs: float) -> str:
    """C7 相对强弱标签(rs = 标的近5日涨幅% − 大盘近5日涨幅%,pp;正=跑赢 / 负=跑输)。

    v2.6 修复(8.26 光伏实测):原实现 rs>0 判'跑输'、rs<0 判'跑赢'符号反了——
    光伏近5日 标的-5.843% vs 大盘-2.53%,rs=-3.31 应为「跑输·大幅」,原代码标成'跑赢'。
    """
    if rs is None:
        return ""
    if abs(rs) <= 0.005:
        return "持平"
    if rs > 0:
        return "跑赢·显著" if rs > 3 else "跑赢"
    return "跑输·大幅" if rs < -3 else "跑输"


def _facts_markdown(facts: dict) -> str:
    """数据快照(确定性渲染,不经过 LLM):标的 + 大盘 两张事实表。

    这张表同时是 LLM 深入分析的输入文本——模型只准引用表内数字,
    保证"用户看到的 = 模型看到的",幻觉可被肉眼复核。
    """
    etf, idx = facts["etf"], facts["index"]
    lines = [f"# 数据快照 {facts['date']} {facts['now']} · {etf['name']}({etf['code']})", ""]
    lines += [f"## 标的 · {etf['name']}({etf['code']})", "",
          "| 周期 | 指标 | 数值 |", "|---|---|---|"]
    lines += _asset_rows(etf, is_index=False)
    # C7 相对强弱(近5日):标的5日跌幅 − 大盘5日跌幅,pp;正=跑输(§4.2④ 同口径)
    rs = facts.get("rs_5d")
    if rs is not None:
        lines.append(f"| 相对强弱 | 近5日 | 标的 vs 大盘 {rs:+.2f}pp({_rs_label(rs)}) |")
    if idx:
        lines += ["", f"## 大盘 · {idx['name']}({idx['code']})", "",
                  "| 周期 | 指标 | 数值 |", "|---|---|---|"]
        lines += _asset_rows(idx, is_index=True)
    else:
        lines += ["", "## 大盘 · 上证指数", "", "| 周期 | 指标 | 数值 |",
                  "|---|---|---|", "| 大盘 | 状态 | 数据获取失败(本报告不含大盘环境) |"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- LLM 报告

def _analysis_prompt(facts: dict, snapshot_md: str) -> str:
    """深入分析 prompt(Kimi 式):多周期矛盾推演 · 唯一核心观察点 · 风控锚点。

    snapshot_md 即数据快照卡渲染的同一份文本——LLM 只准引用其中数字。
    """
    etf = facts["etf"]
    return (
        "你是资深量化技术分析研究员,擅长 Price Action 与多周期矛盾推演,服务于 A股中长线"
        "ETF 交易者。下面是**程序精确计算**的数据快照(每个数字都来自实时 K 线/盘口,不是猜测)。"
        "请基于这份数据快照,输出一份【专业机构级】的盘中技术分析简报。\n\n"
        "硬性规则(必须逐条遵守):\n"
        "1. **禁止罗列数据**:不要用表格或单纯报数(如'MACD柱=0.008'),必须把数据转化为市场语言"
        "(如'MACD红柱缩短,表明多头反攻力度衰减,存在诱多嫌疑')。\n"
        "2. **挖掘周期矛盾**:必须重点分析日线级别与30分钟/分时级别之间的背离与对抗(如'日线超卖"
        "但30分钟超买'),并由此推导'反弹空间受限'或'震荡磨底'等结论。\n"
        "3. **量价与盘口实质化**:分析'缩量'时必须区分是'抛压衰竭'还是'买盘不足',结合分时高低点"
        "给出筹码密集区判断;盘口托单/压单/委差/外内盘说明什么,要写实质。\n"
        "4. **大盘环境钳制**:以上证指数为'背景板',分析当前大盘环境是否支持该 ETF 的突破"
        "(如大盘缩量且权重低迷,则标的难以独自放量突破)。\n"
        "5. **位置定性必须诚实**:引用'距阶段高点回撤'时必须用数据快照中的真实数值定性当前位置"
        "——是'大级别下跌后的反弹'(深跌反弹)还是'高位横盘',严禁把回撤说小。注意区分"
        "'距日内最高回撤'与'距阶段高点(近250日)回撤',两者含义完全不同,不得混淆。\n"
        "6. **唯一核心观察点,拒绝并列方案**:不要给出'A方案/B方案'的并列选择。只给一个最合理"
        "的核心观察点,推演'如果……则……;反之则……'的逻辑闭环,且必须明确给出止损认错的价格锚点。\n"
        "7. **锚定日线级别**:加仓/减仓/止损/目标价必须直接引用数据快照中存在的价位(均线、日内"
        "高低、近20日高低、阶段高点),严禁用'现价上方/下方百分之几'机械切割生成新价位,也不要用"
        "现价本身作锚;30分/分时只用于判断'今天能否动手'与日内节奏,不要写成日内做T指南。\n"
        "8. 报告里每个数字必须来自下面喂入的数据快照,严禁编造/推算/外推;引用价位保留 3 位小数。\n"
        "9. **数字纪律(防幻觉)**:数据快照中已标注的状态(如 KDJ 后的'超买/超卖/中性'、均线性质"
        "的'站上/受制')直接沿用,不得自行更改;外盘>内盘才叫'承接占优',外盘<内盘叫'抛压占优',"
        "比较要按数值大小准确判断;引用任何回撤/区间数值时,必须沿用数据快照中该数值原有的标签"
        "(距日内最高回撤 / 距阶段高点(近250日)回撤 / 近20日区间),不得自行给数值换标签。\n"
        "10. **输出前自查清单(逐条核对,发现不符立即改正)**:① 外盘/内盘直接沿用数据快照盘口行"
        "开头的结论('抛压占优(内盘大)'/'承接占优(外盘大)'),照抄结论词,不要自行计算百分比或改写"
        "为相反含义;② 超买=J>80、"
        "超卖=J<20,其余一律说'中性',不得叫超买/超卖;③ 阶段高点"
        "= '位置'行中紧挨'距阶段高点(近250日)'的那个价位,近20日高/近20日低不是阶段高点;④ 现价、"
        "日内最高、日内最低是三个不同数字,不得混用;⑤ '距日内最高回撤 X%'的 X 只属于日内最高价,"
        "不得改挂到现价或其他价位;⑥ 均线性质直接沿用'均线性质'行(现价在均线上方=下方支撑,下方"
        "=上方压力),不得写反。\n"
        "11. **区间引用**:引用区间边界(上沿/下沿)必须整体引用数据快照给出的区间对(如'近20日"
        "区间 0.604-0.799'),不得只取区间内的某个均线值(如 30分 M20=0.735)冒充区间边界。\n"
        "12. **大盘联动术语**:描述大盘与标的联动时,用'联动拖累''β压制''大盘环境制约'等准确"
        "术语;不得用'跷跷板'(跷跷板=大盘涨标的跌的负相关,与联动拖累含义相反)。\n"
        "13. **锐度与去重**:开篇'多周期矛盾定性'先给一句有判断力的结论(如'弱反弹后再回踩、"
        "反弹空间受限'),再给矛盾依据,禁止罗列指标状态;同一事实(如 KDJ 状态)全报告只出现一次,"
        "后续周期验证不得复述。\n"
        "14. **方向自洽(策略动词)**:策略中每个操作价位,其动词(突破/回踩/跌破/企稳)必须与现价"
        "相对该位的位置自洽——现价下方的位只能触发'回踩企稳/跌破认错',现价上方的位只能触发"
        "'放量突破/突破后回踩确认';'回踩'=价格已从上方回落至某位,现价还在某位下方时禁止说"
        "'回踩该位'(现价在其下方只能'突破'它)。\n"
        "15. **目标锚定技术位**:上涨/下跌目标必须直接引用数据快照中的技术位(均线、近20日高低、"
        "阶段高点、区间边界;支撑带/压力带可引用'均线性质'行的分组),或整数关口(如 0.70);严禁"
        "用百分比从任何价位推算新目标(如'0.721-10%'、'现价+3%')。数据快照没有合适技术位时,"
        "写'暂无明确技术支撑/压力',不得编造新价位。\n"
        "16. **ETF 资金解读**:标的为 ETF 时,内外盘/大单净流入参考性弱(套利机制下不代表真实"
        "多空),资金信号以数据快照的'份额'/'估算规模'与'折溢价'为主——份额增长且溢价=资金流入"
        "偏好,份额下降或折价=资金流出/抛压;不要过度解读内外盘。\n\n"
        f"【数据快照】\n{snapshot_md}\n\n"
        "输出结构(按此输出 markdown):\n"
        f"# 盘中深入分析 {facts['date']} {facts['now']} · {etf['name']}({etf['code']})\n"
        "## 一、多周期矛盾定性\n"
        "第一句直接给最有判断力的结论(如'弱反弹后再回踩、反弹空间受限'),随后用 2-3 句点出"
        "日线 vs 30分 vs 分时之间的矛盾依据。\n"
        "## 二、日线定势\n"
        "分析趋势、均线压力带、MACD 方向、KDJ 超卖/超买的实质含义;必须用阶段高点回撤定性位置"
        "(深跌反弹 vs 高位横盘)。\n"
        "## 三、30分钟与分时微观验证\n"
        "分析日内高低点、量能背后的多空态度、小级别动能是否衰竭、盘口博弈。\n"
        "## 四、大盘环境钳制\n"
        "分析上证指数对标的的制约或托举作用。\n"
        "## 五、综合应对逻辑\n"
        "只给一种最合理的仓位管理策略:写明'突破哪里加仓'和'跌破哪里必须砍仓',以及止损认错的价格锚点;"
        "每个操作的动词必须与现价相对该位的位置自洽(下方位说回踩/跌破,上方位说突破),目标价锚定技术位。\n"
        "## ⚠️ 风险提示\n"
        "## 操作参数(必须输出,严格格式)\n"
        "以下四行,每个价位必须直接来自数据快照(3位小数),严禁用百分比推算;方向约束:突破加仓位"
        "必须高于现价,跌破砍仓位与止损认错位必须低于现价。\n"
        "- 核心观察点: <一句话>\n"
        "- 突破加仓位: <价位> (<技术位名称>)\n"
        "- 跌破砍仓位: <价位> (<技术位名称>)\n"
        "- 止损认错位: <价位> (<技术位名称>)\n"
    )


def _llm_analysis(facts: dict, snapshot_md: str, cfg) -> str | None:
    """LLM 生成深入分析。失败返回 None(调用方降级确定性信号)。"""
    from ..llm import chat

    prompt = _analysis_prompt(facts, snapshot_md)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg,
                    temperature=0.3, max_tokens=2200)
        return text.strip() if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("盘中深入分析 LLM 失败: %s", exc)
        return None


def _label_price_map(snapshot_md: str) -> dict[str, float]:
    """从数据快照【标的】表解析 技术位标签 → 价位(用于操作参数标签校验)。

    只解析标的表(## 大盘 之前),避免被大盘数值覆盖。
    三列结构: | 周期 | 指标 | 数值 |。标签规范化:去空格
    (如 '30分 M20' → '30分M20'),日线均线同时注册 'M20' 与 '日线M20',
    30分均线注册 '30分M{p}'(30分行已拆为 均线/KDJ/MACD 三行)。
    """
    etf_part = snapshot_md.split("## 大盘")[0]
    m: dict[str, float] = {}

    def add(k: str, v):
        if v is not None:
            m[k] = float(v)

    for period, indicator, value in re.findall(
            r"\| (分时|日线|30分|量能|盘口|资金) \| ([^|]+) \| ([^|]+) \|",
            etf_part):
        if indicator == "现价":
            p = re.search(r"([\d.]+)", value)
            if p:
                add("现价", p.group(1))
        elif indicator == "日内高低":
            hm = re.search(r"([\d.]+)@.*?/ ([\d.]+)", value)
            if hm:
                add("日内最高", hm.group(1))
                add("日内最低", hm.group(2))
        elif indicator == "位置":
            sh = re.search(r"距阶段高点\(近250日\) ([\d.]+)", value)
            if sh:
                add("阶段高点", sh.group(1))
            rn = re.search(r"近20日区间 ([\d.]+)-([\d.]+)", value)
            if rn:
                add("近20日区间下沿", rn.group(1))
                add("近20日区间上沿", rn.group(2))
        elif indicator == "均线" and period == "日线":
            for p, v in re.findall(r"M(\d+)=([\d.]+)", value):
                add(f"M{p}", v)
                add(f"日线M{p}", v)
        elif indicator == "均线" and period == "30分":
            # 2026-08-29:30分 均线行从"均线/KDJ/MACD"合并行拆分独立,标签照旧注册
            for p, v in re.findall(r"M(\d+)=([\d.]+)", value):
                add(f"30分M{p}", v)
        elif indicator == "区间" and period == "30分":
            rng = re.search(r"近60根区间 ([\d.]+)-([\d.]+)", value)
            if rng:
                add("近60根区间下沿", rng.group(1))
                add("近60根区间上沿", rng.group(2))
    return m


def _validate_analysis(analysis: str, snapshot_md: str,
                       etf_price: float | None = None) -> str:
    """后校验(确定性兜底):价位存在性 + 操作参数方向自洽 + 标签与价位一致。

    防 LLM 编造价位(如"止损 0.715(10日均线)"——0.715 不在快照中)、方向矛盾
    (如"回踩 0.742"——0.742 在现价上方只能突破不能回踩)、标签错配
    (如"0.732 (30分M20)"——快照中 30分M20=0.735,0.732 是分时采样价)。
    校验集 = 数据快照中所有三位小数数值(价格/MACD 等,含大盘)。
    操作参数块:突破加仓位须高于现价,跌破砍仓位/止损认错位须低于现价;
    参数标签须与快照中该技术位的实际值一致。
    发现问题 → 追加警示行,不修改正文。
    """
    notes: list[str] = []
    valid = set(re.findall(r"\d+\.\d{3}(?!\d)", snapshot_md))
    bad = sorted(p for p in set(re.findall(r"\d+\.\d{3}(?!\d)", analysis)) if p not in valid)
    if bad:
        notes.append(f"以下价位不在数据快照中({'/'.join(bad)}),疑似 LLM 编造")
    if etf_price is not None:
        for field, must in (("突破加仓位", "above"), ("跌破砍仓位", "below"),
                            ("止损认错位", "below")):
            m = re.search(rf"{field}[:：]\s*(\d+\.\d{{3}})", analysis)
            if m:
                v = float(m.group(1))
                bad_dir = (must == "above" and v <= etf_price) or \
                          (must == "below" and v >= etf_price)
                if bad_dir:
                    notes.append(f"操作参数'{field}' {v:.3f} 与现价 {etf_price:.3f} 方向矛盾"
                                 f"({'应高于现价' if must == 'above' else '应低于现价'})")
    label_map = _label_price_map(snapshot_md)
    if label_map:
        for field in ("突破加仓位", "跌破砍仓位", "止损认错位"):
            m = re.search(rf"{field}[:：]\s*(\d+\.\d{{3}})\s*\(([^)]+)\)", analysis)
            if m:
                v, label = float(m.group(1)), m.group(2).strip()
                norm = label.replace(" ", "")
                if norm in label_map and abs(v - label_map[norm]) > 1e-9:
                    notes.append(f"操作参数'{field}' {v:.3f} 标注为 {label},"
                                 f"但快照中 {label}={label_map[norm]:.3f}")
    if not notes:
        return analysis
    logger.warning("深入分析后校验: %s", "; ".join(notes))
    return analysis + "\n\n> ⚠️ **后校验警示**:\n> " + "\n> ".join(notes) + \
        "\n> 请以上方「数据快照」卡为准,勿据此操作。"


# --------------------------------------------------------------------------- 规则降级

def _rule_signals(facts: dict) -> list[str]:
    """规则降级用:确定性信号 + 止损锚点(不编造,只引用事实)。"""
    out = []
    etf = facts["etf"]
    d, mn = etf["daily"], etf["minute"]
    ob = etf.get("orderbook") or {}
    price = mn.get("price")
    if price is not None:
        m250 = d["ma"].get(250)
        if m250 is not None:
            out.append(f"现价{'站上' if price >= m250 else '跌破'}年线 M250({m250:.3f})"
                       + ("——关键支撑,需收盘确认" if price >= m250 else "——下方空间打开"))
        below = [p for p in (5, 10, 20, 60, 100, 120)
                 if d["ma"].get(p) is not None and price < d["ma"][p]]
        if below:
            out.append("短期均线压制(M" + "/M".join(str(p) for p in below) + ")")
        avg = mn.get("avg")
        if avg is not None:
            out.append("现价低于均价·日内抛压占优" if price < avg else "现价高于均价·日内承接占优")
        if etf.get("swing_dd") is not None and d.get("swing_high") is not None:
            out.append(f"距阶段高点 {d['swing_high']:.3f} 回撤 {etf['swing_dd']:.2f}%")
    j = d["kdj"].get("J")
    if j is not None:
        out.append(f"日线KDJ {'超卖(J<20)' if j < 20 else '超买(J>80)' if j > 80 else '中性'} J={j}")
    macd_s = ind.macd_state(d["macd"])
    if macd_s != "-":
        out.append(f"日线MACD {macd_s}")
    vr = etf.get("vol_ratio")
    if vr is not None:
        label = "放量" if vr >= 1.5 else "缩量" if vr <= 0.8 else "平量"
        out.append(f"量比 {vr}({label})")
    # 盘口要点(卖一也可能 price=None → 先防御再格式化)
    if (ob.get("buy") and ob["buy"][0].get("price") is not None
            and ob.get("sell") and ob["sell"][0].get("price") is not None):
        b1, s1 = ob["buy"][0], ob["sell"][0]
        out.append(f"盘口:买一 {b1['price']:.3f}/{_w(b1.get('vol'))} "
                   f"卖一 {s1['price']:.3f}/{_w(s1.get('vol'))}")
    # 止损锚点(证伪条件)
    m20 = d["ma"].get(20)
    r20_lo = d.get("recent_low")
    if price is not None and m20 is not None:
        target = f"看近20日低 {r20_lo:.3f}" if r20_lo else "减仓"
        out.append(f"证伪:收盘跌破 M20({m20:.3f}) → {target}")
    return out or ["数据不足,仅作事实展示"]


def _degraded_analysis(facts: dict) -> str:
    """LLM 不可用时的深入分析降级段:确定性信号 + 止损锚点(不编造解读)。

    数据快照由 build_tech_facts 单独渲染,本段只出规则信号,不重复事实表。
    """
    etf = facts["etf"]
    lines = [f"# 盘中深入分析 {facts['date']} {facts['now']} · {etf['name']}({etf['code']})", "",
             "> ⚠️ LLM 未启用/失败,以下为基于数据快照的确定性信号,不构成投资建议。", ""]
    for s in _rule_signals(facts):
        lines.append(f"- {s}")
    lines += ["", "> 完整事实见上方「数据快照」卡(程序计算,不受本段影响)。"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- 验证层·第二轨(LLM 风控复核)

def _review_prompt(analysis_md: str, snapshot_md: str) -> str:
    """风控复核 prompt(Consistency Checking):只读不改,输出矛盾清单。"""
    return (
        "你是交易风控审核员。给你：(A) 程序计算的事实卡片(数据快照+盘面状态,其中的"
        "状态词/操作参数/大盘方向定性/KDJ标注/位置档/相对强弱是**确定性事实**);(B) 一份 AI 初稿报告。\n"
        "逐条检查报告是否有以下问题,输出**矛盾清单**(markdown 列表,每行一条):\n"
        "- [硬伤] 结论与事实卡片矛盾(如大盘程序定性=系统性压制,报告却写'中性偏暖';"
        "或相对强弱卡=跑输,报告写'跑赢');\n"
        "- [可疑] 无事实依据的定性(如'下轨附近'而事实是46%中下轨,或'动能积蓄'而事实是超买区J上翘);\n"
        "- [注水] 结论无方向判断/整句复述事实或状态词;\n"
        "- [轻微] 重复表述、病句、未量化模糊词;\n"
        "- [硬伤·事实卡内部] 若卡片自身标注与卡片内数字矛盾(如'跑赢3.31pp'而同期五日"
        "标的-5.843% vs 大盘-2.53% 实为跑输),也列出——这类是程序标注疑似错误,报告只是照抄。\n"
        "**硬伤/硬伤·事实卡内部 条目必须同时给出修正建议**,格式:"
        "- [硬伤] 原文「报告原句」→ 事实「卡片事实」→ 修正「建议替换句」;\n"
        "规则:① 只基于事实卡片核对,不得臆造新事实或新价位;修正句禁止含'建议类别/买入/卖出"
        "/立即加仓/立即减仓'等指令词,禁止白名单外价位;② 若全部通过,输出一行:"
        "'复核通过:未发现与事实卡片矛盾的定性';③ 输出中禁止出现'建议类别'字样,"
        "禁止给任何买卖建议。\n\n"
        "【A 事实卡片】\n" + snapshot_md + "\n\n【B 初稿报告】\n" + analysis_md +
        "\n\n输出矛盾清单:"
    )


def _parse_review_fixes(review: str) -> list[tuple[str, str]]:
    """解析复核清单中的硬伤自愈条目: [(原文子句, 修正句), ...];不含修正的硬伤不参与自愈。"""
    items: list[tuple[str, str]] = []
    for m in re.finditer(r"\[硬伤[^\]]*\]\s*原文「([^」]{2,160})」[^\n]*?修正「([^」]{2,300})」",
                         review or ""):
        items.append((m.group(1).strip(), m.group(2).strip()))
    return items


def _apply_review_fixes(md: str, items: list[tuple[str, str]],
                        snapshot_md: str, anchors: dict) -> tuple[str, bool]:
    """硬伤自愈(v2.6):把复核指出的原文子句替换为修正句(仅[硬伤]且有修正建议者)。

    白名单:修正句价位必须 ∈ 快照(非盘口)+锚点;禁指令词/建议类别;子句须在正文中精确出现。
    自愈后整篇由调用方再过一遍 sanitize,防止修正句引入新问题。
    """
    if not items or not anchors or not anchors.get("ok"):
        return md, False
    allowed: set[str] = set()
    for line in snapshot_md.splitlines():
        if not (line.startswith("| 盘口 |") or line.startswith("| 资金 |")):
            allowed |= set(re.findall(r"\d+\.\d{3}(?!\d)", line))
    for v in (anchors.get("anchor_price"), anchors.get("breakout_add"),
              anchors.get("pullback_add"), anchors.get("cut_loss"),
              anchors.get("stop_loss")):
        if v is not None:
            allowed.add(f"{float(v):.3f}")
    text, changed = md, False
    for clause, fix in items:
        if re.search(r"建议类别|买入|卖出|立即加仓|立即减仓|马上买|马上卖", fix):
            continue
        if set(re.findall(r"\d+\.\d{3}(?!\d)", fix)) - allowed:
            continue
        if clause in text:
            # 2026-08-29:修改静默生效,不再标注"(风控复核修正)"(用户只看修改好的结果)
            text = text.replace(clause, fix, 1)
            changed = True
        # 子句无法精确匹配(LLM 引文有出入)→ 跳过该条自愈,保留复核标注
    return text, changed


def _merge_review(md: str, review: str) -> tuple[str, bool]:
    """(v3.2 弃展示)复核清单只入日志,不再追加到报告输出。

    2026-08-29 用户拍板:复核发现问题应直接修改报告,把修改好的结果展示;
    「风控复核」审计段不是给用户看的自然语言,不再展示。硬伤修复由
    _maybe_llm_review 回填正文;可疑/注水(观点分歧)不机器纠正,记日志。
    """
    if (review or "").strip():
        logger.info("风控复核结果(仅日志,不展示): %s", review[:400])
    return md, False


def _maybe_llm_review(md: str, snapshot_md: str, cfg,
                      state: dict | None = None, anchors: dict | None = None,
                      catalyst: dict | None = None) -> str:
    """验证层第二轨(v3.2):LLM 风控复核 → 硬伤自愈(回填正文),不追加复核段。

    2026-08-29 用户拍板:只展示"修改好的报告",复核段不再追加。
    硬伤 → 修正句替换正文并标注"(风控复核修正)";可疑/注水/轻微 → 仅日志
    (观点分歧不机器纠正)。失败/无修正 → 原样返回(不追加任何审计文本)。
    """
    try:
        review = _llm_chat(_review_prompt(md, snapshot_md), cfg, max_tokens=900)
        if not review:
            return md
        items = _parse_review_fixes(review)
        if items and anchors:
            fixed, changed = _apply_review_fixes(md, items, snapshot_md, anchors)
            if changed:
                # 2026-08-29 修复:verifier 修正后【不再整体跑 sanitize】。
                # sanitize 的规则16/19 会误删合法 LLM 段(如"现价位于区间 41%"
                # 被当模糊词、引用现价 0.846 被当盘口价),导致整段丢空。
                # _apply_review_fixes 已只替换硬伤句且不新增价位,直接采用。
                md = fixed
        merged, _appended = _merge_review(md, review)
        return merged
    except Exception as exc:  # noqa: BLE001
        logger.warning("风控复核失败(跳过): %s", exc)
        return md


# --------------------------------------------------------------------------- 反事实推演(盘后·情景分支,2026-08-26)

def _migration_table(facts: dict) -> tuple[list[str], list[str]]:
    """确定性迁移表:跌破/站上当前档后的逐级观察档(只取白名单位源,LLM 不得越界)。

    下行链 = 现价下方全部 支撑类+止损类(降序);上行链 = 结构压力类+均线压力带(升序)。
    返回 (下行段, 上行段),每段形如 "跌破 0.836(30分M10) → 观察 0.822(30分区间低)"。
    """
    from .intraday_engine import _classify_sources

    etf = facts.get("etf") or {}
    d = etf.get("daily") or {}
    m = etf.get("m30") or {}
    mn = etf.get("minute") or {}
    price = mn.get("price")
    if price is None:
        return [], []
    support, struct_p, resist_band, stop_src, labels = _classify_sources(d, m, mn, price)
    down = sorted(set(support) | set(stop_src), reverse=True)
    up = sorted(set(struct_p) | set(resist_band))

    def seg(v, nxt, verb):
        s = f"{verb} {v:.3f}({labels.get(v) or '白名单'})"
        return s + (f" → 观察 {nxt:.3f}({labels.get(nxt) or '白名单'})" if nxt
                    else " → 更远无白名单档(分段由目标位分级表补充)")
    down_lines = [seg(lv, down[i + 1] if i + 1 < len(down) else None, "跌破")
                  for i, lv in enumerate(down)]
    up_lines = [seg(lv, up[i + 1] if i + 1 < len(up) else None, "站上")
                for i, lv in enumerate(up)]
    return down_lines, up_lines


def _counterfactual_prompt(snapshot_md: str, analysis_md: str,
                           down_lines: list[str], up_lines: list[str],
                           scenario: str | None) -> str:
    """反事实推演 prompt:事实卡片 + 当日报告 + 迁移表 → LLM 只做情景叙事。"""
    return (
        "你是情景推演员。基于【今日收盘事实卡片】(含盘面状态=确定性结论)与【当日深入分析】,"
        "做一次反事实推演(假设性,非当下操作指令)。\n"
        f"用户情景: {scenario or '(默认:承压/修复两分支)'}\n"
        "输出结构:\n"
        "- 承压分支:若 {冲击/触发} 出现 → 沿迁移表下行,逐级说明 到哪档、该档承接/性质如何"
        "(支撑转压力?恐慌积累?左侧候选是否更新);\n"
        "- 修复分支:若 放量站上某档 → 沿迁移表上行,说明压力带如何转换为支持、到哪一档;"
        "核心矛盾一句总结。\n"
        "纪律:① 只引用迁移表与事实卡片中的价位,禁止自造任何新价位;② 假设推演,"
        "不出现'建议类别/买入/卖出/立即加仓/立即减仓'等指令字样;③ 每步格式 "
        "'若{触发} → 沿迁移表至{价位+位源} → {判断}';④ 若某档在迁移表之外,写'超出白名单,不推演'。\n\n"
        "【今日收盘事实卡片】\n" + snapshot_md +
        (f"\n\n【当日深入分析】\n{analysis_md}" if analysis_md else "") +
        "\n\n【迁移表·下行】\n" + "\n".join(down_lines) +
        "\n\n【迁移表·上行】\n" + "\n".join(up_lines) +
        "\n\n输出情景推演:"
    )


def _check_counterfactual(md: str, snapshot_md: str, down_lines: list[str],
                          up_lines: list[str]) -> tuple[str, list[str]]:
    """反事实输出校验:价位白名单(迁移表+快照非盘口) + 剥离建议类别/指令词。"""
    issues: list[str] = []
    allowed: set[str] = set()
    for l in down_lines + up_lines:
        allowed |= set(re.findall(r"\d+\.\d{3}", l))
    for line in snapshot_md.splitlines():
        if not (line.startswith("| 盘口 |") or line.startswith("| 资金 |")):
            allowed |= set(re.findall(r"\d+\.\d{3}(?!\d)", line))
    kept: list[str] = []
    for line in (md or "").splitlines():
        if "建议类别" in line or re.search(r"(买入|卖出|立即加仓|立即减仓|马上买|马上卖)", line):
            issues.append(f"指令性/类别表述已剔除: {line.strip()[:30]}")
            continue
        bad = sorted(p for p in set(re.findall(r"\d+\.\d{3}(?!\d)", line)) if p not in allowed)
        if bad:
            issues.append(f"越界价位({'/'.join(bad)})句已剔除: {line.strip()[:30]}")
            continue
        kept.append(line)
    return "\n".join(kept), issues


def build_counterfactual(code: str, cfg: Config | None = None, now: datetime | None = None,
                         scenario: str | None = None,
                         analysis_md: str | None = None) -> tuple[str, bool]:
    """反事实推演(盘后·情景分支):收盘快照 + 确定性迁移表 → LLM 情景推演 → 白名单校验。

    建议在收盘后手动运行(先看当天完整盘面,再推"若明天"分支)。
    返回 (markdown, ok);LLM 失败 → 纯迁移表降级(零幻觉)。
    """
    from .intraday_engine import param_anchors, render_state_md, signal_state

    cfg = cfg or Config()
    now = now or datetime.now()
    facts = _load_facts(code, cfg, now)
    state = signal_state(facts)
    anchors = param_anchors(facts)
    snapshot_md = _facts_markdown(facts) + render_state_md(facts, state, anchors, None, None)
    down_lines, up_lines = _migration_table(facts)
    if not down_lines and not up_lines:
        return snapshot_md + "\n\n> ⚠️ 反事实推演:档位候选不足,无法构建迁移表。", False
    prompt = _counterfactual_prompt(snapshot_md, analysis_md or "", down_lines, up_lines, scenario)
    text = _llm_chat(prompt, cfg, max_tokens=800)
    body = "# 反事实推演(盘后·情景分支)\n> ⚠️ 情景推演,非投资建议;价位链由程序生成。\n\n"
    if text:
        md, issues = _check_counterfactual(text, snapshot_md, down_lines, up_lines)
        if issues:
            logger.warning("反事实推演校验: %s", "; ".join(issues))
        return body + (md or "(推演内容为空)"), True
    # LLM 失败降级:确定性迁移表(零幻觉,足够回答"跌破X看哪档")
    degraded = (body
                + "LLM 不可用,以下为程序生成的确定性迁移表(供自行推演):\n\n"
                + "**下行(跌破逐级观察)**\n" + "\n".join(" - " + l for l in down_lines) + "\n\n"
                + "**上行(站上逐级测试)**\n" + "\n".join(" - " + l for l in up_lines))
    return degraded, False


# --------------------------------------------------------------------------- 对外(v2.0 编排层)

def _llm_chat(prompt: str, cfg, max_tokens: int = 2600, temperature: float = 0.3) -> str | None:
    """统一 LLM 调用:失败返回 None(调用方降级确定性信号)。

    2026-08-29:每次调用前小间隔(+0.6s),防免费档(智谱等)连续调用 429 限流
    ——实测 9 次连续无间隔时段 2-9 全回退,加间隔 8/8 成功。
    """
    import time
    time.sleep(1.5)
    from ..llm import chat

    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg,
                    temperature=temperature, max_tokens=max_tokens)
        return text.strip() if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("盘中深入分析 LLM 失败: %s", exc)
        return None


def _analysis_prompt_v2(facts: dict, snapshot_md: str, state: dict,
                        anchors: dict, catalyst: dict | None = None,
                        conclusion_card: dict | None = None) -> str:
    """v2.8 骨架填空 prompt(M3'):十段结构 + 结论卡先行引用(prompt 输出结构 → 正文不再含 JSON)。"""
    import json as _json

    etf = facts["etf"]
    risk = (catalyst or {}).get("risk_level", "正常")
    rs_5d = facts.get("rs_5d")
    card_note = ""
    if conclusion_card:
        try:
            card_note = ("程序已先行生成结论卡(下方 JSON;正文与之一致:禁止改其价位/方向/信号类型"
                         "时效,允许用散文展开其逻辑):\n"
                         + _json.dumps({"conclusion": conclusion_card}, ensure_ascii=False)
                         + "\n\n")
        except Exception:  # noqa: BLE001
            card_note = ""
    return (
        "你是资深 A股中长线 ETF 技术分析研究员。下方数据快照由程序精确计算,末尾"
        "「盘面状态」段(场景/状态词/锚定价/操作参数/风险等级)是**确定性结论**,\n"
        "不得修改、不得自行重算;其余数字只准引用快照。\n\n"
        "硬性规则(逐条遵守):\n"
        "1. 场景、状态词、风险等级、锚定价、操作参数一律**照抄**「盘面状态」段,禁止改写、"
        "禁止自造新词;\n"
        "2. 所有价位保留 3 位小数,必须来自快照;严禁百分比推算新价位;\n"
        "3. 目标只取快照关键位(均线/区间上下沿/阶段高低/整数关口)的相邻档;\n"
        "4. ETF 资金面只准引用 份额/折溢价;内外盘只做盘口说明,不给多空结论;\n"
        "5. 动词方向与现价自洽:现价下方的位只说 回踩/跌破/企稳,上方只说 突破/站上/测试;\n"
        "6. 单日截面不得出现'右侧确认'(确认需跨日达成,本报告只有 左侧/蓄势/右侧初现);\n"
        "7. 不罗列数据表,把数字转化为市场语言;同一事实全报告只出现一次;\n"
        "8. 当前风险等级 = {risk}:风险等级只影响开仓/加仓(关注=暂停加仓批,升级=风险上升、"
        "当日禁止新开仓/追单),不影响持有的止损纪律;\n"
        "9. **每段三段式(事实→分析→结论)**:每段先写【事实】行(只引用快照数字,标注来源如"
        "'份额较上期 -34.42%'或'MACD 红柱·柱缩小'),再写【分析】(推演链条,写'因为A所以B';"
        "遇到周期/资金矛盾必须先给**优先级判定**:谁约束当前、谁主导方向),最后【结论】一句话"
        "= 触发条件 + 动作 + 价位 + 失效(参照盘面状态档位);\n"
        "10. **操作建议必须结构化**(禁止无条件结论):只用'若 {触发(价位+确认刻度如放量/日线收盘/"
        "30分K收盘)} → 则 {动作 + 档位};失效 = {价位}'句式;禁止'可考虑加仓/可择机试多'等"
        "无触发条件措辞;\n"
        "11. **试多纪律**:状态词=左侧观望/蓄势时结论必须是'不试多、观察';写'试多'前必须先列"
        "§4.2 准入链(超跌/恐慌证据/性质/位置/大盘)哪条满足哪条缺失;跌破支撑后的'试多'一律错误"
        "(破位=趋势破坏,禁止承接);\n"
        "12. **矛盾优先级规则**:若快照 30分KDJ 超买(J>80)且日线KDJ 超卖(J<20),段三必须先写"
        "'(程序规则)30分超买约束短线反弹高度(执行刻度),日线超卖主导中线观察',再展开;\n"
        "13. **相对强弱强制**:快照有'相对强弱(近5日)'行时,段一必须引用该行数字并判定"
        "(如'标的近5日跑输大盘 Xpp'→弱于大盘,分析是板块自身问题还是跟随调整);\n"
        "14. **KDJ 三线结构**(解读词典《kdj指标macd指标分析》):必须引用快照程序标注的"
        "排向(K>D 多排 / K<D 空排)与 J 开口(J上翘/J深低于K/三线收敛/三线共振);"
        "J 深低于K(倒喇叭)= 超卖修复动能积蓄;J 上翘 = 短线过热回调压力;三线收敛 = 变盘临界;\n"
        "15. **MACD 三维度**:必须区分零轴语境(零上=多头环境/零下=空头环境/穿越)+ 红柱/绿柱缩放;"
        "**零下红柱=空头环境中的弱反弹动能,不是趋势转多**;\n"
        "16. **联动优先级(程序规则)**:日线 MACD 定方向、30分 KDJ 定买卖点;"
        "日线零下(空头环境)时:30分 超买=回调压力、30分 超卖不必然是买点(可能下跌中继);"
        "日线零上时:30分 超卖=低吸机会。段二/段三必须写出这句联动判定;\n"
        "17. **位置与回撤引用**:引用区间位置用快照程序档(下轨区/中下轨/中上轨/上轨区),"
        "禁止'下轨附近/上轨附近/底部附近'等无依据模糊词;引用阶段高点回撤必须完整引"
        "'距阶段高点(近250日) X@日期 回撤 Y%'(含价位与日期),禁止省略;\n"
        "18. **术语与纪律**:'背离'仅指价格-指标背离(如顶背离=价格创新高而MACD未创新高);"
        "日线/30分 方向相反只能用'周期冲突/矛盾/对抗';禁止把本 prompt 的规则原文写进报告"
        "(规则是给分析师的约束,不是报告内容);\n"
        "19. **大盘两维综合(解耦)**:段一必须引用快照大盘'趋势维度(程序)'与'动能维度(程序)'两行,"
        "先分别复述两维事实,再自行综合定性(偏暖/中性/压制);禁止把两维事实说反"
        "(如 DEA 零下 却写'MACD 零上'/DIF 零下 却写'动能翻多');'中性偏暖/回暖/托举'等"
        "综合措辞允许,但必须建立在两维事实之上,且不得谎称两维同时满足;\n"
        "20. **结论公式(防注水)**:每段【结论】= '当前{状态},但因{关键压制/矛盾} → {唯一判断};"
        "若{触发(价位+确认刻度)}则{动作};失效={价位}'。结论必须含'因/但'引导的独立判断,"
        "禁止整句复述【事实】或把状态词/操作参数照抄当结论('市场状态为左侧观望'不是结论,"
        "'操作参数已明确'不是结论);\n"
        "21. **各段分工防复读**:段二=日线方向、段三=30分节奏与可执行性、段四=日内结构、"
        "段五=关键位距离、段八=唯一操作逻辑;禁止多段使用'需回踩确认支撑'等同一句式;\n"
        "22. **操作价位纪律**:触发/失效价位必须是程序锚点(突破/回踩带/减仓/清仓止损/反弹压力带)"
        "或快照关键位标签(均线/区间上下沿/阶段高低/日内高低);盘口五档挂单价(买1-5/卖1-5)不得用作"
        "操作价位;写'失效'必须带来源(如'失效=30分K收盘跌破0.836(回踩带下沿)');\n"
        "23. **KDJ 区间语境**:30分KDJ 超买区(J>80)J上翘=情绪过热回调风险(禁止'动能积蓄/修复');"
        "超卖区(J<20)J深低于K=反弹修复积蓄;企稳信号只许用 C8' 三选一(缩量十字星后放量阳/"
        "日线KDJ低位金叉(J<30 K上穿D)/重新站回日线M20),禁止'J转负/J开口收窄'作企稳;\n"
        "24. **量比无量纲**:引用'量比 0.73'原值,禁止加%或改数值(0.54% 是现价vs均价偏离,不是量比)。\n"
        "25. **必采清单(缺失必须自标)**:以下数据点必须出现在报告,任一缺失在对应段落标注"
        "[数据缺失: xxx] 而非含糊跳过——相对强弱(近5日)行 / 大盘'趋势维度+动能维度'两行 / 份额与折溢价 / "
        "上下压力支撑各至少一档(带距现价%) / 结论卡全部字段。\n"
        "26. **ETF 制度约束(恒量遵守)**:T+1 当日买入不可当日卖;资金面主信号=份额变化与折溢价"
        " (±0.5% 内为温和);内外盘/大单参考弱(套利机制);ETF 尾盘常有申赎收敛,尾盘形态降权;"
        "做市商成交为噪声,量能信号权重低于个股。\n\n"
        f"【数据快照】\n{snapshot_md}\n\n{card_note}"
        "输出结构(段落标题照抄;每段 = 一句【事实】+ 2-3 句【分析】+ 一句【结论】):\n"
        "# 盘中深入分析\n"
        "## 一、大盘环境\n(先引用快照大盘'趋势维度+动能维度'两行事实,再综合定性;"
        "再分析对标的的压制/托举)"
        "【相对强弱】\n"
        + (f"程序提示:标的近5日 {'跑输' if rs_5d is not None and rs_5d < 0 else '跑赢' if rs_5d is not None and rs_5d > 0 else '持平'}"
           f"大盘 {abs(rs_5d or 0):.2f}pp,段一必须引用并分析。\n" if rs_5d is not None else "")
        + "## 二、标的·日线\n(趋势/均线性质/MACD/KDJ,用阶段高点回撤定性位置)\n"
        "## 三、标的·30分\n(与日线的共振或矛盾;**遇 30分KDJ 超买+日线KDJ 超卖必须先给优先级判定**)\n"
        "## 四、分时盘口\n(日内高低/量能/盘口博弈;外内盘结论词照抄盘口行一次,必须带"
        "'ETF套利机制下参考性有限'限定,禁止复读、禁止当多空结论;ETF 不引申多空)\n"
        "## 五、关键价位分级表\n(上方压力/下方支撑各给 1-2 档,只取快照相邻档,每档附"
        "'距现价 x%';均线压力带由程序给出,如'反弹压力带 0.845-0.851(日线M5/M20)',"
        "不得当突破档)\n"
        "## 六、操作参数\n(逐字照抄「盘面状态」段的程序锚点:突破/回踩/减仓/清仓止损;"
        "禁止润色措辞、禁止改价、禁止删档;若显示'止损 缺档'或'回踩 无结构',照抄原文,"
        "不得写'止损暂未设定')\n"
        "## 七、市场状态\n(照抄场景与状态词,一句话;本段不承担结论)\n"
        "## 八、操作建议(统一逻辑,不分持有/新开——每笔=新交易,只问盘面)\n"
        "(单一逻辑阶梯:① 当前结论一句(如'现价 0.838 位于回踩带 0.836-0.837 上沿,30分超买未化解,"
        "不追多');② 近端触发:'若价格回踩 {回踩带下沿} 且 30分 J 回落至 80 以下并出现企稳信号,"
        "进入观察(试多仍需 §4.2 四链,当前缺 {缺项})';③ 中端:'放量+日线收盘站上 {突破档} 才谈"
        "右侧初现';④ 持有护栏:'30分K收盘跌破 {减仓档} → 减仓,跌破 {清仓止损档} → 清仓';"
        "结尾统一写'风险等级 {risk}:止损纪律按档位执行')\n"
        "## 九、风险提示与企稳信号\n(该离场的位 + 什么信号才算转好 + 左侧试多候选缺项)\n"
        "## 十、资金面(强制段,必须输出)\n(引用快照'份额/估算规模/折溢价';份额环比为负且"
        "幅度≥10% 时定性为'中长期资金流出',与短线技术面做矛盾推演;"
        "折溢价解读映射:份额大减且折溢价温和(±0.5%内)→ 一级市场赎回主导(机构端离场),"
        "非散户二级恐慌;折价扩大(≤-0.5%)→ 二级抛压叠加;严禁以'资金面信号参考性弱'整段跳过;"
        "内外盘只做盘口说明)\n"
        "## 建议类别: <加仓|买入|试多|减仓|砍仓|清仓|持有|观望|等待确认>\n"
        "(最后一行只输出白名单内的一个词)\n"
    )


def _ensure_fund_section(md: str, snapshot_md: str) -> tuple[str, list[str]]:
    """D3 资金段机械兜底(v2.2):快照有份额数据但 LLM 文本未分析资金面 → 程序插入规则句。

    8.25 光伏实测:LLM 把模板占位符当标题输出、内容为空。此函数保证资金面
    (半年级别份额变化)绝不因 LLM 疏忽在正文消失。
    """
    notes: list[str] = []
    etf_part = snapshot_md.split("## 大盘")[0]
    if "| 资金 | 份额 |" not in etf_part:
        return md, notes
    if re.search(r"份额|折溢价|申购|赎回|资金流(入|出)", md):
        return md, notes
    m = re.search(r"较上期\s*([+-]?[\d.]+)亿份\(([+-]?[\d.]+)%\)", etf_part)
    if not m:
        return md, notes
    chg, pct = m.group(1), m.group(2)
    try:
        pct_f = float(pct)
    except ValueError:
        return md, notes
    if abs(pct_f) < 10:
        return md, notes
    direction = ("半年级别资金大幅流出" if pct_f <= -10
                 else "半年级别资金流入" if pct_f >= 10 else "资金面中性")
    note = (f"## 十、资金面(程序兜底,确定性事实)\n"
            f"标的份额较上期 {chg}亿份({pct}%),{direction}(快照数据)。"
            f"LLM 正文未做资金面分析,由程序补入;建议与短线技术面做矛盾推演"
            f"({'资金流出压制反弹高度' if pct_f <= -10 else '资金流入或支撑反弹'})。")
    # 清掉 LLM 留下的空资金面占位标题行("十、资金面..." 或 "## 十、资金面...")
    lines = [l for l in md.splitlines()
             if not re.match(r"^#* *十[、．.]?\s*资金面", l)]
    body = "\n".join(lines).strip()
    if "建议类别:" in body:
        body = body.replace("## 建议类别:", note + "\n## 建议类别:", 1)
    else:
        body = body + "\n\n" + note
    notes.append(f"资金面缺位,程序兜底插入(份额 {pct}%)")
    return body, notes


def _archive_advice(code: str, facts: dict, snapshot_md: str, analysis_md: str,
                    state: dict, anchors: dict, catalyst: dict | None,
                    category: str, degraded: bool) -> None:
    """当日建议存档(M1' 归档,MVP 幂等:同交易日期只留最新一份)。"""
    try:
        from ..db import connect, init_db
        conn = connect()
        init_db(conn)
        conn.execute(
            "DELETE FROM advice_archive WHERE trade_date=? AND symbol=? AND created_at=?",
            (str(facts.get("date", "")), code, str(facts.get("now", ""))))
        conn.execute(
            "INSERT INTO advice_archive (trade_date, symbol, created_at, anchor_price,"
            " scenario, state_word, risk_level, category, advice_md, snapshot_md, degraded)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(facts.get("date", "")), code, str(facts.get("now", "")),
             anchors.get("anchor_price"), state.get("scenario"), state.get("state_word"),
             (catalyst or {}).get("risk_level"), category, analysis_md, snapshot_md,
             1 if degraded else 0))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("建议归档失败(%s): %s", code, exc)


def _load_facts(code: str, cfg: Config, now: datetime) -> dict:
    """取大盘 + 选中标的事实合集。标的行情完全取不到 → ValueError(调用方转 502)。"""
    code = str(code).split(".")[0]
    etf = _fetch_etf(code, now)
    if not etf["daily"] and not etf["minute"]:
        raise ValueError(f"标的 {code} 行情获取失败")
    etf["name"] = _name_of(cfg, code)
    idx = _fetch_index(now)
    # C7 相对强弱近5日:标的5日跌幅 − 大盘5日跌幅(pp,正=跑输)
    rs_5d = None
    if etf.get("chg5d") is not None and idx and idx.get("chg5d") is not None:
        rs_5d = round(etf["chg5d"] - idx["chg5d"], 2)
    return {"now": now.strftime("%H:%M"),
            "date": etf.get("date") or now.strftime("%m-%d"),
            "etf": etf, "index": idx, "rs_5d": rs_5d}


def _panel_payload(code: str, facts: dict, state: dict, anchors: dict,
                   catalyst: dict) -> dict:
    """悬浮面板标记载荷(里程碑7):确定性,由「盘面状态」程序输出直接带出。

    字段全来自 intraday_engine 计算好的 dict,零成本;前端以现价 vs anchors
    实时重算"距最近档 %",但 marker 文本本身(状态词+风险)不覆盖。
    未生成快照 → 前端不调本函数,行保持灰行无标记。
    """
    anchors_map = {}
    if anchors.get("ok"):
        for k, v in (("突破加仓", anchors.get("breakout_add")),
                     ("回踩加仓", anchors.get("pullback_add")),
                     ("砍仓", anchors.get("cut_loss")),
                     ("止损", anchors.get("stop_loss"))):
            if v is not None:
                anchors_map[k] = _r3(v)
    return {
        "code": code,
        "status_word": state.get("state_word") or "数据不足",
        "scenario": state.get("scenario") or "-",
        "risk": (catalyst or {}).get("risk_level", "正常"),
        "anchors": anchors_map,
        "pullback_zone": anchors.get("pullback_zone"),
        "no_pullback": bool(anchors.get("no_pullback")),
        "ok": bool(anchors.get("ok")),
        "updated_at": facts.get("now"),
    }


def build_tech_facts(code: str, cfg: Config | None = None,
                     now: datetime | None = None) -> str:
    """数据快照卡(v3.1):纯事实表(标的+大盘),不含「盘面状态」段。

    2026-08-29 用户拍板:「盘面状态(程序输出,LLM 不得修改)」是程序内部决策
    标签/参数,不是给用户看的自然语言,不再展示;决策信息由深入分析卡
    (自然语言)与结论卡(结构化)承载。LLM 输入内部仍含盘面状态(build_tech_analysis
    自行拼接),不受影响——"用户看到的数字 = 模型看到的数字"仍成立
    (价位源是快照表,盘面状态不引入新数字)。
    """
    facts = _load_facts(code, cfg or Config(), now or datetime.now())
    return _facts_markdown(facts)


def build_tech_panel(code: str, cfg: Config | None = None,
                     now: datetime | None = None) -> dict:
    """悬浮面板标记载荷入口:数据快照附带返回的确定性 panel 段。

    与 build_tech_facts 共用同一计算链,保证"标记 = 快照盘面状态逐字一致"。
    数据源不可用时抛 ValueError → API 层转 502(前端灰行,不给臆造标记)。
    """
    from .intraday_engine import (catalyst_context, holding_context, param_anchors,
                                  signal_state)

    cfg = cfg or Config()
    now = now or datetime.now()
    facts = _load_facts(code, cfg, now)
    state = signal_state(facts)
    holding = holding_context(cfg)
    anchors = param_anchors(facts, holding)
    catalyst = catalyst_context(facts, state, cfg)
    return _panel_payload(code, facts, state, anchors, catalyst)


_CANDIDATE_TEMPS = (0.25, 0.55, 0.85)


def _gen_candidates(prompt: str, snapshot_md: str, state: dict, anchors: dict,
                    catalyst: dict | None, cfg, card_md: str,
                    n: int) -> tuple[list[str], list[list[str]]]:
    """verifier 生产路径:正文候选 ×n(温度变体)→ sanitize 过滤,候选附结论卡评审视图。

    返回 (候选列表[正文+卡段], 各候选规则链 issues)。候选全部被剔 → 空列表。
    """
    from .intraday_engine import sanitize_v2

    candidates: list[str] = []
    issues_hist: list[list[str]] = []
    for i in range(max(1, n)):
        t = _CANDIDATE_TEMPS[i % len(_CANDIDATE_TEMPS)]
        text = _llm_chat(prompt, cfg, max_tokens=2600, temperature=t)
        if not text:
            continue
        cand, degraded, cand_issues = sanitize_v2(text, snapshot_md, state,
                                                  anchors, catalyst)
        if not degraded and cand.strip():
            candidates.append(cand + "\n\n【结论卡】\n" + card_md)
            issues_hist.append(cand_issues)
        else:
            logger.info("候选#%d 被规则链剔除(deg=%s)", i, degraded)
    return candidates, issues_hist


def _fill_segments_sequential(facts: dict, snapshot_filtered: str, state: dict,
                              anchors: dict, baselines: dict[str, str],
                              card_md: str, cfg,
                              seg_names: list[str]) -> dict[str, str]:
    """LLM 片段填充(顺序执行,不并发;2026-08-29 实测:并发3 触发智谱 429 导致
    多数段回退底线句 → 输出变成'全是规则句';顺序 8 段全成功)。

    每段独立调用,失败段回退底线句。返回 {段名: 文本}。
    """
    from .segments import segment_prompt

    results: dict[str, str] = {}

    def worker(seg: str, baseline: str) -> None:
        prompt = segment_prompt(seg, baseline, snapshot_filtered, facts, state,
                                anchors, card_md)
        text = _llm_chat(prompt, cfg, max_tokens=700, temperature=0.4)
        # 空 / 只回段名(LLM 常犯:把标题当内容)/ 无实质内容 → 一律回退底线句
        stripped = (text or "").strip()
        seg_like = re.sub(r"\s", "", stripped).lower()
        seg_key = re.sub(r"\s", "", seg).lower()
        if (not stripped or seg_like == seg_key
                or len(stripped) < 8):
            results[seg] = baseline
            return
        # 段级校验(轻量):不做全量价位白名单(LLM 在底线句约束下引用 MACD/KDJ 数值
        # 会被误删),只拦 禁词 / 动词方向 / 盘口价当操作位 / 身份动词 / 内外盘结论。
        try:
            body = text.strip()
            # 剥掉前导段名
            body = re.sub(rf"^\s*{re.escape(seg)}\s*[:：]?\s*", "", body).strip()
            import re as _re
            # 禁词(右侧确认等,单日截面)
            from .intraday_engine import BANNED_WORDS
            if any(w in body for w in BANNED_WORDS):
                results[seg] = baseline
                return
            # 动词方向:上方只准突破/站上,下方只准回踩/跌破/企稳(对照锚定价)
            price = anchors.get("anchor_price")
            if price is not None:
                for verb, pos in _re.findall(
                        r"(突破|回踩|跌破|企稳|站上|失守)\s*(\d+\.\d{2,3})", body):
                    try:
                        v = float(pos)
                    except ValueError:
                        continue
                    if not (0 < v < 100):
                        continue
                    bad_dir = ((verb in ("回踩", "跌破", "企稳") and v > price) or
                               (verb in ("突破", "站上", "失守") and v < price))
                    if bad_dir:
                        results[seg] = baseline
                        return
            # 盘口/资金行数值不得当操作位:仅当价位数命中盘口/资金行数值
            # 且不在【关键位白名单】(现价/均线/区间/锚点)时,才视为误用回退。
            # (2026-08-29 修:LLM 正常引用现价 0.846 也会命中盘口买1价,
            #  不能在无关键位排除的情况下整段回退)
            op_verb_re = re.compile(r"失效|触发|^跌破|^站上|突破|回踩|减仓|止损|清仓|加仓")
            bad_prices = set()
            for line in (snapshot_filtered or "").splitlines():
                if line.startswith("| 盘口 |") or line.startswith("| 资金 |"):
                    bad_prices |= set(re.findall(r"\d+\.\d{3}(?!\d)", line))
            if bad_prices and op_verb_re.search(body):
                # 关键位白名单:快照非盘口/资金行的所有 3 位小数(现价/均线/区间/KDJ等)
                price_whitelist = set()
                for line in (snapshot_filtered or "").splitlines():
                    if not (line.startswith("| 盘口 |") or line.startswith("| 资金 |")):
                        price_whitelist |= set(re.findall(r"\d+\.\d{3}(?!\d)", line))
                for v in (anchors.get("anchor_price"), anchors.get("breakout_add"),
                          anchors.get("pullback_add"), anchors.get("cut_loss"),
                          anchors.get("stop_loss")):
                    if v is not None:
                        price_whitelist.add(f"{float(v):.3f}")
                # 只有"操作动词 + 命中盘口价且非关键位"的组合才回退
                body_prices = set(re.findall(r"\d+\.\d{3}(?!\d)", body))
                misused = body_prices & bad_prices - price_whitelist
                if misused:
                    results[seg] = baseline
                    return
            # 过短(仅段名/无实质)回退底线句
            if len(re.sub(r"\s", "", body)) < 8:
                results[seg] = baseline
                return
            results[seg] = body or baseline
        except Exception:  # noqa: BLE001
            results[seg] = baseline

    for seg in seg_names:
        baseline = baselines.get(seg, "")
        if not baseline:
            continue
        worker(seg, baseline)
    # 兜底:未完成的段(异常/缺失)用底线句
    for seg in seg_names:
        if seg not in results:
            results[seg] = baselines.get(seg, "")
    return results


def _strip_card_section(md: str) -> str:
    """从评审视图(正文+卡段)剥离【结论卡】附加段,返回正文。"""
    if not md:
        return ""
    for i, line in enumerate(md.splitlines()):
        if re.match(r"^#{0,3}\s*【?结论卡】?", line.strip()):
            return ("\n".join(md.splitlines()[:i])).rstrip()
    return md


def _sanitize_internal_labels(md: str) -> str:
    """后处理过滤器(2026-08-29 系统修复#⑦):把一切内部编号/黑话替换为自然语言。

    所有 LLM 输出在展示前统一过此函数,确保用户看不到 C8'/§4.2/盘面状态
    等内部标识。替换后再检查一次,防 LLM 变体。
    """
    if not md:
        return md
    repl = [
        (r"C8'\s*三?选一|C8 企稳信号|企稳信号按\s*C8'?", "企稳信号三选一"),
        (r"§\s*4\.2|4\.2\s*准入", "试多准入条件"),
        (r"「盘面状态」|盘面状态段|盘面状态", "程序计算结果"),
        (r"程序锚点\s*[:：]", "关键价位参考:"),
        (r"\bM3'\b|校验链|sanitize|白名单", "程序校验"),
    ]
    for pat, sub in repl:
        md = re.sub(pat, sub, md)
    # 残余内部标识检查(防漏):C8/§ 数字形式
    md = re.sub(r"\bC8['′]?\b", "企稳", md)
    md = re.sub(r"§\s*\d+(\.\d+)?", "规则", md)
    return md


def _state_meaning(state: dict) -> str:
    """市场状态一行自然语言(2026-08-29 系统修复#⑧:去黑话/去重复)。"""
    sw = state.get("state_word") or ""
    sub = state.get("sub_state") or ""
    meaning = {
        "左侧观望": "价格已超跌,但尚未出现恐慌性抛售,也没有右侧反转信号,因此不急于试多,继续观察",
        "超跌试多": "超跌且有恐慌性抛售证据、位置到位,可轻仓试多一次(不超过总资金3%)",
        "蓄势": "回踩进行中、量能萎缩,等待企稳信号,暂不加仓",
        "右侧初现": "当日放量站上关键位,待日线收盘确认(可考虑不超过满配10%的试探)",
        "破位退出": "放量跌破关键位,按减仓/清仓档位递进离场",
        "区间震荡": "均线粘合、成交平淡,区间内小仓位波段,不视为趋势启动",
        "变盘前兆": "均线粘合但持续放量,可能突破,先做准备不反向操作",
    }.get(sw)
    if not meaning:
        return ""
    return f"\n释义:{meaning}。" + (f"当前细节:{sub}。" if sub and sub != sw else "")


def build_tech_analysis(code: str, cfg: Config | None = None,
                        now: datetime | None = None) -> tuple[str, bool]:
    """深入分析卡(v3.0,2026-08-29 架构转向):骨架底线句 + LLM 逐段填充。

    与 v2 的区别:不再是"LLM 一次生成 2000+ token 长文",而是——
    1. 程序生成各段「底线句」(事实+方向,确定性);
    2. LLM 每段独立扩展填充(并发 ≤3),只准在底线句上扩写,不得改数值/方向;
    3. 每段独立 sanitize,违规/失败段回退底线句 → 输出永远完整可读;
    4. 六/七/建议类别/结论卡 = 纯程序输出,零 LLM;
    5. 内外盘/资金·信号行从 LLM 输入剔除(程序自标参考性弱,不制造矛盾)。
    返回 (markdown, degraded)。数据快照由 build_tech_facts 单独生成。
    """
    from .intraday_engine import (catalyst_context, holding_context, param_anchors,
                                  render_state_md, sanitize_v2, signal_state,
                                  suggest_category)
    from .report_schema import (
        generate_conclusion_card, render_conclusion_card)
    from .segments import (assemble_report, filter_analysis_input,
                           skeleton_baselines)

    cfg = cfg or Config()
    now = now or datetime.now()
    facts = _load_facts(code, cfg, now)
    state = signal_state(facts)
    holding = holding_context(cfg)
    anchors = param_anchors(facts, holding)
    catalyst = catalyst_context(facts, state, cfg)
    snapshot_md = _facts_markdown(facts) + render_state_md(facts, state, anchors,
                                                           catalyst, holding)
    # 结论卡先行(v2.8):独立小调用先生成结构化结论卡(JSON 单一结构,免费档服从率高);
    # 校验不过/调用失败 → 程序兜底(零 LLM)。
    from .verifier import load_evidence
    prior_evidence = load_evidence(code)
    card, _card_src = generate_conclusion_card(snapshot_md, anchors, state, cfg,
                                               prior_evidence=prior_evidence)
    card_md = render_conclusion_card(card)
    category = suggest_category("## 建议类别: 观望", state)   # 程序默认,LLM 不产类别

    # ---- 关键:深入分析输入 = 快照过滤内外盘/资金信号(避免"一边说参考性弱一边分析")----
    snapshot_filtered = filter_analysis_input(snapshot_md)

    # 骨架底线句(程序确定性)
    baselines = skeleton_baselines(facts, state, anchors, catalyst)

    # LLM 填充段(2026-08-29 结构优化:五=程序表零 LLM,八=资金面;六合并八+九)
    seg_names = ["一、大盘环境", "二、标的·日线", "三、标的·30分", "四、分时盘口",
                 "六、操作建议与风险提示", "八、资金面"]
    segments = _fill_segments_sequential(facts, snapshot_filtered, state, anchors,
                                         baselines, card_md, cfg, seg_names)
    # 纯程序段(零 LLM;2026-08-29:五=关键价位表,七=市场状态)
    bo = anchors.get("breakout_add")
    cut = anchors.get("cut_loss")
    stop = anchors.get("stop_loss")
    zone = anchors.get("pullback_zone")
    rb = anchors.get("resist_band") or []
    rb_src = anchors.get("resist_band_src") or {}
    zone_txt = ""
    if zone:
        up, lo = zone.get("upper"), zone.get("lower")
        zh = (f"{zone.get('lower_src') or ''}–{zone.get('upper_src') or ''}")
        zone_txt = f"{lo}-{up}" if lo and up and abs(lo - up) > 1e-9 else f"{up}"
    # 五、关键价位表(2026-08-29:#⑤ 系统性修复——表格文案与状态词护栏自洽:
    # 左侧观望/蓄势下突破档只写"观察/右侧初现候选",不写"可试多"(试多=左侧概念)
    # 超跌试多/右侧初现才写"可试多")
    sw = state.get("state_word") or ""
    breakout_note = {
        "超跌试多": "放量+日线收盘站上可试多",
        "右侧初现": "放量+日线收盘站上确认右侧初现",
        "右侧确认": "放量+日线收盘站上确认右侧",
    }.get(sw, "放量+日线收盘站上为右侧初现观察(当前不试多)")
    table_rows = [
        f"| 突破观察 | {bo if bo is not None else '-'} | {anchors.get('breakout_src') or '白名单'},{breakout_note} |",
    ]
    if rb:
        rb_txt = "、".join(f"{v}({rb_src.get(_r3(v)) or '日线均线'})" for v in rb[:3])
        table_rows.append(f"| 反弹压力 | {rb_txt} | 上方压力,反弹受阻参考,不追 |")
    if zone:
        table_rows.append(
            f"| 回踩观察 | {zone_txt} | {zone.get('upper_src') or ''},回踩观察企稳信号 |")
    if cut is not None:
        table_rows.append(f"| 减仓 | {cut} | 跌破减仓({anchors.get('cut_src') or '白名单'}) |")
    if stop is not None:
        table_rows.append(f"| 清仓止损 | {stop} | 跌破止损({anchors.get('stop_src') or '白名单'}) |")
    price_tbl = _fmt_price(anchors.get("anchor_price"))
    program_sections = {
        "五、关键价位表": (
            f"现价 {price_tbl} 对应的关键价位:\n\n"
            "| 类型 | 价位 | 说明 |\n|---|---|---|\n" + "\n".join(table_rows)
        ),
        "七、市场状态": (
            f"{state.get('scenario', '-')} · {state.get('state_word', '-')}"
            f"({' '.join(filter(None, [state.get('sub_state')])) if state.get('sub_state') else ''})"
            + _state_meaning(state)
        ),
    }
    md = assemble_report(segments, baselines, program_sections, category)
    # 资金段兜底(LLM 十段失败时基线已含,此处再保险)
    md, _fund_notes = _ensure_fund_section(md, snapshot_md)
    # verifier 生产收尾(v2.11):整篇复核 + 硬伤自愈(不再干预片段生成)
    verifier_on = bool(cfg.get("llm", "verifier", default=True))
    if verifier_on:
        md = _maybe_llm_review(md, snapshot_md, cfg, state, anchors, catalyst)
    # 结论卡统一插入一次(2026-08-29:assemble 不再放卡,避免重复)
    md = "\n".join(l for l in md.splitlines()
                   if not re.match(r"^#{0,2}\s*\*?\*?结论卡\*?\*?\s*$", l)).strip()
    if "## 建议类别:" in md:
        md = md.replace("## 建议类别:", card_md + "\n\n## 建议类别:", 1)
    else:
        md = md.rstrip() + "\n\n" + card_md
    # 内部标识后处理(2026-08-29 系统修复#⑦:任何内部编号/黑话不得泄漏给用户)
    md = _sanitize_internal_labels(md)
    _archive_advice(code, facts, snapshot_md, md, state, anchors, catalyst,
                    category, False)
    return md, False