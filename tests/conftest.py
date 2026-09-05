# -*- coding: utf-8 -*-
"""pytest 全局 conftest。

DSH 沙箱兼容补丁(2026-09-05):pytest 的 session-finish 钩子
cleanup_dead_symlinks(basetemp) 在沙箱下对 basetemp 的 iterdir 报
PermissionError(WinError 5),导致整个 session 崩在收尾阶段、
测试汇总输出丢失(用例本身全部正常跑完)。该钩子只做"清理失效
symlink"的目录卫生,与测试语义无关——置空之。
"""
import _pytest.pathlib as _pp

_pp.cleanup_dead_symlinks = lambda _root: None
