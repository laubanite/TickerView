# -*- coding: utf-8 -*-
"""聚宽影子对标策略(Layer3 校准基准)——铁壳版:只做信号与下单,零自定义记录。

对口径:
  入场:收盘价 > 前 20 根最高价(不含当日)→ order_target_value 全仓
  止损:收盘价 <= 持仓成本(avg_cost,含佣金)× 0.92 → order_target 清仓
  成交:聚宽日频默认 = 信号 bar 收盘判定、下一 bar 开盘(9:30)成交
行为约定:
  - 佣金万5双边、滑点 0(与引擎 engine_cfg={'matching':'next_open','slippage_rate':0.0} 对齐)
  - 运行区间 2022-01-01 ~ 2024-12-31
交易数据不要用本文件收集:跑完后在聚宽结果页点「成交」标签导出 xlsx,
再运行 scripts/jq_export_to_csv.py 转成引擎格式 CSV 交给对比脚本。

若页面顶部红字报错,把错误信息整体复制给我,我按错误逐条修。
"""
# -----------------------------------------------------------------------------
from jqdata import *  # noqa: F401,F403

g = {"security": None}
SECURITY = "515790.XSHG"     # 改这里:159516.XSHE / 518850.XSHG


def initialize(context):
    set_benchmark("000300.XSHG")
    # 前复权口径:与引擎数据(fetch_daily 前复权)严格一致。
    # 关键:use_real_price=False → data 与 attribute_history 均为前复权价;
    # 否则(True)历史按真实价,遇份额折算/分红(如 159516 1:4、518850 折算)
    # 信号日与成交价全盘错位(2026-09 Layer3 实测:价比恒 0.25 / 触发日差月)。
    set_option("use_real_price", False)
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=0.0005,
                             close_commission=0.0005, min_commission=0),
                   type="fund")
    set_slippage(FixedSlippage(0))
    g["security"] = SECURITY
    set_universe([g["security"]])


def handle_data(context, data):
    sec = g["security"]
    # 聚宽 attribute_history 含当前 bar:取 21 根去尾 = 前 20 根(不含当日)
    h = attribute_history(sec, 21, unit='1d', fields='high', skip_paused=False)
    prev_peak = float(h["high"].iloc[:-1].max()) if len(h) > 1 else None

    close = float(data[sec].close)
    pos = context.portfolio.positions[sec]
    holding = pos.total_amount if pos else 0

    if holding == 0 and prev_peak is not None and close > prev_peak:
        order_target_value(sec, context.portfolio.available_cash)
    elif holding > 0 and pos.avg_cost > 0 and close <= pos.avg_cost * 0.92:
        order_target(sec, 0)