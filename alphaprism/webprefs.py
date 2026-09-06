"""运行时 Web 偏好(config/web.yaml):刷新间隔等。

Web 行情页与悬浮面板是两个进程(Flask 服务 / `alphaprism panel` pywebview 窗),
各自持有独立 Config()。放主配置里改动要重启、且跨进程不同步;
故把「可运行时调整、需两进程共享」的前端偏好独立为一个轻量 yaml 文件,
读取带默认值兜底,写入全量覆盖(DEFAULTS 之外的不认识的键被过滤)。

默认刷新间隔 12 秒:候选股池每次刷新会对池内每只标的做一次实时行情请求
(同花顺 fuyao,限频 4001;失败自动降级腾讯免费接口)。6 只池 → 12s ≈ 30次/分钟,
请求量约为原 60s 的 5 倍,属可接受上限边缘,再快容易触发限频。可在设置 Tab 调整。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .paths import USER_CONFIG_DIR

PREFS_FILE = USER_CONFIG_DIR / "web.yaml"

DEFAULTS: dict = {
    "refresh_interval_sec": 12,  # 顶部指数 + 候选股池自动轮询间隔(秒)
    "panel_start_hidden": False,  # 托盘面板:启动即藏入系统托盘(True=只见托盘,False=显示面板)
}

# 允许被前端写入的键(白名单,防止 Web 端误写其它配置)
_WRITABLE: frozenset[str] = frozenset(DEFAULTS.keys())


def load_prefs() -> dict:
    """读取偏好文件,缺失键用 DEFAULTS 兜底;文件不存在则为全默认。"""
    data: dict = {}
    try:
        raw = yaml.safe_load(PREFS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001  损坏时退回默认,不让前端崩
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def refresh_interval_sec() -> int:
    """当前刷新间隔(秒),兜底默认。"""
    try:
        return max(1, int(load_prefs().get("refresh_interval_sec", DEFAULTS["refresh_interval_sec"])))
    except (TypeError, ValueError):
        return int(DEFAULTS["refresh_interval_sec"])


def save_prefs(patch: dict) -> dict:
    """保存偏好(仅白名单内键),返回合并后全量。"""
    cur = load_prefs()
    for key, value in patch.items():
        if key in _WRITABLE:
            cur[key] = value
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(yaml.safe_dump(cur, allow_unicode=True, sort_keys=False),
                          encoding="utf-8")
    return cur
