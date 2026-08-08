# thumbnail.py
import random
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_MARU = "/usr/share/fonts/truetype/keifont/keifont.ttf"
W, H = 1080, 1920
MINT = (0, 168, 138)


def capture_frame(input_webm: str, output_png: str, target_time: float) -> None:
    """指定した時刻のフレームを1枚切り出す"""
    cmd = ["ffmpeg", "-y", "-i", input_webm,
           "-ss", str(target_time), "-vframes", "1", output_png]
    subprocess.run(cmd, check=True, timeout=30)
    print(f"[サムネイル] {target_time}秒のフレーム切り出し: {output_png}")


def capture_thumbnail_frame(input_webm: str, output_png: str, emotions: list[dict]) -> None:
    """動画からサムネイル用フレームを切り出す"""
    target_time = 2.0
    # バニラHappy（高valence・低arousal）以外の表情が出るフレームを優先する
    expressive = [e for e in emotions if e.get("valence", 0.8) <= 0.6 or e.get("arousal", 0.3) >= 0.4]
    if expressive:
        chosen = random.choice(expressive)
        target_time = chosen["time"] + random.uniform(0.0, 1.5)

    capture_frame(input_webm, output_png, target_time)


def draw_text_with_outline(draw, text, font, x, y, text_color, outline_color, outline_width=5):
    """縁取りテキストを描画する"""
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=text_color)


def generate_thumbnail(frame_png: str, output_png: str, thumbnail_text: str) -> None:
    """サムネイル画像を生成する"""
    # ベース画像（フレーム）を読み込み
    img = Image.open(frame_png).convert("RGBA")
    img = img.resize((W, H))
    draw = ImageDraw.Draw(img)

    # 上部テキスト「今日の全肯定」
    font_top = ImageFont.truetype(FONT_MARU, 160)
    top_text = "今日の全肯定"
    bbox = draw.textbbox((0, 0), top_text, font=font_top)
    text_w = bbox[2] - bbox[0]
    x = (W - text_w) // 2
    y = 190
    draw_text_with_outline(draw, top_text, font_top, x, y,
                           text_color=MINT,
                           outline_color=(255, 255, 255),
                           outline_width=8)

    # 下部テキスト（ミント帯 + 白文字）
    font_size = 90
    font_bottom = ImageFont.truetype(FONT_MARU, font_size)
    bbox = draw.textbbox((0, 0), thumbnail_text, font=font_bottom)
    while bbox[2] - bbox[0] > W - 40 and font_size > 50:
        font_size -= 5
        font_bottom = ImageFont.truetype(FONT_MARU, font_size)
        bbox = draw.textbbox((0, 0), thumbnail_text, font=font_bottom)
    box_y = H - 480
    box_h = 230
    draw.rectangle([0, box_y, W, box_y + box_h], fill=MINT + (235,))
    bbox = draw.textbbox((0, 0), thumbnail_text, font=font_bottom)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (W - text_w) // 2
    y = box_y + (box_h - text_h) // 2 - bbox[1]
    draw.text((x, y), thumbnail_text, font=font_bottom, fill=(255, 255, 255))

    # PNG保存
    img = img.convert("RGB")
    img.save(output_png)
    print(f"[サムネイル] 生成完了: {output_png}")


# ──────────────────────────────────────────────
# 朝のクイズ版サムネイル
# ──────────────────────────────────────────────

def _wrap_cjk(text: str, font, draw, max_w: int) -> list[str]:
    """描画幅で折り返す。行頭に来てほしくない文字は前の行に送る。"""
    no_line_start = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮー、。，．・？！」』）】")
    lines, cur = [], ""
    for ch in text:
        cand = cur + ch
        if cur and draw.textbbox((0, 0), cand, font=font)[2] > max_w:
            if ch in no_line_start:
                lines.append(cand)
                cur = ""
                continue
            lines.append(cur)
            cur = ch
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def generate_quiz_thumbnail(frame_png: str, output_png: str, question_text: str,
                            corner_text: str = "朝の勘違いクイズ",
                            max_lines: int = 3) -> None:
    """問題文を中央に極大表示したサムネイルを作る。

    既存の generate_thumbnail は単一行専用なので流用できない。
    縁取りは draw_text_with_outline だと (2w+1)^2 回描画することになるため
    （170px×3行×outline10 で2000回超）、PIL の stroke_width を使う。
    """
    img = Image.open(frame_png).convert("RGBA").resize((W, H))
    draw = ImageDraw.Draw(img)

    # 上部ラベル
    font_top = ImageFont.truetype(FONT_MARU, 110)
    bbox = draw.textbbox((0, 0), corner_text, font=font_top)
    draw_text_with_outline(draw, corner_text, font_top,
                           (W - (bbox[2] - bbox[0])) // 2, 150,
                           text_color=MINT, outline_color=(255, 255, 255),
                           outline_width=8)

    # 問題文（収まるまでフォントを落とす）
    size = 170
    while size > 70:
        font = ImageFont.truetype(FONT_MARU, size)
        lines = _wrap_cjk(question_text, font, draw, W - 100)
        block_h = len(lines) * int(size * 1.22)
        if len(lines) <= max_lines and block_h <= 780:
            break
        size -= 8

    top = 420
    # 半透明の下敷きで背景（キャラ）に負けないようにする
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(
        [0, top - 45, W, top + block_h + 45], fill=MINT + (140,))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text(((W - (bbox[2] - bbox[0])) // 2, top + i * int(size * 1.22)),
                  line, font=font, fill=(255, 255, 255),
                  stroke_width=10, stroke_fill=MINT)

    # 煽り文句は問題文の直下に置く（キャラの顔に重ねない）
    font_ab = ImageFont.truetype(FONT_MARU, 120)
    bbox = draw.textbbox((0, 0), "A or B？", font=font_ab)
    draw_text_with_outline(draw, "A or B？", font_ab,
                           (W - (bbox[2] - bbox[0])) // 2, top + block_h + 70,
                           text_color=(255, 255, 255), outline_color=MINT,
                           outline_width=10)

    img.convert("RGB").save(output_png)
    print(f"[サムネイル] 生成完了: {output_png} (問題文 {size}px / {len(lines)}行)")
