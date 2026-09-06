"""把 logo3.png 直接套进白色圆角矩形,作为唯一的原创应用图标。

关键:不裁切、不重排箭头——logo3.png 的箭头本就位于画面正中心(用户确认),
这里只做两件事:
  1) 把 logo3 的白色背景抠成透明(亮度反推 alpha),箭头/节点/粗细原样保留;
  2) 在其后垫一层白色圆角矩形(透明圆角),得到"白砖黑标、对角居中"的最终图标。
产物(assets/): tray_icon.ico(多尺寸) / tray_icon.png(托盘) / icon_master.png / icon_final_preview.png
运行: python scripts/gen_logo_final.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "alphaprism" / "planner" / "assets"
SRC = ASSETS / "logo3.png"


def _build() -> Image.Image:
    im = Image.open(SRC).convert("RGBA")          # 2048×2048,白底黑标
    W, H = im.size
    lum = im.convert("L")
    # alpha = 255 - 亮度:黑=不透明,白=透明;再掐掉近白微残留 → 箭头保留原位、白底透明
    alpha = lum.point(lambda v: max(0, 255 - v))
    alpha = alpha.point(lambda v: 0 if v < 14 else v)
    glyph = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glyph.putalpha(alpha)

    # 白色圆角矩形(铺满画布,圆角处透明)
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    rad = int(min(W, H) * 0.225)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=rad, fill=(255, 255, 255, 255))
    # 细浅描边(边界定义,白桌面也能辨认)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=rad,
                        outline=(0, 0, 0, 24), width=max(1, W // 512))

    # 把箭头包围盒平移到画布正中心(严格水平/垂直居中),再按需向下微调留白,缓解"视觉偏上"
    bb = alpha.getbbox()
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    DOWN = 0.10 * H  # 向下留白占比(视觉居中补偿,可按需调)
    shift = (int(round(W / 2 - cx)), int(round(H / 2 - cy + DOWN)))
    centered = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    centered.paste(glyph, shift, glyph)

    # 居中后的箭头叠加在白色圆角矩形上
    tile.alpha_composite(centered, (0, 0))
    return tile


def _size(img, s: int) -> Image.Image:
    return img.resize((s, s), Image.LANCZOS)


def _save(path: Path, img: Image.Image, **kw):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    img.save(path, **kw)


def _bold_glyph(kernel: int = 13) -> Image.Image:
    """透明底 + 加粗黑箭头(形态学膨胀加粗线条/节点,形状不变),包围盒居中。
    供托盘/隐藏列表用(浅底 overflow 上比白砖更清晰)。
    """
    im = Image.open(SRC).convert("RGBA")
    W, H = im.size
    lum = im.convert("L")
    alpha = lum.point(lambda v: max(0, 255 - v))
    alpha = alpha.point(lambda v: 0 if v < 14 else v)
    alpha = alpha.filter(ImageFilter.MaxFilter(kernel))  # 膨胀加粗
    bb = alpha.getbbox()
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    shift = (int(round(W / 2 - cx)), int(round(H / 2 - cy)))
    a2 = Image.new("L", (W, H), 0)
    a2.paste(alpha, shift)
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g.putalpha(a2)
    return g


def _size(img, s: int) -> Image.Image:
    return img.resize((s, s), Image.LANCZOS)


def main():
    icon = _build()          # 白砖黑标(exe/快捷方式/安装器)
    bold = _bold_glyph()     # 透明加粗黑标(托盘/隐藏列表)
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes_ico = [16, 24, 32, 48, 64, 128, 256]
    _save(ASSETS / "tray_icon.ico", _size(icon, 256), format="ICO", sizes=[(s, s) for s in sizes_ico])
    _save(ASSETS / "tray_icon.png", _size(bold, 64))           # 托盘:透明加粗黑标
    _save(ASSETS / "icon_master.png", _size(icon, 1024))       # 母图(白砖)
    # 预览:加粗透明标 × 浅底(模拟 overflow)/ 深底,含 16/24/32/48 塔
    from PIL import ImageDraw as ID
    sizes = [48, 32, 24, 16]
    cell = 96
    row_w = 2 * (sum(sizes) + len(sizes) * 24 + 60)
    sheet = Image.new("RGBA", (row_w, 2 * cell + 40), (255, 255, 255, 255))
    d = ID.Draw(sheet)
    for ri, (bg, name) in enumerate([((243, 244, 246, 255), "浅底(隐藏列表/overflow)"),
                                     ((28, 32, 34, 255), "深底(深色任务栏)")]):
        y = ri * (cell + 20) + 8
        d.rectangle([0, y, row_w, y + cell], fill=bg)
        x = 20
        for s in sizes:
            g = _size(bold, s)
            # 深底上黑标看不见 → 预览里深底用白标反白示意(实际交付仍是黑标)
            if bg[0] < 100:
                wv = Image.new("RGBA", g.size, (255, 255, 255, 0))
                wv.putalpha(g.getchannel("A"))
                g = wv
            sheet.alpha_composite(g, (x, y + (cell - s) // 2))
            x += s + 24
        d.text((x + 6, y + cell // 2 - 6), name + "  48·32·24·16", fill=(120, 126, 130, 255))
    _save(ASSETS / "icon_final_preview.png", sheet)
    print("preview:", ASSETS / "icon_final_preview.png")
    print("preview:", ASSETS / "icon_final_preview.png")


if __name__ == "__main__":
    main()
