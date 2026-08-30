"""悬浮面板(产品方案 §5.1,里程碑7):pywebview 无边框置顶小窗。

只给结论,细节走快照:每行 现价 | 涨跌 | 放量/缩量 | 结论词(5档)+ 大盘门控灯。
数据来源:check_live(核对引擎:实时快照 + 时间调整量比 + 大盘门控 J 值)。
面板 JS 按 config/web.yaml 的 refresh_interval_sec 秒(默认 12)调 js_api.refresh() 拉最新核对;
底部两按钮:
- [盘中快照] → js_api.snapshot()(规则事实 + LLM 解读,差异输出)
- [展开行情页] → 打开 http://127.0.0.1:8765(需 Web 服务在跑)

用法: alphaprism panel
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..config import Config
from .checker import CONCLUSION_STYLE

logger = logging.getLogger(__name__)

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>AlphaPrism</title>
<style>
  :root{--bg:#10131a;--card:#171c26;--line:#232a38;--dim:#787b86;--up:#f23645;--down:#089981;--flat:#d1d4dc}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--flat);font:12px/1.5 system-ui,"Microsoft YaHei",sans-serif;
       user-select:none;overflow:hidden;border:1px solid var(--line)}
  .hd{display:flex;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid var(--line)}
  .logo{font-weight:700;letter-spacing:.5px;font-size:13px}
  .logo span{color:#4c8dff}
  .gate{margin-left:auto;display:flex;align-items:center;gap:6px}
  .dot{width:8px;height:8px;border-radius:50%;background:#089981}
  .dot.off{background:#f23645}
  .j{color:var(--dim);font-size:11px}
  .list{max-height:280px;overflow-y:auto}
  .row{display:flex;align-items:center;gap:8px;padding:7px 10px;border-bottom:1px solid #1b2130}
  .row.dimmed{opacity:.45}
  .nm{width:56px;font-weight:600;white-space:nowrap;overflow:hidden}
  .px{width:58px;text-align:right}
  .chg{width:58px;text-align:right;font-size:11px}
  .up{color:var(--up)}.down{color:var(--down)}
  .vol{width:40px;text-align:center;font-size:11px;color:var(--dim)}
  .tag{flex:1;text-align:center;padding:2px 4px;border-radius:4px;font-size:11px;white-space:nowrap}
  .t-normal{background:#262c3a;color:#9aa0ae}
  .t-near{background:#5c4a1a;color:#ffd166}
  .t-warn{background:#5c2a1a;color:#ff9f6e}
  .t-hot{background:#5c1a2a;color:#ff7b8a}
  .t-hit{background:#7a1626;color:#ff5c6c}
  .t-ok{background:#1a4a2a;color:#6ee7a0}
  .ft{display:flex;gap:8px;padding:8px 10px;border-top:1px solid var(--line)}
  .btn{flex:1;background:#1d2434;color:#d1d4dc;border:1px solid var(--line);border-radius:6px;
       padding:7px 0;font-size:12px;cursor:pointer}
  .btn:hover{background:#26334a}
  .btn.primary{background:#234b8c;border-color:#2f5fb0;color:#fff}
  .snap{max-height:150px;overflow-y:auto;padding:8px 10px;border-top:1px solid var(--line);font-size:11px;color:#c6cbd6;white-space:pre-wrap;display:none}
</style>
</head>
<body>
  <div class="hd">
    <div class="logo">Alpha<span>Prism</span></div>
    <div class="gate">
      <span class="dot" id="dot"></span>
      <span id="gtext">门控…</span>
      <span class="j" id="gj">J=—</span>
    </div>
  </div>
  <div class="list" id="list"></div>
  <div class="snap" id="snap"></div>
  <div class="ft">
    <button class="btn" onclick="doSnapshot()">盘中快照</button>
    <button class="btn primary" onclick="openWeb()">展开行情页</button>
  </div>
<script>
  const STYLE = {"平静":"t-normal","接近买点":"t-near","接近卖点":"t-warn","等待·缺条件":"t-hot","买点触发":"t-hit","破位":"t-hit"};
  async function refresh() {
    try {
      const r = await pywebview.api.refresh();
      render(r);
    } catch(e) {}
  }
  function render(r) {
    const g = r.gate || {};
    const dot = document.getElementById('dot');
    const gt = document.getElementById('gtext');
    const gj = document.getElementById('gj');
    if (g.open) { dot.className='dot'; gt.textContent='门控开放'; }
    else { dot.className='dot off'; gt.textContent='门控关闭·不加仓'; }
    gj.textContent = 'J=' + (g.j ?? '-');
    const list = document.getElementById('list');
    const rows = r.verdicts || [];
    list.innerHTML = rows.map(v => {
      const dim = v.conclusion === '平静' ? ' dimmed' : '';
      const p = v.price != null ? Number(v.price).toFixed(3) : '-';
      const c = v.change_pct != null ? (v.change_pct > 0 ? '+' : '') + Number(v.change_pct).toFixed(2) + '%' : '-';
      const cls = v.change_pct > 0 ? 'up' : v.change_pct < 0 ? 'down' : '';
      const st = STYLE[v.conclusion] || 't-normal';
      const nm = String(v.name||'').replace(/ETF.*$/,'').replace(/.*ETF/,'') || v.code;
      const near = v.near ? ' <span style="color:#787b86;font-size:10px">' + v.near + '</span>' : '';
      return '<div class="row' + dim + '">' +
        '<div class="nm">' + nm + '</div>' +
        '<div class="px">' + p + '</div>' +
        '<div class="chg ' + cls + '">' + c + '</div>' +
        '<div class="vol">' + (v.vol_label||'') + '</div>' +
        '<div class="tag ' + st + '">' + v.conclusion + near + '</div></div>';
    }).join('');
  }
  async function doSnapshot() {
    const snap = document.getElementById('snap');
    snap.style.display = 'block';
    snap.textContent = '生成中…';
    try {
      snap.textContent = await pywebview.api.snapshot();
    } catch(e) { snap.textContent = '快照生成失败: ' + e; }
  }
  function openWeb() { pywebview.api.open_web(); }
  window.addEventListener('pywebviewready', () => {
    refresh();
    setInterval(refresh, __REFRESH_MS__);
  });
</script>
</body>
</html>"""


class PanelAPI:
    """js_api:pywebview 前端可调用的 Python 方法。"""

    def __init__(self, model, cfg: Config) -> None:
        self._model = model
        self._cfg = cfg

    def refresh(self) -> dict:
        from .live import check_live

        try:
            return check_live(self._model, self._cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("悬浮面板刷新失败: %s", exc)
            return {"gate": {"open": True, "j": None, "rule": "", "action": ""},
                    "verdicts": [], "error": str(exc)}

    def snapshot(self) -> str:
        from .live import check_live
        from .snapshot import build_snapshot

        try:
            r = check_live(self._model, self._cfg)
            return build_snapshot(self._model, r, self._cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("盘中快照生成失败: %s", exc)
            return f"快照生成失败: {exc}"

    def open_web(self) -> None:
        import webbrowser

        port = int(__import__("os").environ.get("ALPHAPRISM_WEB_PORT", "8765"))
        threading.Thread(target=lambda: webbrowser.open(f"http://127.0.0.1:{port}"),
                         daemon=True).start()


def run_panel(cfg: Config | None = None) -> None:
    """启动悬浮面板(pywebview 置顶窗)。阻塞直到窗口关闭。"""
    import webview

    cfg = cfg or Config()
    from .parser import parse_file as parse_battlemap

    from ..config import PROJECT_ROOT  # noqa: F401  (用于默认路径探测)
    path = cfg.get("battlemap", "path", default="")
    if not path:
        import glob

        cands = sorted(glob.glob(r"E:\AITrader\七只ETF作战地图_*.md"), reverse=True)
        if not cands:
            print("未找到作战地图。用法: alphaprism panel [path]")
            return
        path = cands[0]
    model = parse_battlemap(path)
    api = PanelAPI(model, cfg)
    try:
        from ..webprefs import refresh_interval_sec

        interval_ms = max(1000, int(refresh_interval_sec() * 1000))
        html = PANEL_HTML.replace("__REFRESH_MS__", str(interval_ms))
        window = webview.create_window(
            "AlphaPrism", html=html, js_api=api,
            width=360, height=430, x=20, y=80,
            frameless=True, easy_drag=True, on_top=True, resizable=False,
        )
        webview.start(debug=False)
    except Exception as exc:  # noqa: BLE001
        print(f"悬浮面板启动失败: {exc}")
        print("需要 Edge WebView2 运行时(Windows 10/11 自带)或 pywebview 支持的后端。")
        raise


if __name__ == "__main__":
    run_panel()