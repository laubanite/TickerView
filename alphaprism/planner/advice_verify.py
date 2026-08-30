"""盘后增量4 · 建议验证(后端 MVP,交易系统设计 §10 / 产品架构 §五·5.4)。

建议存档(advice_archive)→ N 个交易日后用收盘行情判定(确定性)→
写 advice_verification + 回填 verdict → 分层统计(应验率,供规则校准输入)。

判据(已定稿):方向(优先结论卡 signal_type,回退建议类别)+ ±5% 阈值(初值,待 walk-forward 校准)。
- 看涨(试多候选/右侧初现候选/加仓/买入/试多):N日后收盘 ≥ 锚定价+5% → 应验;0~5% → 部分应验;≤0 → 未应验
- 看跌(减仓参考/破位退出/清仓止损/减仓/砍仓/清仓):N日后收盘 ≤ 锚定价-5% → 应验;-5~0% → 部分应验;≥0 → 未应验
- 无方向(观望/蓄势候选/持有/等待确认):不参与方向判定 → 无法判定
2026-08-27 迁移(借鉴 daily_stock_analysis decision_signal/outcome evaluator):
- 多窗口 HORIZONS=(1,3,5,10)(1d 短线参考 / 3、5 主流 / 10d 中期),direction_correct 列记录方向对错。
验证对象是"规则/阈值质量",不是 LLM 文本(建议由骨架生成、LLM 只转译)。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from ..db import connect, init_db

logger = logging.getLogger(__name__)

HORIZONS = (1, 3, 5, 10)          # 1d 短线参考、3/5 主流、10d 中期(借鉴 outcome evaluator)
VERIFY_THRESHOLD_PCT = 5.0        # 初值,待校准
CATEGORY_UP = {"加仓", "买入", "试多"}
CATEGORY_DOWN = {"减仓", "砍仓", "清仓"}
NO_DIRECTION = {"持有", "观望", "等待确认"}
SIGNAL_UP = {"试多候选", "右侧初现候选"}
SIGNAL_DOWN = {"减仓参考", "破位退出", "清仓止损"}
SIGNAL_NONE = {"观望", "蓄势候选"}


def _signal_type_from_md(advice_md: str) -> str | None:
    """从报告正文的结论卡解析 signal_type(渲染行:"- 信号类型: 观望 · 时效: ...")。"""
    if not advice_md:
        return None
    m = re.search(r"信号类型[:：]\s*([^\s·|]+)", advice_md)
    return m.group(1).strip() if m else None


def _direction(arch: dict) -> str | None:
    """方向:结论卡 signal_type 优先,回退建议类别。返回 'up' / 'down' / None(无方向)。"""
    sig = _signal_type_from_md(str(arch.get("advice_md") or ""))
    if sig in SIGNAL_UP:
        return "up"
    if sig in SIGNAL_DOWN:
        return "down"
    if sig in SIGNAL_NONE:
        return None
    cat = str(arch.get("category") or "")
    if cat in CATEGORY_UP:
        return "up"
    if cat in CATEGORY_DOWN:
        return "down"
    return None


def _verify_one(conn, arch: dict, n_day: int) -> tuple[str, float | None, int | None]:
    """单条建议 N 日验证:返回 (outcome, move_pct, direction_correct)。"""
    sym = str(arch.get("symbol", ""))
    t0 = str(arch.get("trade_date") or "")
    anchor = arch.get("anchor_price")
    direction = _direction(arch)
    if not sym or not t0 or not anchor:
        return "无法判定", None, None
    rows = conn.execute(
        "SELECT close FROM etf_kline_daily WHERE symbol=? AND trade_date > ? "
        "ORDER BY trade_date ASC LIMIT ?", (sym, t0, n_day)).fetchall()
    if len(rows) < n_day:
        return "无法判定", None, None      # 未到期 / K线不足
    c_n = float(rows[n_day - 1]["close"])
    move = round((c_n / float(anchor) - 1) * 100, 2)
    if direction == "up":
        out = ("应验" if move >= VERIFY_THRESHOLD_PCT
               else "部分应验" if move > 0 else "未应验")
    elif direction == "down":
        out = ("应验" if move <= -VERIFY_THRESHOLD_PCT
               else "部分应验" if move < 0 else "未应验")
    else:
        out = "无法判定"
    correct = 1 if out in ("应验", "部分应验") else 0 if out == "未应验" else None
    return out, move, correct


def run_advice_verification() -> int:
    """对所有未验证的存档跑 1/3/5/10 日验证(MVP:CLI/定时调用)。返回本次判定条数。"""
    conn = connect()
    init_db(conn)
    # 旧库迁移:补 direction_correct 列(SQLite 无 IF NOT EXISTS column)
    try:
        conn.execute("ALTER TABLE advice_verification ADD COLUMN direction_correct INTEGER")
    except Exception:  # noqa: BLE001
        pass
    done = 0
    try:
        # 重试语义:verdict=NULL(从未判)或 无法判定(上次数据不足/无方向)都重新尝试;
        # 无方向建议(持有/观望)每次重判结果仍为 无法判定,幂等无害。
        archives = [dict(r) for r in conn.execute(
            "SELECT * FROM advice_archive WHERE verdict IS NULL OR verdict='无法判定' ORDER BY id")]
        for arch in archives:
            for n in HORIZONS:
                outcome, move, correct = _verify_one(conn, arch, n)
                if outcome != "无法判定":
                    conn.execute(
                        "INSERT OR REPLACE INTO advice_verification (archive_id, n_day,"
                        " outcome, move_pct, evidence, direction_correct)"
                        " VALUES (?,?,?,?,?,?)",
                        (arch["id"], n, outcome, move,
                         f"锚定价{arch.get('anchor_price')} → {n}日后 {outcome}",
                         correct))
                    done += 1
            row = conn.execute(
                "SELECT outcome FROM advice_verification WHERE archive_id=? AND n_day=?",
                (arch["id"], HORIZONS[-1])).fetchone()
            verdict = str(row["outcome"]) if row else "无法判定"
            conn.execute("UPDATE advice_archive SET verdict=? WHERE id=?",
                         (verdict, arch["id"]))
        conn.commit()
    finally:
        conn.close()
    return done


def advice_verification_stats(n_day: int = 5, layer: str = "category") -> list[dict]:
    """分层统计(按建议类别或结论卡 signal_type):样本/应验率/方向对错;供规则校准输入。"""
    conn = connect()
    init_db(conn)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT a.category, a.state_word, a.advice_md, v.outcome, v.direction_correct"
            " FROM advice_verification v JOIN advice_archive a ON a.id = v.archive_id"
            " WHERE v.n_day=?", (n_day,))]
    finally:
        conn.close()
    groups: dict[str, dict] = {}
    for r in rows:
        key = (str(r.get("category") or "?") if layer == "category"
               else (_signal_type_from_md(str(r.get("advice_md") or "")) or "未解析"))
        g = groups.setdefault(key, {"n": 0, "应验": 0, "部分应验": 0, "未应验": 0,
                                    "direction_ok": 0, "direction_n": 0})
        g["n"] += 1
        if r.get("outcome") in g:
            g[r["outcome"]] += 1
        if r.get("direction_correct") is not None:
            g["direction_n"] += 1
            if r["direction_correct"]:
                g["direction_ok"] += 1
    out = []
    for cat, g in sorted(groups.items()):
        hit = g["应验"] + g["部分应验"]
        dir_rate = (round(g["direction_ok"] / g["direction_n"] * 100, 1)
                    if g["direction_n"] else None)
        out.append({"category": cat, "n": g["n"],
                    "hit_rate": round(hit / g["n"] * 100, 1) if g["n"] else 0.0,
                    "direction_rate": dir_rate,
                    "samples": g})
    return out


def tuning_report(n_day: int = 5, layer: str = "category") -> str:
    """人工闸门报告(增量4 → 规则校准输入,交易系统设计 §10):应验率 + 动作建议。

    建议动作:应验率≥60% 保持;25%~60% 复核阈值/触发条件;<25% 复核规则;
    样本<10 只展示不下结论(沿用盘前纪律)。
    """
    stats = advice_verification_stats(n_day, layer=layer)
    layer_name = "结论卡信号类型" if layer == "signal_type" else "建议类别"
    lines = [f"# 建议验证报告(窗口 {n_day} 日 · 按{layer_name}) · {datetime.now():%Y-%m-%d}",
             "",
             "> 验证对象 = 建议(规则+转译)整体;低应验率指向触发规则/阈值需复查",
             "> (验证的是策略规则质量,不是 LLM 文本质量)。",
             "",
             "| 分组 | 样本 | 应验率 | 方向对率 | 应验 | 部分 | 未应验 | 动作建议 |",
             "|---|---|---|---|---|---|---|---|"]
    for s in stats:
        g = s["samples"]
        n = s["n"]
        if n < 10:
            action = "样本不足,仅展示"
        elif s["hit_rate"] >= 60:
            action = "保持"
        elif s["hit_rate"] >= 25:
            action = "复核阈值/触发条件"
        else:
            action = "复核规则(低应验)"
        dir_txt = f"{s['direction_rate']}%" if s["direction_rate"] is not None else "-"
        lines.append(f"| {s['category']} | {n} | {s['hit_rate']}% | {dir_txt} | "
                     f"{g['应验']} | {g['部分应验']} | {g['未应验']} | {action} |")
    if not stats:
        lines.append("| - | 0 | - | - | - | - | - | 暂无已判定样本(存档后需等待窗口) |")
    lines += ["", "> 每 2 周人工复核一次;确认后才调整规则(机器提议、人拍板)。"]
    return "\n".join(lines)