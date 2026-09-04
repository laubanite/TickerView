# -*- coding: utf-8 -*-
"""聚宽「成交」导出 xlsx → 引擎对比格式 CSV。

用法:
  python scripts/jq_export_to_csv.py <聚宽导出.xlsx> <输出.csv>

映射:日期(2022/3/1 → 2022-03-01), 买卖(买/卖 → BUY/SELL),
     股数(62200股 → 62200), 价格, 成交额(取正), 手续费。
"""
import argparse
import csv
import re
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("out_csv", nargs="?", default="")
    args = ap.parse_args()

    try:
        import pandas as pd
    except ImportError:
        print("需要 pandas+openpyxl: pip install pandas openpyxl")
        return 1
    df = pd.read_excel(args.xlsx, header=None)   # 聚宽成交导出默认无表头
    first = [str(c).strip() for c in df.iloc[0]]
    if any("日期" in s or "Date" in s.lower() for s in first):
        df = df.iloc[1:].reset_index(drop=True)
        colmap = {s.strip(): i for i, s in enumerate(first)}
        get = lambda r, name, fallback: r[colmap.get(name, fallback)]
    else:
        # 无表头:按聚宽固定列位(0日期 1时间 2标的 3买卖 4类型 5股数 6价格 7成交额 8收益 9手续费)
        get = lambda r, name, fallback: r[fallback]
    for need in ("日期", "买卖", "股数", "价格", "成交额", "手续费"):
        if need not in (colmap if any("日期" in s for s in first) else {}):
            continue   # 无表头模式无需列名校验
    i_date, i_side, i_shares = 0, 3, 5
    i_price, i_amount, i_fee = 6, 7, 9
    out = args.out_csv or str(Path(args.xlsx).with_suffix(".trades.csv"))
    def fmt_date(d):
        s = str(d).split(" ")[0].split("T")[0].replace("/", "-")
        y, m, dd = s.split("-")
        return f"{y}-{int(m):02d}-{int(dd):02d}"
    def parse_shares(v):
        return int(re.sub(r"\D", "", str(v)))
    rows = []
    for _, r in df.iterrows():
        if str(r[i_date]).strip() in ("日期", "") or str(r[i_side]).strip() in ("买卖", ""):
            continue
        rows.append({
            "date": fmt_date(r[i_date]),
            "side": "BUY" if str(r[i_side]).strip() in ("买", "买入", "B", "BUY") else "SELL",
            "price": float(r[i_price]),
            "shares": parse_shares(r[i_shares]),
            "amount": round(abs(float(str(r[i_amount]).replace(",", ""))), 2),
            "fee": round(float(r[i_fee]), 2),
            "reason": "",
        })
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "side", "price", "shares",
                                          "amount", "fee", "reason"])
        w.writeheader()
        w.writerows(rows)
    print(f"✅ 转换完成: {len(rows)} 笔 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())