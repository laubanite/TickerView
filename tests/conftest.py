# -*- coding: utf-8 -*-
"""pytest 全局 conftest。

DSH 沙箱兼容补丁(2026-09-05):pytest 的 session-finish 钩子
cleanup_dead_symlinks(basetemp) 在沙箱下对 basetemp 的 iterdir 报
PermissionError(WinError 5),导致整个 session 崩在收尾阶段、
测试汇总输出丢失(用例本身全部正常跑完)。该钩子只做"清理失效
symlink"的目录卫生,与测试语义无关——置空之。
"""
import _pytest.pathlib as _pp
import _pytest.tmpdir as _td
import pathlib as _pl

_pp.cleanup_dead_symlinks = lambda _root: None
# tmpdir.py 是 from-import 绑定,必须同时补丁其命名空间
_td.cleanup_dead_symlinks = lambda _root: None

# DSH Windows 沙箱把 mkdir(mode=0o700) 映射成限制性 DACL,随后连属主都
# 无法 iterdir 该目录(pytest 自建 basetemp 正是 0o700)。测试进程内统一
# 按默认 0o777 创建,绕开该映射;与测试语义无关。
_orig_mkdir = _pl.Path.mkdir


def _mkdir_777(self, mode=0o777, parents=False, exist_ok=False):
    return _orig_mkdir(self, mode=0o777, parents=parents, exist_ok=exist_ok)


_pl.Path.mkdir = _mkdir_777
