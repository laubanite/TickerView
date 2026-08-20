@echo off
REM AlphaPrism CLI wrapper (run from project root in VS Code terminal)
REM e.g.  .\alphaprism.bat --help
chcp 65001 >nul
set PYTHONUTF8=1
D:\Anaconda\python.exe E:\AgentProjects\AlphaPrism\scripts\cli.py %*