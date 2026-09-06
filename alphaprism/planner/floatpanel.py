"""悬浮面板托盘宿主(TickerView,里程碑7·终版)。

面板视觉/数据全部来自网页端悬浮面板(单源):本进程只负责——
 1) 按需拉起 Web 服务(127.0.0.1:8765,若已在跑则复用);
 2) pywebview 无边框置顶小窗加载 http://...:port/?float=1(即网页内那块悬浮面板);
 3) pystray 系统托盘:左键唤醒面板,右键菜单(显示/隐藏/打开行情网页/退出)。

面板页的数据由网页端 Vue 自行轮询(不走 js_api),js_api 仅保留三个桥:
 hide_to_tray / quit_app / open_web(打开完整行情页)。

用法: alphaprism panel
"""
from __future__ import annotations

import logging
import threading
import time
import webbrowser
from pathlib import Path
import os

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _is_up(url: str, timeout: float = 1.2) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _ensure_web(port: int) -> None:
    """确保 127.0.0.1:port 的 Web 服务在跑;不在则线程内拉起。"""
    url = f"http://127.0.0.1:{port}/"
    if _is_up(url):
        return
    from ..web.app import create_app

    application = create_app()
    threading.Thread(
        target=lambda: application.run(host="127.0.0.1", port=port, debug=False, threaded=True),
        daemon=True,
    ).start()
    deadline = time.time() + 20
    while time.time() < deadline:
        if _is_up(url):
            return
        time.sleep(0.3)
    raise RuntimeError(f"Web 服务启动失败(端口 {port})")


def _load_tray_icon():
    """加载托盘图标;缺文件时用 Pillow 现画一个白芯黑框倒三角兜底。"""
    from PIL import Image, ImageDraw

    path = ASSETS_DIR / "tray_icon.png"
    if path.exists():
        return Image.open(path).convert("RGBA")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(8, 10), (56, 10), (32, 56)], fill=(16, 18, 22, 255))
    d.polygon([(16, 17), (48, 17), (32, 48)], fill=(255, 255, 255, 255))
    return img


def _round_window_corners(title: str = "TickerView", timeout: float = 12.0) -> None:
    """不透明深色窗口 + Win11 DWM 圆角(平滑、抗锯齿、无白底)。

    桌面级逐像素透明在这套 WebView2/WinForms 上合成不出(表单浅灰透不出桌面 = 白方底),
    故改走不透明深色窗口:窗口底色与卡片同色(#101415),卡片铺满窗口,窗口由 DWM 裁圆角。
    DWM 圆角上限约 8px 但平滑无毛躁;Win10 无此属性,静默跳过(退化为深色直角,仍无白底)。
    """
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi
    except Exception:  # noqa: BLE001
        return
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    deadline = time.time() + timeout
    hwnd = 0
    while time.time() < deadline and not hwnd:
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            time.sleep(0.15)
    if not hwnd:
        return
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    try:
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))  # 33=CORNER_PREFERENCE
    except Exception:  # noqa: BLE001
        pass


def _acquire_single_instance(name: str = "TickerView_SingleInstance_Mutex"):
    """命名互斥量单实例锁。返回进程级持有的 HANDLE(勿关闭,进程退出自动释放);
    已有实例在跑则返回 None。非 Windows / 创建失败时返回一个哨兵放行(不因锁问题挡启动)。
    """
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:  # noqa: BLE001
        return object()  # 非 Windows:放行
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.GetLastError.restype = wintypes.DWORD
    ERROR_ALREADY_EXISTS = 183
    h = kernel32.CreateMutexW(None, False, name)
    if not h:
        return object()  # 创建失败:放行
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(wintypes.HANDLE(h))
        return None      # 已有实例
    return h


class PanelController:
    """窗口 + 托盘之间的协调者(被 js_api 与托盘菜单回调共用)。"""

    def __init__(self, port: int, start_hidden: bool) -> None:
        self._port = port
        self._window = None
        self._icon = None
        self._start_hidden = start_hidden

    def attach(self, window, icon) -> None:
        self._window = window
        self._icon = icon

    def show(self) -> None:
        if self._window is None:
            return
        try:
            self._window.show()
        except Exception as exc:  # noqa: BLE001
            logger.warning("显示面板失败: %s", exc)
        self._wake()

    def hide(self) -> None:
        if self._window is not None:
            try:
                self._window.hide()
            except Exception as exc:  # noqa: BLE001
                logger.warning("隐藏面板失败: %s", exc)

    def set_size(self, width, height) -> None:
        """把窗口贴合到"卡片 + 四周透明投影边"的尺寸(前端 ResizeObserver 调用)。"""
        if self._window is None:
            return
        try:
            self._window.resize(int(width), int(height))
        except Exception as exc:  # noqa: BLE001
            logger.warning("调整面板尺寸失败: %s", exc)

    def open_web(self) -> None:
        threading.Thread(target=lambda: webbrowser.open(f"http://127.0.0.1:{self._port}/"),
                         daemon=True).start()

    def quit(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:  # noqa: BLE001
                pass

    def _wake(self) -> None:
        """面板刚唤醒时补刷一次数据,抵消 WebView 隐藏期对定时器的节流。"""
        if self._window is None:
            return
        try:
            self._window.evaluate_js("window.__fpWake && window.__fpWake()")
        except Exception:  # noqa: BLE001
            pass


class PanelAPI:
    """js_api:pywebview 前端可调用的 Python 方法(最小桥)。"""

    def __init__(self, ctl: PanelController) -> None:
        self._ctl = ctl

    def hide_to_tray(self) -> None:
        self._ctl.hide()

    def quit_app(self) -> None:
        self._ctl.quit()

    def open_web(self) -> None:
        self._ctl.open_web()

    def set_size(self, width, height) -> None:
        self._ctl.set_size(width, height)


def _menu(ctl: PanelController):
    import pystray
    from pystray import Menu, MenuItem

    return Menu(
        MenuItem("显示面板", lambda i, g: ctl.show(), default=True),
        MenuItem("隐藏面板", lambda i, g: ctl.hide()),
        MenuItem("打开行情网页", lambda i, g: ctl.open_web()),
        MenuItem("退出", lambda i, g: ctl.quit()),
    )


def run_panel(cfg=None) -> None:
    """启动托盘宿主。阻塞直到窗口关闭(Alt+F4 / 托盘退出)。"""
    import webview

    mutex = _acquire_single_instance()
    if mutex is None:
        print("已有 TickerView 实例在运行(见系统托盘),本次启动退出。")
        return

    port = int(os.environ.get("ALPHAPRISM_WEB_PORT", DEFAULT_PORT))
    _ensure_web(port)

    from ..webprefs import load_prefs

    start_hidden = bool(load_prefs().get("panel_start_hidden", False))

    ctl = PanelController(port, start_hidden)
    url = f"http://127.0.0.1:{port}/?float=1"
    try:
        window = webview.create_window(
            "TickerView", url=url, js_api=PanelAPI(ctl),
            width=380, height=460, x=20, y=80,
            frameless=True, easy_drag=True, on_top=True, resizable=False,
            background_color="#101415", hidden=start_hidden,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"悬浮面板启动失败: {exc}")
        print("需要 Edge WebView2 运行时(Windows 10/11 自带)或 pywebview 支持的后端。")
        raise

    import pystray

    icon = pystray.Icon("TickerView", _load_tray_icon(), "TickerView", _menu(ctl))
    ctl.attach(window, icon)
    try:
        icon.run_detached()  # pystray 用自己的线程,主线程留给 webview
    except Exception:  # noqa: BLE001
        threading.Thread(target=icon.run, daemon=True).start()

    # 窗口出现后给不透明深色窗口加 DWM 圆角(后台轮询 HWND,不阻塞 GUI 线程)
    threading.Thread(target=_round_window_corners, daemon=True).start()

    try:
        webview.start(debug=False)
    finally:
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    run_panel()
