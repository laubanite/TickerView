"""每日抓取入口(计划任务 / 手动运行)。

运行:
  python scripts/run_daily.py          # 全量跟踪池
  python scripts/run_daily.py 510300   # 只抓单只

日志:控制台 + data/run_daily.log(UTF-8,供计划任务排查)。
注意:若 `python` 在本机是 Windows 商店占位符,请用完整解释器路径。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphaprism.config import PROJECT_ROOT
from alphaprism.runner import run_daily as _run_daily

_log_file = PROJECT_ROOT / "data" / "run_daily.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger("run_daily")


def main() -> None:
    symbols = sys.argv[1:] or None
    summary, path = _run_daily(symbols=symbols)
    logger.info("=== 抓取摘要 ===")
    for r in summary:
        status = "⚠️ " + "; ".join(r["errors"]) if r["errors"] else "ok"
        logger.info("  %s %s: 日线%s 周线%s 30分%s  [%s]",
                    r["symbol"], r["name"], r["daily"], r["weekly"], r["m30"], status)
    logger.info("日报已生成: %s", path)


if __name__ == "__main__":
    main()
