# -*- coding: utf-8 -*-
"""探针:腾讯 qt 实时接口字段核实(方案 §8-Q1,只读探测)。"""
import requests

r = requests.get("https://qt.gtimg.cn/q=sz300308,sh600519,sz000001,sh510300,bj837172",
                 timeout=10)
r.encoding = "gbk"
print("HTTP", r.status_code, "bytes", len(r.content))
for line in r.text.strip().splitlines():
    f = line.split("~")
    if len(f) < 10:
        print("SHORT:", line[:80])
        continue
    print("=" * 50)
    print(f"code={f[2]} name={f[1]} fields={len(f)}")
    for i, v in enumerate(f):
        if v:
            print(f"  [{i:>2}] {v}")
    break  # 先只看第一只的字段布局,其余打印关键位
for line in r.text.strip().splitlines()[1:]:
    f = line.split("~")
    if len(f) < 10:
        continue
    print(f"--- {f[2]} {f[1]}: price={f[3]} prev={f[4]} vol={f[6]} "
          f"[36]={f[36] if len(f) > 36 else ''} [38]={f[38] if len(f) > 38 else ''} "
          f"[47]={f[47] if len(f) > 47 else ''} [48]={f[48] if len(f) > 48 else ''}")
