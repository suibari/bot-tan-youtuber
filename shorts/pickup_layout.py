#!/usr/bin/env python3
"""
朝の作品紹介動画のパネル（アートワーク・作品名・日付・画像の出典）

パネルの高さはクイズと同じ quiz_layout.PANEL_H にする。Unity のカメラオフセット
（quiz_pipeline.CAMERA_OFFSET_Y）がこの高さと連動しているので、変えると botたんの
口が字幕帯や Shorts の UI に落ちる。

アートワークは引用として扱う（主役は botたん）:
  - パネルの中の枠に収め、全画面にしない
  - 縮小と枠付けだけにして、切り抜き・色調変更はしない
  - 画像のすぐ下に出典（pickup_sources.IMAGE_CREDITS）を出す
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

import quiz_layout
import rich_text
from pickup_sources import THEME_LABELS, jp_date

W = quiz_layout.W
PANEL_H = quiz_layout.PANEL_H
THEMES = quiz_layout.THEMES
CORNER_LABEL = "botたんの作品紹介"

LABEL_Y = 22
ART_X, ART_Y = 44, 92           # アートワークの枠（左上）。出典の行までパネルに収まる最大
ART_MAX_W, ART_MAX_H = 360, 330
CREDIT_SIZE = 20
TEXT_X_GAP = 34
TITLE_MAX_SIZE, TITLE_MIN_SIZE = 60, 36
INFO_SIZE = 34

# 日付の出し方。放送中のものは日付を出さない。AniList の放送開始日は
# 配信先行の日のことがあり、地上波の日付と食い違った（PoC の転剣: AniList 10/1、地上波 10/7）
DATE_FORMATS = {
    "anime_new": "放送中",
    "anime_old": "{date} 放送開始",
    "music_new": "{date} 配信",
    "movie_new": "{date} 公開",
    "movie_old": "{date} 公開",
    "drama_new": "放送中",
    "game_new":  "{date} 発売",
}


def date_line(item: dict) -> str:
    return DATE_FORMATS[item["theme"]].format(date=jp_date(item["date"]))


def sub_lines(item: dict) -> list[str]:
    """作品名の下に出す行。データベースの値だけを使う（台本の値は使わない）。"""
    lines = [item["artist"]] if item.get("artist") else []
    lines.append(date_line(item))
    return lines


def _fit_title(draw, title: str, max_w: int) -> tuple[list[str], int]:
    """3行までに収める。最後の行が1〜2文字だけになるときも小さくする（「け」だけが落ちた）。"""
    size = TITLE_MAX_SIZE
    while True:
        lines = quiz_layout.wrap_cjk(title, max_units=max_w * 2 // size)
        orphan = len(lines) > 1 and len(lines[-1]) <= 2
        if (len(lines) <= 3 and not orphan) or size <= TITLE_MIN_SIZE:
            return lines[:3], size
        size -= 4


def panel_image(item: dict, artwork: Path, theme: dict, out_dir) -> Path:
    """パネルを PNG にする。動画の最初から最後まで出し続ける。"""
    main, accent = quiz_layout._rgb(theme["main"]), quiz_layout._rgb(theme["accent"])
    ink = quiz_layout._rgb(quiz_layout.INK)

    img = Image.new("RGBA", (W, PANEL_H + 24), (0, 0, 0, 0))
    # 影とカードはクイズのパネルと同じ作り
    sh = Image.new("L", img.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle((16, 8, W - 16, PANEL_H + 4),
                                         radius=quiz_layout.PANEL_RADIUS, fill=110)
    img.paste(Image.new("RGBA", img.size, (20, 30, 60, 255)), (0, 0),
              sh.filter(ImageFilter.GaussianBlur(8)))
    card = Image.new("RGBA", (W - 32, PANEL_H - 8))
    top, bottom = (255, 255, 255), rich_text._lighten(main, 0.88)
    for yy in range(card.height):
        k = yy / card.height
        card.paste(tuple(round(a + (b - a) * k) for a, b in zip(top, bottom)) + (255,),
                   (0, yy, card.width, yy + 1))
    m = Image.new("L", card.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, card.width - 1, card.height - 1),
                                        radius=quiz_layout.PANEL_RADIUS, fill=255)
    img.paste(card, (16, 0), m)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 0, W - 17, PANEL_H - 9), radius=quiz_layout.PANEL_RADIUS,
                        outline=main, width=6)

    # リボン：コーナー名 + その日のテーマ
    label = f"{CORNER_LABEL}｜{THEME_LABELS[item['theme']]}"
    f = quiz_layout._rich_font(32)
    tw = int(d.textlength(label, font=f))
    d.rounded_rectangle((34, LABEL_Y, 34 + tw + 44, LABEL_Y + 56), radius=28, fill=accent)
    d.text((34 + 22, LABEL_Y + 28), label, font=f, fill=(255, 255, 255), anchor="lm")

    # アートワーク：縦横比を保って枠に収める（切り抜かない）
    with Image.open(artwork) as src:
        art = src.convert("RGB")
    scale = min(ART_MAX_W / art.width, ART_MAX_H / art.height)
    art = art.resize((max(1, round(art.width * scale)), max(1, round(art.height * scale))),
                     Image.LANCZOS)
    ax, ay = ART_X, ART_Y + (ART_MAX_H - art.height) // 2
    d.rounded_rectangle((ax - 6, ay - 6, ax + art.width + 6, ay + art.height + 6),
                        radius=10, fill=(255, 255, 255), outline=main, width=4)
    img.paste(art, (ax, ay))
    # 出典は画像のすぐ下
    fc = quiz_layout._rich_font(CREDIT_SIZE)
    d.text((ax + art.width / 2, ay + art.height + 10), item["credit"], font=fc,
           fill=(110, 110, 110), anchor="mt")

    # 作品名と日付。縦長のポスターでは枠より細いので、実際の画像の右端から始める
    tx = ax + art.width + TEXT_X_GAP
    max_w = W - 50 - tx
    lines, size = _fit_title(d, item["title"], max_w)
    subs = sub_lines(item)
    block_h = len(lines) * (size + 10) + len(subs) * (INFO_SIZE + 12) + 16
    y = ART_Y + max(0, (ART_MAX_H - block_h) // 2)
    for line in lines:
        q = rich_text.outlined_line(line, size, "#2B2B2B", inner="white", inner_w=6,
                                    outer_w=2, outline=(255, 255, 255), gradient=0.0,
                                    shadow=False)
        img.alpha_composite(q, (tx - 8, y - 8))
        y += size + 10
    y += 16
    fi = quiz_layout._rich_font(INFO_SIZE)
    for line in subs:
        for cut in quiz_layout.wrap_cjk(line, max_units=max_w * 2 // INFO_SIZE)[:1]:
            d.text((tx, y), cut, font=fi, fill=main if line == subs[-1] else ink)
        y += INFO_SIZE + 12

    out = Path(out_dir) / "pickup_panel.png"
    img.save(out)
    return out
