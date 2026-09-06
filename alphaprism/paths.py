"""路径解析:区分「只读资源」与「可写用户数据」,兼容开发态与 PyInstaller 打包态。

- 开发态(未冻结):两者都指向 repo,行为与历史完全一致(零回归)。
- 打包态(PyInstaller onedir,frozen):
    · 只读资源(static / settings.yaml 模板 / assets)从 sys._MEIPASS 取;
    · 可写数据(settings.local.yaml / watchlist.yaml / web.yaml / SQLite)统一落到
      %APPDATA%\\TickerView,避免写进只读或每次解压变动的 bundle 目录。

其它模块统一从这里取路径,不要再各自 Path(__file__) 拼。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_FROZEN = bool(getattr(sys, "frozen", False))

_REPO_ROOT = Path(__file__).resolve().parent.parent  # alphaprism/ 的上一级 = repo 根

# 只读资源根:打包 = PyInstaller 解包根(_MEIPASS);开发 = repo 根
BUNDLE_ROOT = Path(sys._MEIPASS) if IS_FROZEN else _REPO_ROOT  # type: ignore[attr-defined]


def _user_root() -> Path:
    if not IS_FROZEN:
        return _REPO_ROOT
    base = os.environ.get("APPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Roaming")
    return root / "TickerView"


USER_ROOT = _user_root()

# 只读:随包分发的出厂配置模板目录
BUNDLE_CONFIG_DIR = BUNDLE_ROOT / "config"
# 可写:用户配置与数据
USER_CONFIG_DIR = USER_ROOT / "config"
USER_DATA_DIR = USER_ROOT / "data"

# 兼容旧命名:CONFIG_DIR 现指「可写用户配置目录」(dev 下 == repo/config)
CONFIG_DIR = USER_CONFIG_DIR
DATA_DIR = USER_DATA_DIR
DB_PATH = DATA_DIR / "alphaprism.db"

# 具体文件
DEFAULT_CONFIG = BUNDLE_CONFIG_DIR / "settings.yaml"     # 只读模板
LOCAL_CONFIG = USER_CONFIG_DIR / "settings.local.yaml"   # 可写覆盖(含 API key)
WATCHLIST_FILE = USER_CONFIG_DIR / "watchlist.yaml"      # 可写


def ensure_user_dirs() -> None:
    """首启建目录,并把出厂 watchlist.yaml 播种到用户目录(若尚无)。dev 下 no-op。"""
    if not IS_FROZEN:
        return
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    seed = BUNDLE_CONFIG_DIR / "watchlist.yaml"
    if seed.exists() and not WATCHLIST_FILE.exists():
        try:
            WATCHLIST_FILE.write_bytes(seed.read_bytes())
        except OSError:
            pass
