# -*- mode: python ; coding: utf-8 -*-
"""TickerView 二期打包 spec(PyInstaller onedir → dist/TickerView/TickerView.exe)。

构建:  pyinstaller TickerView.spec
产物:  dist/TickerView/  (整个目录交给 Inno Setup 打包)

风险点(需构建 spike 验证):
  · pythonnet(clr / Python.Runtime)+ pywebview EdgeChromium 的 WebView2 程序集;
  · akshare 的动态 import 与随包数据文件。
两者都靠 collect_all 尽量收全;若运行期报缺模块,回来往 hiddenimports/datas 补。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
ROOT = Path(SPECPATH)  # spec 所在目录 = repo 根

datas = []
binaries = []
hiddenimports = []

# ---- 只读资源随包(冻结后由 alphaprism/paths.py 从 _MEIPASS 读取) ----
datas += [(str(ROOT / "alphaprism" / "web" / "static"), "alphaprism/web/static")]
# 只打包运行时真正需要的托盘图(不整目录打包,避免把旧/未用资产塞进安装包)
datas += [(str(ROOT / "alphaprism" / "planner" / "assets" / "tray_icon.png"), "alphaprism/planner/assets")]
datas += [(str(ROOT / "config" / "settings.yaml"), "config")]
# 出厂自选种子:默认关闭 → 用户首启为空白清单,自行添加(隐私/通用考虑)。
# 若想随包带一份示例自选,取消下面注释并把 config/watchlist.yaml 换成公开示例代码。
# _watch = ROOT / "config" / "watchlist.yaml"
# if _watch.exists():
#     datas += [(str(_watch), "config")]

# ---- 重依赖全量收集 ----
for pkg in ("akshare", "pywebview", "pandas", "PIL", "yaml", "pystray", "pythonnet", "clr", "Python.Runtime"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:  # noqa: BLE001  某包不可 collect 时跳过,不阻断构建
        pass

# ---- pywebview Windows 后端 + WebView2 显式补 ----
try:
    hiddenimports += collect_submodules("webview.platforms")
except Exception:  # noqa: BLE001
    pass
hiddenimports += [
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr",
    "Python.Runtime",
]

a = Analysis(
    [str(ROOT / "entry_panel.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 面板用不到的重型可选依赖:akshare 的 collect_all 会顺带引用,这里强制剔除瘦身。
        # 若重跑构建后 exe 运行报缺其中某个(极小概率),把它从本列表删掉再打包即可。
        "torch", "torchvision", "torchaudio", "playwright", "botocore", "boto3",
        "panel", "holoviews", "bokeh", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "llvmlite", "numba", "scipy", "sklearn", "statsmodels", "matplotlib",
        "seaborn", "plotly", "pyarrow", "h5py", "sympy", "dask", "xarray",
        "skimage", "astropy", "babel", "sphinx",
        "jupyterlab", "notebook", "IPython", "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TickerView",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # 无黑框(托盘后台常驻)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "alphaprism" / "planner" / "assets" / "tray_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TickerView",
)
