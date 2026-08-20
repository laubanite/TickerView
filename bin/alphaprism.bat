@echo off
REM AlphaPrism CLI wrapper (cmd / PowerShell)
REM bin\ 已加入 PATH 后,可直接敲: alphaprism --help
set PYTHONUTF8=1
D:\Anaconda\python.exe E:\AgentProjects\AlphaPrism\scripts\cli.py %*
