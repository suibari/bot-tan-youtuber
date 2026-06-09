# thumbnail.py
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_MARU = "/usr/share/fonts/opentype/mplus/Mplus1-Bold.otf"
W, H = 1080, 1920
BLUE = (0, 133, 255)


def capture_thumbnail_frame(input_webm: str, output_png: str, emotions: list[dict]) -> None:
    """動画からサムネイル用フレームを切り出す"""
    target_time = 2.0
    for e in emotions:
        if e["emotion"] != "Happy":
            target_time = e["time"] + 0.5
            break

    cmd = ["ffmpeg", "-y", "-i", input_webm,
           "-ss", str(target_time), "-vframes", "1", output_png]
    subprocess.run(cmd, check=True, timeout=30)
    print(f"[サムネイル] {target_time}秒のフレーム切り出し: {output_png}")


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
    y = 60
    draw_text_with_outline(draw, top_text, font_top, x, y,
                           text_color=BLUE,
                           outline_color=(0, 0, 0),
                           outline_width=8)

    # 下部テキスト（青帯 + 白文字）
    font_bottom = ImageFont.truetype(FONT_MARU, 60)
    box_y = H - 220
    draw.rectangle([0, box_y, W, box_y + 180], fill=BLUE + (235,))
    bbox = draw.textbbox((0, 0), thumbnail_text, font=font_bottom)
    text_w = bbox[2] - bbox[0]
    x = (W - text_w) // 2
    y = box_y + 55
    draw.text((x, y), thumbnail_text, font=font_bottom, fill=(255, 255, 255))

    # PNG保存
    img = img.convert("RGB")
    img.save(output_png)
    print(f"[サムネイル] 生成完了: {output_png}")
