"""TickerView 打包入口(PyInstaller onedir → TickerView.exe)。

冻结后 exe 直接进托盘宿主:run_panel 内部按需拉起 Flask Web 服务 + pywebview 悬浮面板 +
系统托盘。刻意不走 scripts/cli.py(那会连带 backtest/walkforward/wind 等无关重模块)。
"""
from __future__ import annotations

import sys

from alphaprism.planner.floatpanel import run_panel

if __name__ == "__main__":
    run_panel()
    sys.exit(0)
