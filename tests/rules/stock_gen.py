# -*- coding: utf-8 -*-
"""个股合成规则矩阵生成器(v3 自适应层)——镜像 tests/rules/gen.py 架构。

每个形态 = 一类市场体质 × 一个规则分支。v3 的核心检验目标:
**同一参数对不同体质的股票行为一致**(固定阈值 R6/R7 的教训)。
命名规则:S<体质>-<分支>。
"""
from __future__ import annotations

# --------------------------------------------------------------------------- builder

_LOW_P = dict(m5=10.2, m20=10.5, m60=11.0, low20_close=10.8, low250_close=7.0,
              peak250=15.0, p250_pos=0.30, weak_count=0, consec_down=0,
              atr20_pct=3.0, atr_pctile=50, vol_ratio=0.8, pct_chg=0.2)


def mk(price=11.0, **kw) -> dict:
    """健康多头基线 + 覆盖项。价格在 M20 上、无破位、常态波动。"""
    base = dict(price=price, pct_chg=0.2, m5=11.2, m20=10.8, m60=10.5,
                low20_close=10.0, low250_close=7.0, peak250=12.0,
                p250_pos=0.65, weak_count=0, consec_down=0,
                atr20_pct=2.5, atr_pctile=50, vol_ratio=1.0,
                cost=None, suspended=False,
                at_limit_up=False, at_limit_down=False)
    base.update(kw)
    return base


MATRIX: list[tuple[str, dict, dict]] = []

# ========== 族 A:健康多头(各种体质,期望全部"正常")==========

MATRIX.append(("A1-健康多头-低价股", mk(price=4.2, m5=4.3, m20=4.1, m60=4.0,
                                       low20_close=3.8, low250_close=3.0,
                                       peak250=5.0), {}))
MATRIX.append(("A2-健康多头-高价股", mk(price=1330.0, m5=1310.0, m20=1280.0, m60=1250.0,
                                        low20_close=1255.0, peak250=1400.0), {}))
MATRIX.append(("A3-健康多头-高波动体质(科技股常态ATR7%)", mk(atr20_pct=7.0, atr_pctile=40), {}))
MATRIX.append(("A4-健康多头-低波动体质(公用事业ATR1%)", mk(atr20_pct=1.0, atr_pctile=30), {}))
# 关键回归:旧 R7 固定阈值 6% 会对 A3 误报;新体制(自身分位 40)必须不触发
MATRIX.append(("A5-高ATR但自身常态(旧R7会误报)", mk(atr20_pct=7.5, atr_pctile=55), {}))

# ========== 族 B:趋势结构走弱(R12)==========

MATRIX.append(("B1-空头排列-底部区", mk(price=8.9, m5=8.0, m20=9.0, m60=9.5,
                                       low20_close=8.5, p250_pos=0.10), {}))
MATRIX.append(("B2-空头排列-但位置高(不触发)", mk(price=8.9, m5=8.0, m20=9.0, m60=9.5,
                                                 low20_close=8.5, low250_close=8.4,
                                                 p250_pos=0.70), {}))
MATRIX.append(("B3-纠缠排列(不触发)", mk(price=10.2, m5=10.4, m20=10.5, m60=10.1,
                                        low20_close=9.9, p250_pos=0.40), {}))

# ========== 族 C:持续走弱(R10 计数阶梯)==========

MATRIX.append(("C1-走弱1次(不触发)", mk(weak_count=1), {}))
MATRIX.append(("C2-走弱2次(关注)", mk(weak_count=2), {}))
MATRIX.append(("C3-走弱3次(风险)", mk(weak_count=3), {}))
MATRIX.append(("C4-走弱4次封顶(风险)", mk(weak_count=4), {}))

# ========== 族 D:波动体制(R11)==========

MATRIX.append(("D1-ATR自身90分位(高波动体制)", mk(atr20_pct=4.0, atr_pctile=95), {}))
# 同样 4% 的 ATR,但处在自身低分位 → 不触发(自归一化核心断言)
MATRIX.append(("D2-同ATR但自身低分位(不触发)", mk(atr20_pct=4.0, atr_pctile=30), {}))
MATRIX.append(("D3-历史不足无分位(固定阈值兜底)", mk(atr20_pct=6.5, atr_pctile=None), {}))

# ========== 族 E:切位与成本(已验证内核,口径不变)==========

MATRIX.append(("E1-跌破20日低(R3风险)", mk(price=9.9, m5=10.1, m20=10.8,
                                          low20_close=10.0), {}))
MATRIX.append(("E2-跌破250日低(R4严重)", mk(price=6.9, m5=7.2, m20=7.5, m60=8.0,
                                           low250_close=7.0, low20_close=7.2,
                                           p250_pos=0.0), {}))
MATRIX.append(("E3-成本-8%(R5风险)", mk(price=9.2, cost=10.0), {}))
MATRIX.append(("E4-破位M20放量(R2风险)", mk(price=10.7, m5=10.9, m20=10.8, m60=10.5,
                                           vol_ratio=1.8), {}))

# ========== 族 F:结构状态与停牌守卫 ==========

MATRIX.append(("F1-触及跌停(R9风险)", mk(pct_chg=-10.0, at_limit_down=True,
                                        vol_ratio=0.5), {}))
MATRIX.append(("F2-停牌守卫(陈旧价不作判定)", mk(suspended=True, price=6.9,
                                                low250_close=7.0, low20_close=7.2,
                                                weak_count=4, atr_pctile=99), {}))
MATRIX.append(("F3-无有效行情", mk(price=None), {}))

# ========== 族 G:复合压力(多规则叠加,验档位取最高)==========

MATRIX.append(("G1-走弱3次+高波动体制(风险)", mk(weak_count=3, atr20_pct=5.5,
                                                atr_pctile=96), {}))
MATRIX.append(("G2-破20日低+走弱2(风险,取最高)", mk(price=9.9, low20_close=10.0,
                                                  weak_count=2), {}))
MATRIX.append(("G3-全绿但250日低位纠缠(仅关注)", mk(price=10.4, m5=10.5, m20=10.6,
                                                  m60=10.7, low20_close=10.3,
                                                  p250_pos=0.20, weak_count=2), {}))
