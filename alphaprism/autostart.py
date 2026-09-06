"""开机自启开关:注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run。

开发模式(源码运行)写入 value = pythonw.exe <scripts/cli.py> panel(无控制台窗);
打包版(TickerView.exe)写入 value = <exe 路径>。同一注册表键、同一开关语义,
设置 Tab 与未来托盘右键"开机自启"共用本模块。

Windows-only;非 Windows 环境所有函数静默返回默认值。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover 非 Windows 平台
    winreg = None

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "TickerView"


def _launch_command() -> str:
    """写入 Run 键的启动命令。"""
    if getattr(sys, "frozen", False):
        # 打包版:直接指向 exe(自身即托盘宿主)
        return f'"{sys.executable}"'
    # 开发模式:pythonw 静默启动 cli.py panel
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        exe = Path(sys.executable)
    from .config import PROJECT_ROOT

    cli = PROJECT_ROOT / "scripts" / "cli.py"
    return f'"{exe}" "{cli}" panel'


def is_enabled() -> bool:
    """自启是否已注册(检测 Run 键是否存在)."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_NAME)
        return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    """写入或删除 Run 键;幂等。"""
    if winreg is None:
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, _RUN_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, _RUN_NAME)
            except OSError:
                pass
