@echo off
REM AlphaPrism intraday monitor entry (Task Scheduler every 15 min)
REM Self-skips non-trading days/sessions; writes UTF-8 log to data\run_monitor.log
D:\Anaconda\python.exe E:\AgentProjects\AlphaPrism\scripts\run_monitor.py
