"""盘中监测定时入口(计划任务每 15 分钟用)。

脚本自检:非交易日 / 非交易时段(9:30-11:30,13:00-15:00)直接退出。
日志:控制台 + data/run_monitor.log(UTF-8)。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphaprism.config import PROJECT_ROOT
from alphaprism.monitor import run_monitor

_log_file = PROJECT_ROOT / "data" / "run_monitor.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger("run_monitor")


def main() -> None:
    triggered = run_monitor(push=True)
    if triggered:
        logger.info("本次触发 %d 条", len(triggered))


if __name__ == "__main__":
    main()
