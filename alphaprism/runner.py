"""每日运行编排:抓取 → 入库 → 信号日志 → 生成日报 → 推送。供 run_daily.py 与 cli.py 共用。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config
from .db import connect, init_db
from .pipeline import refresh_signal_log, run_pipeline
from .report import build_daily_report, write_report


def run_daily(cfg: Config | None = None, symbols: list[str] | None = None) -> tuple[list[dict], Path]:
    """执行抓取 → 入库 → 信号日志 → 生成日报 → 推送,返回 (summary, 报告路径)。"""
    cfg = cfg or Config()
    events: list[dict] = []
    conn = connect()
    init_db(conn)
    try:
        summary = run_pipeline(conn, cfg, symbols=symbols)
        events = refresh_signal_log(conn, cfg, symbols)  # 信号日志(§6.4)
    finally:
        conn.close()

    conn = connect()
    report_md = ""
    try:
        latest = conn.execute("SELECT MAX(trade_date) FROM etf_kline_daily").fetchone()[0]
        report_md = build_daily_report(conn, cfg, summary)
        stamp = latest or datetime.now().strftime("%Y-%m-%d")
        path = write_report(cfg.get("report_dir", default="reports"), report_md, stamp)
        # 推送(§6.3):信号事件强提醒 + 简报静默;失败不中断
        from .push import notify_daily

        notify_daily(events, report_md, cfg)
    finally:
        conn.close()
    return summary, path
