#!/usr/bin/env python3
"""縁取り文字を画像（PNG）で描く。字幕と朝版のクイズ欄で使う。

ffmpeg の drawtext では「白フチ・紺フチ」の二重縁取りを、太さの違う border を
重ねて描くしかない。紺の border は文字の内側のすき間（「e」「許」の中）にも
はみ出し、文字の中に黒い点が見えた（2026-10-08）。ここでは
  1. 文字 + 白フチの形を作る
  2. その形の穴（文字の内側のすき間）を塞ぐ
  3. 穴を塞いだ形を太らせたものを紺フチにする
の順で描くので、紺は外側の輪郭にだけ付き、すき間は白になる。

塗りは上から下へのグラデーション。影は紺フチの形をずらして、ぼかして敷く。
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont

import core

FONT = core.RICH_FONT if Path(core.RICH_FONT).exists() else core.FONT_PATH
OUTLINE = (30, 43, 79)          # 紺フチ（core.TOP_SUB_OUTLINE と同じ）
SHADOW_OFFSET = (8, 10)
SHADOW_ALPHA = 110


def _rgb(c) -> tuple:
    """"0xRRGGBB"（ffmpeg の書き方）・"#RRGGBB"・色名・タプルのどれでも受ける。"""
    if isinstance(c, tuple):
        return c[:3]
    if c.startswith("0x"):
        c = "#" + c[2:]
    return ImageColor.getrgb(c)[:3]


def _lighten(rgb: tuple, k: float) -> tuple:
    return tuple(round(v + (255 - v) * k) for v in rgb)


def _fill_holes(mask: Image.Image) -> Image.Image:
    """閉じた形の穴を塞ぐ。外側から塗りつぶして、届かなかった所を形の内側とみなす。

    縮めて塗ると、戻したときに輪郭がブロック状になって紺フチがギザギザになった。
    等倍で塗る（字幕1行で 0.1秒程度）。
    """
    pad = Image.new("L", (mask.width + 2, mask.height + 2), 0)
    pad.paste(mask.point(lambda v: 255 if v > 0 else 0), (1, 1))
    ImageDraw.floodfill(pad, (0, 0), 128)
    inside = pad.crop((1, 1, mask.width + 1, mask.height + 1)).point(lambda v: 0 if v == 128 else 255)
    return ImageChops.lighter(mask, inside)


def outlined_line(text: str, size: int, fill: str, *, inner: str = "white",
                  inner_w: int = 10, outer_w: int = 10, outline: tuple = OUTLINE,
                  gradient: float = 0.45, shadow: bool = True) -> Image.Image:
    """1行の縁取り文字を、文字の外接矩形＋縁取りぶんの大きさの RGBA 画像で返す。

    fill は文字の色（下端の色。上端は gradient の割合だけ白に寄せる）。
    inner は内側のフチの色、outline は外側のフチの色。
    """
    font = ImageFont.truetype(FONT, size)
    margin = inner_w + outer_w + max(SHADOW_OFFSET) + 8
    l, t, r, b = font.getbbox(text)
    w, h = r - l + 2 * margin, b - t + 2 * margin
    origin = (margin - l, margin - t)

    glyph = Image.new("L", (w, h), 0)
    ImageDraw.Draw(glyph).text(origin, text, font=font, fill=255)
    body = Image.new("L", (w, h), 0)
    ImageDraw.Draw(body).text(origin, text, font=font, fill=255,
                              stroke_width=inner_w, stroke_fill=255)
    body = _fill_holes(body)
    outer = body.filter(ImageFilter.MaxFilter(outer_w * 2 + 1))

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if shadow:
        sh = Image.new("L", (w, h), 0)
        sh.paste(outer, SHADOW_OFFSET)
        sh = sh.filter(ImageFilter.GaussianBlur(3)).point(lambda v: v * SHADOW_ALPHA // 255)
        img.paste(Image.new("RGBA", (w, h), _rgb(outline) + (255,)), (0, 0), sh)
    img.paste(Image.new("RGBA", (w, h), _rgb(outline) + (255,)), (0, 0), outer)
    img.paste(Image.new("RGBA", (w, h), _rgb(inner) + (255,)), (0, 0), body)

    base = _rgb(fill)
    top = _lighten(base, gradient)
    grad = Image.new("RGBA", (1, h))
    y0, y1 = origin[1] + t, origin[1] + b
    for y in range(h):
        k = min(1.0, max(0.0, (y - y0) / max(1, y1 - y0)))
        grad.putpixel((0, y), tuple(round(a + (c - a) * k) for a, c in zip(top, base)) + (255,))
    img.paste(grad.resize((w, h)), (0, 0), glyph)
    return img


def caption_image(text: str, peak: bool, out: Path) -> tuple[Path, int, int]:
    """字幕1枚を PNG にする。行の割り方と文字の大きさは core._top_subtitle_layout。

    強調の1枚は黄色の文字に紺の内フチ・白の外フチ（ちびキャラの背景でも浮かせる）。
    返り値は (パス, 幅, 高さ)。画像は横いっぱい（W）で、行は中央に置く。
    """
    lines, size = core._top_subtitle_layout(
        text, core.TOP_SUB_PEAK_SIZE if peak else core.TOP_SUB_FONT_SIZE)
    if peak:
        imgs = [outlined_line(l, size, core.TOP_SUB_PEAK_COLOR, inner="#1E2B4F",
                              outline=(255, 255, 255), gradient=0.0) for l in lines]
    else:
        imgs = [outlined_line(l, size, core.TOP_SUB_COLOR) for l in lines]
    step = int(size * 1.18)
    h = step * (len(imgs) - 1) + max(i.height for i in imgs)
    canvas = Image.new("RGBA", (core.W, h), (0, 0, 0, 0))
    for k, im in enumerate(imgs):
        canvas.alpha_composite(im, ((core.W - im.width) // 2, step * k))
    canvas.save(out)
    return out, canvas.width, canvas.height


def caption_overlays(subtitles: list[dict], top: int, out_dir: Path) -> list[dict]:
    """字幕を core.run_ffmpeg_finalize の overlays にする。出るときに少し上から落ちる。

    画像の上端には縁取りと影のぶんの余白があるので、その分だけ上にずらして置く。
    """
    pad = 10 + 10 + max(SHADOW_OFFSET) + 8
    out = []
    for i, sub in enumerate(subtitles):
        path, _, _ = caption_image(sub["text"], bool(sub.get("peak")),
                                   Path(out_dir) / f"caption_{i:02d}.png")
        s = sub["start"]
        drop = f"{core.TOP_SUB_POP_PX}*pow(max(0,1-(t-{s})/{core.TOP_SUB_POP_SEC}),2)"
        out.append({"path": path, "start": s, "end": sub["end"],
                    "x": 0, "y": f"{top - pad}-{drop}"})
    return out
