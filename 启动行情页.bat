@echo off
REM AlphaPrism Web 行情页 - 一键启动(双击即可)
REM 自动启动服务并打开浏览器。关闭本窗口即停止服务。
chcp 65001 >nul
set PYTHONUTF8=1
D:\Anaconda\python.exe E:\AgentProjects\AlphaPrism\scripts\cli.py web
pause