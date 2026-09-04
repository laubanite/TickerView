# -*- coding: utf-8 -*-
"""AlphaPrism 后端回测引擎(backtest_engine,2026-09)。

对标聚宽的引擎级回测基础设施。设计规范(不可妥协):
1. 指标计算:强制 TA-Lib(indicator_calc 唯一出口),禁止手搓 rolling window;
2. 策略注入:引擎不内置策略,只读取 YAML 配置(strategy_runner);
3. 无未来函数:Day N 信号只能访问 [0:N] 数据(GoLookAheadGuard 化);
4. 撮合:默认收盘撮合(与聚宽日频一致),佣金/滑点走 engine.yaml;
5. 审计:每笔交易附带输入快照哈希(auditor),可回溯任何信号。

验收三层:Layer1 指标数学一致性(TA-Lib diff)、Layer2 无未来函数、
Layer3 聚宽影子回测逐笔对标(<1% 误差)。
"""
__version__ = "0.1.0"