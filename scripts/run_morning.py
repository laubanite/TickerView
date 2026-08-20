"""盘前简报定时入口(计划任务 8:55 用,9:15 前推送)。

日志:控制台 + data/run_morning.log(UTF-8)。非交易日自动跳过。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphaprism.config import PROJECT_ROOT
from alphaprism.morning import run_morning

_log_file = PROJECT_ROOT / "data" / "run_morning.log"
_log_file.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger("run_morning")


def main() -> None:
    briefing = run_morning(push=True)
    if briefing:
        logger.info("盘前简报已生成(%d 字)并推送", len(briefing))
    else:
        logger.info("盘前简报跳过(非交易日或失败)")


if __name__ == "__main__":
    main()
