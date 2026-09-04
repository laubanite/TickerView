# -*- coding: utf-8 -*-
"""盘中规则历史回放(2026-09):一行命令出《规则检验报告》+ 最低样本量 Checklist。

用法:
  python scripts/replay_rules.py --pool                    # 预设 13 只 ETF 池
  python scripts/replay_rules.py --symbol 515790 --symbol 159516
        [--start 2021-01-01] [--end 2026-08-20] [--out data/replays/xxx.md]

- 日K历史:data/kline_cache 缓存优先,腾讯拉取兜底(不入库);次新 ETF 按上市日自动截短窗口;
- 大盘日K:同缓存路径(sh000001_daily.csv);
- 输出:逐日信号记录 + 总体/状态词/档位宽度/风控有效性/次日延续性/分半验证 + 全池 Checklist。
"""
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphaprism.planner.replaysim import (  # noqa: E402
    build_report, replay_symbol, ensure_daily_history, load_index_history)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

POOL = {
    "510300": "沪深300", "510500": "中证500", "159915": "创业板",
    "512480": "半导体", "159995": "芯片", "159516": "半导体设备",
    "515790": "光伏", "518850": "黄金", "512400": "有色金属",
    "512010": "医药", "159928": "消费", "512880": "证券", "512660": "军工",
}


def build_checklist(all_rows: list[dict], start: str, end: str) -> str:
    """全池最低样本量 Checklist(Kimi 标准,2026-09)。"""
    dec = [r for r in all_rows if r["direction"]]
    break_n = sum(1 for r in dec if r["state_word"] == "破位退出")
    per_sym: dict[str, int] = {}
    for r in all_rows:
        if r["direction"]:
            per_sym[r["symbol"]] = per_sym.get(r["symbol"], 0) + 1
    avg = sum(per_sym.values()) / len(per_sym) if per_sym else 0.0
    years = sorted({r["date"][:4] for r in all_rows})
    span = (years[-1] if years else "") + " 起 " + str(len(years)) + " 个年份"
    has_2022 = "2022" in years
    ends = f"{years[0]}~{years[-1]}" if years else "-"
    lines = ["## ✅ 全池最低样本量 Checklist(参考 Kimi 标准)", "",
             "| 指标 | 目标 | 实测 | 达标 |", "|---|---|---|---|",
             f"| 总方向性信号 | ≥200 | {len(dec)} | {'✅' if len(dec) >= 200 else '❌'} |",
             f"| 单只平均方向信号 | ≥15 | {avg:.1f} | {'✅' if avg >= 15 else '❌'} |",
             f"| 破位减仓类信号 | ≥50 | {break_n} | {'✅' if break_n >= 50 else '❌'} |",
             f"| 标的数 | ≥10 | {len(per_sym)} | {'✅' if len(per_sym) >= 10 else '❌'} |",
             f"| 时间跨度 | 含2022熊市等 | {ends} | {'✅' if has_2022 else '❌'} |", ""]
    lines += ["> 未达标项 → 继续补样本;达标后且规则参数经「训练/验证分半」确认,再谈实盘。", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="盘中规则历史回放器")
    ap.add_argument("--symbol", action="append", help="ETF 代码(可多次)")
    ap.add_argument("--pool", action="store_true", help="使用预设 13 只 ETF 池")
    ap.add_argument("--start", default="2021-01-01", help="回放起点")
    ap.add_argument("--end", default="", help="回放终点(默认到最后交易日)")
    ap.add_argument("--out", default="", help="报告落盘路径(默认 data/replays/{日期}-规则检验报告.md)")
    args = ap.parse_args()

    symbols = list(args.symbol or []) if args.symbol else []
    if args.pool:
        symbols = list(POOL.keys())
    if not symbols:
        ap.error("需 --symbol 或 --pool")
    names = {**POOL, **{s: s for s in symbols}}

    index_daily = load_index_history()
    out_path = args.out or (Path(__file__).resolve().parent.parent / "data" / "replays"
                            / f"{datetime.now():%Y-%m-%d}-规则检验报告.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sections: list[str] = [f"# AlphaPrism 盘中规则检验报告 · {datetime.now():%Y-%m-%d}", ""]
    all_rows: list[dict] = []
    for sym in symbols:
        print(f"[replay] {sym} {names.get(sym, sym)} 拉取/回放中…")
        daily = ensure_daily_history(sym)
        rows = replay_symbol(sym, names.get(sym, sym), daily, index_daily,
                             args.start, args.end)
        md = build_report(sym, rows, args.start, args.end)
        sections.append(md)
        all_rows += rows
        dec = [r for r in rows if r["direction"]]
        print(f"  → {sym}: 信号日 {len(rows)} / 方向性 {len(dec)}")
    sections.append(build_checklist(all_rows, args.start, args.end))
    sections.append(f"> 由 scripts/replay_rules.py 生成 · 判据:±5%(3/5/10日) · 回放语义:当日收盘决策(日线粒度)")
    out_path.write_text("\n\n---\n\n".join(sections), encoding="utf-8")
    print(f"[replay] 报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())