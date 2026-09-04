# -*- coding: utf-8 -*-
"""聚宽影子回测对比(Layer3):引擎 trades.csv vs 聚宽导出 trades.csv 逐笔 diff。

对标参数(两边必须一致):
  引擎:  engine_cfg={'matching':'next_open','slippage_rate':0.0}
  聚宽:  reference/joinquant_breakout_stop.py(滑点 0、止损=avg_cost、成交价=次日开盘)

判定(2026-09 对齐口径):
  - 交易笔数一致、日期 100% 一致、方向一致;
  - 成交价差 <= 0.01(数据源开盘价噪声 0.5% 级:腾讯前复权 vs 聚宽);股数差 <= 100
    (整手取整);手续费差 <= 0.10(随整手差的结构性费用偏移);
  - 盈亏误差:配对后 |Σ(引擎盈亏) − Σ(聚宽盈亏)| / |Σ引擎盈亏| < 1%(验收核心)。
"""
import argparse
import csv


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def pnl_sum(rows: list[dict]) -> float:
    """配对盈亏:按 BUY→SELL 成对,Σ(卖额−卖费−买额−买费)。"""
    total = 0.0
    for i in range(0, len(rows) - 1, 2):
        if rows[i]["side"].upper() == "BUY" and rows[i + 1]["side"].upper() == "SELL":
            b, s = rows[i], rows[i + 1]
            total += (float(s["amount"]) - float(s["fee"])
                      - float(b["amount"]) - float(b["fee"]))
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("engine_csv")
    ap.add_argument("joinquant_csv")
    args = ap.parse_args()
    eng = load(args.engine_csv)
    jq = load(args.joinquant_csv)
    print(f"引擎 {len(eng)} 笔 vs 聚宽 {len(jq)} 笔")
    if len(eng) != len(jq):
        print(f"❌ 笔数不一致: {len(eng)} vs {len(jq)}")
        return 1
    mism = []
    for e, j in zip(eng, jq):
        ok_date = e["date"].strip() == j["date"].strip()
        ok_side = e["side"].strip().upper() == j["side"].strip().upper()
        p_diff = abs(float(e["price"]) - float(j["price"]))
        s_diff = abs(int(float(e["shares"])) - int(float(j["shares"])))
        f_diff = abs(float(e["fee"]) - float(j["fee"]))
        if not (ok_date and ok_side and p_diff <= 0.01 and s_diff <= 100
                and f_diff <= 0.10):
            mism.append((e["date"], j["date"], e["side"], p_diff, s_diff, f_diff))
    if mism:
        print(f"❌ 不一致 {len(mism)}/{len(eng)} 笔(日期,价格差,股数差,费差):")
        for m in mism[:20]:
            print("  ", m)
        return 1
    print("✅ 日期/方向 100% 一致;价格/股数/费用全部在容差内(±100 股整手)")
    pnle, pnlj = pnl_sum(eng), pnl_sum(jq)
    denom = abs(pnle)
    if denom <= 0:
        # 全为未平仓(只有 BUY):盈亏判定无配对基准,横断面对照跳过,其余判定已通过
        print("  未平仓交易(无卖出配对):盈亏误差判定跳过;其余逐笔校验全部通过")
        return 0
    err = abs(pnle - pnlj) / denom * 100
    print(f"  引擎盈亏 {pnle:.2f} / 聚宽盈亏 {pnlj:.2f} / 误差 {err:.2f}%"
          f"({'✅ <1%' if err < 1 else '❌ ≥1%'})")
    return 0 if err < 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())