"""Genera el logo (hoja + boton de play) en PNG, ICNS (macOS) y favicon."""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1]
S = 2048  # lienzo de trabajo (se reduce al final)


def gradient(size, top, bottom):
    g = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g.resize((size, size))


def lens(size, r_scale=1.0):
    """Forma de hoja: interseccion de dos circulos."""
    m1, m2 = Image.new("L", (size, size), 0), Image.new("L", (size, size), 0)
    R = int(size * 0.62 * r_scale)
    d = int(size * 0.34 * r_scale)
    c = size // 2
    ImageDraw.Draw(m1).ellipse([c - R, c - d - R + R // 2 - R // 2 + 0, c + R, c - d + R], fill=255)
    ImageDraw.Draw(m2).ellipse([c - R, c + d - R, c + R, c + d + R], fill=255)
    return ImageChops.multiply(m1, m2)


def build() -> Image.Image:
    pad = int(S * 0.098)
    body = S - 2 * pad
    bg = gradient(body, (10, 66, 44), (34, 197, 94)).convert("RGBA")
    # hoja blanca inclinada (lienzo grande para que las puntas no se recorten)
    big = int(body * 1.3)
    m = lens(big, 0.66).rotate(45, resample=Image.BICUBIC)
    leaf = Image.new("RGBA", (big, big), (255, 255, 255, 255))
    leaf.putalpha(m)
    off = ((body - big) // 2, (body - big) // 2)
    layer = Image.new("RGBA", (body, body), (0, 0, 0, 0))
    sh = Image.new("RGBA", (big, big), (0, 30, 15, 130))
    sh.putalpha(m.point(lambda v: v * 130 // 255))
    layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(body // 60)), (max(off[0], 0) + body // 70, max(off[1], 0) + body // 45)) if False else None
    tmp = Image.new("RGBA", (body, body), (0, 0, 0, 0))
    tmp.paste(sh.filter(ImageFilter.GaussianBlur(body // 60)), (off[0] + body // 70, off[1] + body // 45))
    bg = Image.alpha_composite(bg, tmp)
    tmp = Image.new("RGBA", (body, body), (0, 0, 0, 0))
    tmp.paste(leaf, off)
    bg = Image.alpha_composite(bg, tmp)

    d = ImageDraw.Draw(bg)
    cx, cy, h = body // 2, body // 2, int(body * 0.12)
    d.polygon([(cx - h * 0.6, cy - h), (cx - h * 0.6, cy + h), (cx + h * 1.0, cy)], fill=(11, 74, 48, 255))
    # tallo saliendo de la punta inferior izquierda
    tip = (cx - body * 0.285, cy + body * 0.285)
    d.line([tip, (tip[0] - body * 0.09, tip[1] + body * 0.09)], fill=(255, 255, 255, 255), width=int(body * 0.03))

    # esquinas redondeadas estilo macOS
    mask = Image.new("L", (body, body), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, body - 1, body - 1], radius=int(body * 0.225), fill=255)
    bg.putalpha(mask)
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh = Image.new("RGBA", (body, body), (0, 0, 0, 90))
    sh.putalpha(mask.point(lambda v: v * 90 // 255))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(S // 80)), (pad, pad + S // 100))
    canvas.alpha_composite(bg, (pad, pad))
    return canvas


if __name__ == "__main__":
    big = build().resize((1024, 1024), Image.LANCZOS)
    (OUT / "mac").mkdir(exist_ok=True)
    big.save(OUT / "mac" / "icon.png")
    big.save(OUT / "mac" / "icon.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
    big.resize((256, 256), Image.LANCZOS).save(OUT / "app" / "static" / "logo.png")
    big.resize((64, 64), Image.LANCZOS).save(OUT / "app" / "static" / "favicon.png")
    print("ok")
