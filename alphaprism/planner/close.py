"""盘后生成(产品方案 §5.3 盘后 tab,里程碑4):收盘对账 + 复盘五问 + 纪律评分。

规则驱动(§5.4):引擎用速查表 levels + 当日收盘价算事实(结论词),LLM 只做复盘五问措辞。
LLM 不可用 → 降级为纯规则对账(不编造,只给事实)。

输出:收盘对账 markdown,可追加写入作战地图"每日盯盘记录"(格式:### 📅 日期 盘后 时段)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from ..config import Config
from ..fetchers import fuyao
from ..fetchers.etf_kline import fetch_minute
from .checker import check_instrument
from .rulemodel import RuleModel

logger = logging.getLogger(__name__)

_WEEKDAYS = "一二三四五六日"


def _weekday(d: date) -> str:
    return _WEEKDAYS[d.weekday()]


def _close_price(symbol: str) -> tuple[float | None, float | None]:
    """当日收盘价 + 涨跌幅(腾讯分时最后一根 + 昨收)。失败返回 (None, None)。"""
    try:
        df = fetch_minute(symbol)
        if df.empty:
            return None, None
        last = df.iloc[-1]
        close = float(last["price"])
        prev = float(last["prev_close"]) if last["prev_close"] is not None else None
        chg = (close / prev - 1) * 100 if prev else None
        return close, chg
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] 当日收盘获取失败: %s", symbol, exc)
        return None, None


def _index_close() -> tuple[float | None, float | None, float | None]:
    """上证指数收盘 + 涨跌 + 门控 J 值。失败返回 (None, None, None)。

    注意:上证指数腾讯符号是 sh000001,不能走 fetch_minute(其 _tx_symbol
    对 "000001" 会生成 sz000001=平安银行)。这里直接调腾讯分钟接口。
    """
    try:
        from ..fetchers.etf_kline import HEADERS, _get_json
        from .checker import kdj_j
        from .live import _index_closes

        closes = _index_closes()
        js = _get_json("https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                       {"code": "sh000001"})
        data = (js.get("data") or {}).get("sh000001") or {}
        points = (data.get("data") or {}).get("data") or []
        if not points:
            return None, None, (kdj_j(closes) if closes else None)
        last = points[-1].split()
        close = float(last[1]) if len(last) > 1 else None
        # 昨收:qt 列表 index 4
        prev = None
        qt = data.get("qt")
        if isinstance(qt, dict):
            for v in qt.values():
                if isinstance(v, list) and len(v) > 4:
                    try:
                        prev = float(v[4])
                        break
                    except (ValueError, TypeError):
                        continue
        chg = (close / prev - 1) * 100 if close and prev else None
        return close, chg, (kdj_j(closes) if closes else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("上证指数收盘获取失败: %s", exc)
        return None, None, None


# ---------------------------------------------------------------- 规则层(事实)

def _rule_review(model: RuleModel) -> list[dict[str, Any]]:
    """每只标的:当日收盘 → 结论词 + 量能(纯规则,不依赖 LLM)。"""
    rows = []
    for instr in model.instruments:
        close, chg = _close_price(instr.code)
        snap = None
        try:
            snap = fuyao.fetch_fund_snapshot(instr.code)
        except Exception:  # noqa: BLE001
            snap = None
        vr = None
        if snap and snap.get("turnover_ratio_pct") is not None:
            vr = float(snap["turnover_ratio_pct"]) / 2  # 收盘换手率近似量比(粗)
        v = check_instrument(instr, close, vr, True, chg)
        d = v.to_dict()
        d["close"] = close
        d["change_pct"] = chg
        rows.append(d)
    return rows


# ---------------------------------------------------------------- LLM 层(复盘五问)

_QUESTIONS = [
    "大盘环境如何?(趋势 / 压力支撑 / J 值位置)",
    "各标的处于哪个阶段?(回踩 / 突破 / 持有 / 预警)",
    "今天加/减仓了吗?依据哪个信号?",
    "哪一步判断错了?(信号失真 or 执行变形)",
    "明日计划:谁的买点临近?谁的红线接近?",
]


def _llm_review(rows: list[dict], index: dict, cfg) -> str | None:
    """LLM 生成复盘五问解读 + 纪律评分。失败返回 None(调用方降级)。"""
    from ..llm import chat

    lines = ["你是 A股中长线交易系统的盘后复盘助手。基于以下收盘事实,回答复盘五问并给纪律评分。"
             "只输出 markdown,简洁、基于数据,不编造。"]
    lines.append(f"\n【上证指数】收盘 {index.get('close')} ({index.get('change_pct')}%),J={index.get('j')}")
    lines.append("\n【各标的收盘核对】")
    for r in rows:
        near = f" 接近:{r['near']}" if r.get("near") else ""
        lines.append(f"- {r['name']}({r['code']}) 收盘 {r.get('close')} ({r.get('change_pct')}%) "
                     f"结论:{r['conclusion']}{near}")
    lines.append("\n按五问作答:")
    for i, q in enumerate(_QUESTIONS, 1):
        lines.append(f"{i}. {q}")
    lines.append("\n最后给一行纪律评分:满分 100,有违规操作(追高/破位不砍/计划外加减仓)扣分,并说明理由。")
    prompt = "\n".join(lines)
    try:
        text = chat([{"role": "user", "content": prompt}], cfg=cfg, temperature=0.3, max_tokens=800)
        return text.strip() if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘 LLM 失败: %s", exc)
        return None


def _rule_review_fallback(rows: list[dict], index: dict) -> str:
    """LLM 不可用时降级:只给事实对账 + 固定复盘问题(不编造解读)。"""
    lines = []
    for i, q in enumerate(_QUESTIONS, 1):
        lines.append(f"**{i}. {q}**")
    # 用规则事实填部分
    lines.append("\n> (LLM 未配置/失败,以下为规则事实,供人工复盘)")
    lines.append(f"> 上证收盘 {index.get('close')} ({index.get('change_pct')}%),J={index.get('j')}")
    for r in rows:
        lines.append(f"> {r['name']} 收盘 {r.get('close')} ({r.get('change_pct')}%) → {r['conclusion']}"
                     + (f" {r['near']}" if r.get("near") else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- 拼装

def _format_review(rows: list[dict], index: dict, llm_text: str | None,
                   now: datetime | None = None) -> str:
    now = now or datetime.now()
    lines = [f"### 📅 {now.strftime('%Y-%m-%d')}({_weekday(now.date())})盘后 {now.strftime('%H:%M')}"]
    lines += ["", "**收盘对账**:", f"- 大盘:上证收盘 {index.get('close')} ({index.get('change_pct')}%),"
                                   f"门控 J={index.get('j')}", ""]
    lines += ["| 标的 | 收盘 | 涨跌 | 结论 |", "|---|---|---|---|"]
    for r in rows:
        c = f"{r.get('close')}" if r.get("close") is not None else "-"
        chg = f"{r.get('change_pct'):+.2f}%" if r.get("change_pct") is not None else "-"
        near = f" {r['near']}" if r.get("near") else ""
        lines.append(f"| {r['name']} | {c} | {chg} | {r['conclusion']}{near} |")
    lines += ["", "**复盘五问**:", ""]
    if llm_text:
        lines.append(llm_text)
    else:
        lines.append(_rule_review_fallback(rows, index))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 对外

def build_close_review(model: RuleModel, cfg=None, now: datetime | None = None) -> str:
    """生成收盘对账 markdown。规则算事实,LLM 做复盘五问(失败降级规则)。"""
    cfg = cfg or Config()
    index_close, index_chg, index_j = _index_close()
    index = {"close": index_close, "change_pct": index_chg, "j": index_j}
    rows = _rule_review(model)
    llm_text = _llm_review(rows, index, cfg)
    return _format_review(rows, index, llm_text, now)


def append_to_journal(review_md: str, path: str) -> None:
    """把收盘对账追加写入作战地图"每日盯盘记录"段落末尾。

    若文件已有该日期"盘后"条目,则替换;否则追加到段落末尾。
    保持原文件其余内容不动(计划层不被机器污染,仅追加/替换对账块)。
    """
    from pathlib import Path

    p = Path(path)
    text = p.read_text(encoding="utf-8")
    date_md = review_md.splitlines()[0].replace("### 📅 ", "").split("(")[0]  # YYYY-MM-DD
    marker = f"### 📅 {date_md}"

    # 该日期已有条目 → 在其内替换/追加"盘后"子块
    date_idx = text.find(marker)
    if date_idx >= 0:
        day_end = text.find("### 📅", date_idx + len(marker))
        if day_end < 0:
            day_end = len(text)
        block = text[date_idx:day_end]
        if "盘后" in block:
            # 替换该日期下的盘后子块(不重复)
            new_text = text[:date_idx] + review_md + text[day_end:]
            p.write_text(new_text, encoding="utf-8")
            return
        # 该日期有盘前,无盘后 → 在盘前块后插入盘后
        new_text = text[:day_end] + "\n" + review_md + "\n" + text[day_end:]
        p.write_text(new_text, encoding="utf-8")
        return

    # 无该日期条目 → 追加到"每日盯盘记录"段落末尾;无段落则创建
    seg_start = text.find("每日盯盘记录")
    if seg_start >= 0:
        new_text = text.rstrip() + "\n\n" + review_md + "\n"
    else:
        new_text = text.rstrip() + "\n\n## 每日盯盘记录\n\n" + review_md + "\n"
    p.write_text(new_text, encoding="utf-8")
