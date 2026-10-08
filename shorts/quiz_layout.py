#!/usr/bin/env python3
"""
朝のクイズ動画の画面レイアウト（ffmpeg フィルタ生成）

上部にテキスト領域（PANEL_H）を確保し、botたんは Unity 側で
-cameraOffsetY により下へずらす（サイズは変えない）。

そのため下部の字幕帯（夜版の y=1500）は使わない。カメラを上げると口が
下がってくるので、字幕帯を出すとリップシンクが隠れてしまうため。
テキストはすべて上部パネル内に表示する。

ゲージについて:
  ffmpeg 6.1.1 の drawbox は時間変数を持たない（`n` は未定義、`t` は太さ）。
  crop の w/h も config時に1回だけ評価されるためアニメーションできない。
  よって短い区間に分割した drawbox を enable='between(t,...)' で並べる。
  enable が false の間は timeline bypass されるので処理コストはほぼ増えない。
"""

import subprocess

import core

W, H = core.W, core.H
FONT = core.FONT_PATH

# 色（夜版のミントを踏襲しつつ、アクセントは朝色のアンバーにする）
MINT   = "0x00A88A"
AMBER  = "0xFFB300"
INK    = "0x2B2B2B"
SCRIM  = "black@0.45"
PANEL  = "white@0.86"
WARN   = "0xE8503A"

# 回ごとに配色を変える。毎日同じ画面が続くと量産コンテンツに見えるため
# （2026-09 から Shorts フィードに乗らなくなった。shorts/turn.py の冒頭を参照）。
# main は正解の塗り・枠・ゲージ・締めの字幕。正解の白文字が読める濃さにすること。
# accent はコーナーラベルと「せいかい！」。先頭が従来の配色
THEMES = [
    {"name": "mint",     "main": MINT,       "accent": AMBER,      "panel": PANEL},
    {"name": "coral",    "main": "0xD9485F", "accent": "0x1F8A7E", "panel": "0xFFF7F2@0.88"},
    {"name": "sky",      "main": "0x2F6FD6", "accent": "0xF28A00", "panel": "0xF3F8FF@0.88"},
    {"name": "lavender", "main": "0x6A4FE0", "accent": "0xE0457B", "panel": "0xF8F5FF@0.88"},
]
DEFAULT_THEME = THEMES[0]

# レイアウト
#
# パネルの高さは Unity のカメラオフセットと連動している。
#   実測（オフセット0/0.17の2本をピクセル解析）:
#     ピクセル密度 ≒ 3647 px/m、オフセット0のとき髪の頂点は y=96
#   カメラを Δy 上げると被写体は Δy * 3647 だけ画面内で下がるので
#     Δy = (PANEL_H - 96) / 3647 + 余裕
#
# PANEL_H を大きくしすぎると口が画面下端まで落ちて、YouTube Shorts の
# タイトル/チャンネル名オーバーレイ（下端200px程度）にリップシンクが隠れる。
# PANEL_H=470 / Δy=0.11 で、口が y≒1660、顎の下に約230pxの余白が残る。
#
# 収録済み50問はすべて問題文が1行に収まるため、3行ぶんの領域は確保していない
# （2行までは位置を動かさずに入る。3行以上になるとフォントを46pxに落とす）。
# ここを変えるならカメラオフセットも合わせて変えること。
PANEL_H     = 470
LABEL_Y     = 26
LABEL_H     = 62
Q_Y         = 108
Q_SIZE      = 52
Q_LINE_H    = 58           # 通常は1行。2行までは位置を動かさずに収まる
CHOICE_X    = 90
CHOICE_W    = W - 180
CHOICE_H    = 76
CHOICE_A_Y  = 236
CHOICE_B_Y  = 320
CHOICE_SIZE = 46
GAUGE_X     = 90
GAUGE_Y     = 412          # 選択肢B(320〜396)より下に置く
GAUGE_W     = 750
GAUGE_H     = 30
# カウントダウン数字はゲージの右側に置く。パネルの外に出すと
# botたんの頭（髪の頂点が y≒PANEL_H）に重なってしまう。
COUNT_X     = GAUGE_X + GAUGE_W + 35
COUNT_Y     = 398
COUNT_SIZE  = 58

CORNER_LABEL = "朝の勘違いクイズ"


# ──────────────────────────────────────────────
# テキスト折り返し
# ──────────────────────────────────────────────

# 折り返しは夜版と共有する（core）。禁則処理を2箇所に持つと片方だけ直る。
wrap_cjk            = core.wrap_cjk
wrap_subtitle_lines = core.wrap_subtitle_lines
_NO_LINE_START      = core._NO_LINE_START
_NO_LINE_END        = core._NO_LINE_END
_char_width         = core._char_width


def _enable(start: float, end: float) -> str:
    return f":enable='between(t,{start:.3f},{end:.3f})'"


def _text(content: str, size: int, color: str, x: str, y, start: float, end: float,
          borderw: int = 0, bordercolor: str = "white", line_spacing: int = None) -> str:
    """drawtext を1本組み立てる。

    expansion=none は必須。これが無いと CSV に `%{...}` が入ったときに
    ffmpeg の内部変数が展開されてしまう（`\\%` でエスケープしても防げない）。
    """
    parts = [
        f"drawtext=fontfile={FONT}",
        f"text='{core.esc_drawtext(content)}'",
        "expansion=none",
        f"fontsize={size}",
        f"fontcolor={color}",
        f"x={x}",
        f"y={y}",
    ]
    if borderw:
        parts += [f"borderw={borderw}", f"bordercolor={bordercolor}"]
    if line_spacing is not None:
        parts.append(f"line_spacing={line_spacing}")
    return ":".join(parts) + _enable(start, end)


# ──────────────────────────────────────────────
# 各パーツ
# ──────────────────────────────────────────────

def build_panel_filters(start: float, end: float, theme: dict = DEFAULT_THEME) -> list[str]:
    """上部の白パネルとコーナーラベル"""
    panel, accent = theme["panel"], theme["accent"]
    box_w = min(len(CORNER_LABEL) * 38 + 40, W - 40)
    return [
        f"drawbox=x=0:y=0:w={W}:h={PANEL_H}:color={panel}:t=fill" + _enable(start, end),
        f"drawbox=x=20:y={LABEL_Y}:w={box_w}:h={LABEL_H}:color=white@0.95:t=fill" + _enable(start, end),
        f"drawbox=x=20:y={LABEL_Y+LABEL_H-2}:w={box_w}:h=6:color={accent}:t=fill" + _enable(start, end),
        _text(CORNER_LABEL, 36, accent, "30", LABEL_Y + 12, start, end),
    ]


def build_question_filters(question: str, start: float, end: float) -> list[str]:
    """問題文（最大3行。溢れるならフォントを落とす）"""
    size, line_h = Q_SIZE, Q_LINE_H
    lines = wrap_cjk(question, max_units=(W - 120) * 2 // size)
    if len(lines) > 3:
        size, line_h = 46, 56
        lines = wrap_cjk(question, max_units=(W - 120) * 2 // size)

    filters = []
    for i, line in enumerate(lines[:4]):
        filters.append(
            _text(line, size, INK, "(w-text_w)/2", Q_Y + i * line_h, start, end))
    return filters


def _choice_filters(label: str, text: str, y: int, box_color: str, text_color: str,
                    start: float, end: float, border: str = None) -> list[str]:
    f = [f"drawbox=x={CHOICE_X}:y={y}:w={CHOICE_W}:h={CHOICE_H}"
         f":color={box_color}:t=fill" + _enable(start, end)]
    if border:
        f.append(f"drawbox=x={CHOICE_X}:y={y}:w={CHOICE_W}:h={CHOICE_H}"
                 f":color={border}:t=6" + _enable(start, end))
    f.append(_text(label, 50, text_color, str(CHOICE_X + 36), y + 12, start, end))
    f.append(_text(text, CHOICE_SIZE, text_color, str(CHOICE_X + 118), y + 15, start, end))
    return f


def build_choice_filters(quiz: dict, start: float, end: float,
                         theme: dict = DEFAULT_THEME) -> list[str]:
    """未回答状態の選択肢A/B"""
    return (
        _choice_filters("A", quiz["選択肢A"], CHOICE_A_Y,
                        "white@0.95", INK, start, end, border=theme["main"])
        + _choice_filters("B", quiz["選択肢B"], CHOICE_B_Y,
                          "white@0.95", INK, start, end, border=theme["main"])
    )


def build_answer_filters(quiz: dict, start: float, end: float,
                         theme: dict = DEFAULT_THEME) -> list[str]:
    """正解発表。不正解をスクリムで沈め、正解をテーマ色で塗って白文字にする。

    フィルタは後に書いたものが上に描かれるので、未回答状態の上に重ねる前提。
    """
    correct = quiz["正解"]
    y_ok = CHOICE_A_Y if correct == "A" else CHOICE_B_Y
    y_ng = CHOICE_B_Y if correct == "A" else CHOICE_A_Y
    ok_text = quiz["選択肢A"] if correct == "A" else quiz["選択肢B"]

    return [
        # 不正解を沈める
        f"drawbox=x={CHOICE_X}:y={y_ng}:w={CHOICE_W}:h={CHOICE_H}"
        f":color={SCRIM}:t=fill" + _enable(start, end),
        # 正解を塗りつぶして白枠で囲む
        f"drawbox=x={CHOICE_X}:y={y_ok}:w={CHOICE_W}:h={CHOICE_H}"
        f":color={theme['main']}@0.97:t=fill" + _enable(start, end),
        f"drawbox=x={CHOICE_X}:y={y_ok}:w={CHOICE_W}:h={CHOICE_H}"
        f":color=white:t=7" + _enable(start, end),
        _text(correct, 50, "white", str(CHOICE_X + 36), y_ok + 12, start, end),
        _text(ok_text, CHOICE_SIZE, "white", str(CHOICE_X + 118), y_ok + 15, start, end),
        # せいかいバッジ
        _text("せいかい！", 36, theme["accent"], str(CHOICE_X + CHOICE_W - 230), y_ok + 20,
              start, end, borderw=5, bordercolor="white"),
    ]


def build_gauge_filters(start: float, duration: float = 3.0, steps: int = 50,
                        theme: dict = DEFAULT_THEME) -> list[str]:
    """減少ゲージ。drawbox が時間式を持てないので階段状に並べる。

    実測: 1080x1920 / 5秒 / 50段でエンコード増分は 0.6秒程度。
    既定値は THINK_DURATION（quiz_pipeline.py）と揃えてある。
    """
    end = start + duration
    filters = [
        # トラック
        f"drawbox=x={GAUGE_X}:y={GAUGE_Y}:w={GAUGE_W}:h={GAUGE_H}"
        f":color=black@0.25:t=fill" + _enable(start, end),
    ]
    step = duration / steps
    for i in range(steps):
        s = start + i * step
        e = s + step
        ratio = 1.0 - i / steps
        w = int(GAUGE_W * ratio)
        if w < 4:
            continue
        color = theme["main"] if ratio > 0.2 else WARN
        filters.append(
            f"drawbox=x={GAUGE_X}:y={GAUGE_Y}:w={w}:h={GAUGE_H}"
            f":color={color}@0.95:t=fill" + _enable(s, e))
    filters.append(
        f"drawbox=x={GAUGE_X}:y={GAUGE_Y}:w={GAUGE_W}:h={GAUGE_H}"
        f":color=white@0.85:t=4" + _enable(start, end))
    return filters


def build_countdown_filters(start: float, duration: float = 3.0,
                            theme: dict = DEFAULT_THEME) -> list[str]:
    """残り秒数の数字（1秒ごとに切り替え）。ゲージの右側、パネル内に置く。

    既定値は THINK_DURATION（quiz_pipeline.py）と揃えてある。
    表示する数字は duration から作るので、秒数を変えても追従する。
    """
    n = int(duration)
    return [
        _text(str(n - i), COUNT_SIZE, theme["main"], str(COUNT_X), COUNT_Y,
              start + i, start + i + 1, borderw=6, bordercolor="white")
        for i in range(n)
    ]


def build_caption_filters(subtitles: list[dict], parts: set[str],
                          y: int, size: int, color: str = INK,
                          line_h: int = None) -> list[str]:
    """指定パートの字幕を上部領域に表示する。長い行は折り返す。"""
    line_h = line_h or int(size * 1.2)
    max_units = (W - 120) * 2 // size
    filters = []
    for s in subtitles:
        if s.get("part") not in parts:
            continue
        lines = wrap_subtitle_lines(s["text"], max_units)
        for i, line in enumerate(lines):
            filters.append(
                _text(line, size, color, "(w-text_w)/2", y + i * line_h,
                      s["start"], s["end"]))
    return filters


# ──────────────────────────────────────────────
# 組み立て
# ──────────────────────────────────────────────

def build_quiz_filters(quiz: dict, seg: dict, subtitles: list[dict],
                       theme: dict = DEFAULT_THEME, panel_captions: bool = True,
                       draw_panel: bool = True) -> list[str]:
    """クイズ動画のフィルタチェーン全体を組む。

    seg: {"Q": {...}, "THINK": {...}, ...} パートIDをキーにした辞書
    theme: THEMES の1つ。抽選は呼び出し側（quiz_pipeline）で行う
    panel_captions: False なら台詞の字幕をパネルに出さない（パネルの下に夜版と同じ
                    縁取りの字幕を出すとき）。問題文は最後まで残す
    draw_panel: False ならパネル・問題・選択肢・正解ハイライトを描かず、ゲージと
                カウントダウンだけを返す（パネルを rich_panel_images の画像で重ねるとき）
    """
    t_q     = seg["Q"]["start"]
    t_think = seg["THINK"]["start"]
    t_a     = seg["A"]["start"]
    t_expl  = seg["EXPL"]["start"]
    t_aff   = seg["AFF"]["start"]
    t_end   = seg["END"]["start"]
    t_last  = seg["END"]["end"] + 1.0

    f = []
    if not draw_panel:
        return (build_gauge_filters(t_think, seg["THINK"]["duration"], theme=theme)
                + build_countdown_filters(t_think, seg["THINK"]["duration"], theme=theme))
    # パネルは最初から最後まで
    f += build_panel_filters(0.0, t_last, theme)

    # 問題文は Q 〜 解説開始まで（解説中は同じ場所に解説字幕を出す）。
    # 字幕をパネルに出さないときは最後まで残す
    f += build_question_filters(quiz["問題"], t_q, t_expl if panel_captions else t_last)

    # 選択肢は Q 〜 全肯定コメント開始まで
    f += build_choice_filters(quiz, t_q, t_aff, theme)

    # シンキングタイム
    f += build_gauge_filters(t_think, seg["THINK"]["duration"], theme=theme)
    f += build_countdown_filters(t_think, seg["THINK"]["duration"], theme=theme)

    # 正解発表以降のハイライト（選択肢の上に重ねる）
    f += build_answer_filters(quiz, t_a, t_aff, theme)

    if not panel_captions:
        return f

    # 字幕
    #   Q/THINK は問題文そのものが出ているので字幕は出さない
    #   A は正解ハイライトがあるので出さない
    f += build_caption_filters(subtitles, {"EXPL"}, y=Q_Y, size=46)
    # 全肯定〜エンディングは選択肢が消えてパネルが空くので、中央寄りに置く
    f += build_caption_filters(subtitles, {"AFF", "END"}, y=190, size=54, color=theme["main"])

    return f


def render_preview(vf_parts: list[str], output_mp4: str, duration: float,
                   bg: str = "0x35507a") -> None:
    """Unityを回さずに単色背景へ合成する。レイアウト調整の反復用。

    Unity録画が1本20分以上かかるので、ここを速く回せるかが生産性を決める。
    """
    vf = ",".join(vf_parts)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:d={duration:.2f}:r=30",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        output_mp4,
    ]
    subprocess.run(cmd, check=True, timeout=300)
    print(f"[Preview] 出力: {output_mp4} ({duration:.1f}秒, フィルタ{len(vf_parts)}個)")


# ──────────────────────────────────────────────
# リッチなパネル（画像で描く）
# ──────────────────────────────────────────────
# drawbox / drawtext で組んだパネルは、白い板に普通のゴシック体を置いただけだった。
# 夜版の字幕と同じ丸ゴシックの極太で、角丸のカードと A/B の丸いバッジに描き直す。
# 回答前と正解発表後の2枚を作り、正解発表の時刻で差し替える。
# ゲージとカウントダウンは時間で動くので、今までどおり ffmpeg で上に描く
# （位置 GAUGE_* / COUNT_* はこのパネルの中に収まるようにしてある）。

PANEL_RADIUS = 28
CHOICE_RADIUS = CHOICE_H // 2


def _rich_font(size: int):
    from PIL import ImageFont
    import rich_text
    return ImageFont.truetype(rich_text.FONT, size)


def _rgb(c: str) -> tuple:
    import rich_text
    return rich_text._rgb(c.split("@")[0])


def _choice_card(draw, y: int, label: str, text: str, fill, edge, badge, badge_text, ink):
    draw.rounded_rectangle((CHOICE_X, y, CHOICE_X + CHOICE_W, y + CHOICE_H),
                           radius=CHOICE_RADIUS, fill=fill, outline=edge, width=5)
    r = CHOICE_H // 2 - 8
    cx, cy = CHOICE_X + CHOICE_H // 2 + 4, y + CHOICE_H // 2
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=badge)
    f = _rich_font(44)
    draw.text((cx, cy), label, font=f, fill=badge_text, anchor="mm")
    draw.text((CHOICE_X + CHOICE_H + 24, cy), text, font=_rich_font(CHOICE_SIZE), fill=ink,
              anchor="lm")


def rich_panel_images(quiz: dict, theme: dict, out_dir) -> tuple:
    """回答前と正解発表後のパネルを PNG にする。返り値は (回答前, 正解発表後) のパス。"""
    from pathlib import Path
    from PIL import Image, ImageDraw, ImageFilter
    import rich_text

    main, accent = _rgb(theme["main"]), _rgb(theme["accent"])
    ink = _rgb(INK)

    def base():
        img = Image.new("RGBA", (W, PANEL_H + 24), (0, 0, 0, 0))
        # 影
        sh = Image.new("L", img.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle((16, 8, W - 16, PANEL_H + 4), radius=PANEL_RADIUS, fill=110)
        img.paste(Image.new("RGBA", img.size, (20, 30, 60, 255)), (0, 0), sh.filter(ImageFilter.GaussianBlur(8)))
        # 白からテーマ色へ薄く寄せたグラデーションのカード
        card = Image.new("RGBA", (W - 32, PANEL_H - 8))
        top, bottom = (255, 255, 255), rich_text._lighten(main, 0.88)
        for yy in range(card.height):
            k = yy / card.height
            card.paste(tuple(round(a + (b - a) * k) for a, b in zip(top, bottom)) + (255,),
                       (0, yy, card.width, yy + 1))
        m = Image.new("L", card.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, card.width - 1, card.height - 1),
                                            radius=PANEL_RADIUS, fill=255)
        img.paste(card, (16, 0), m)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((16, 0, W - 17, PANEL_H - 9), radius=PANEL_RADIUS, outline=main, width=6)
        # コーナーラベル（リボン）
        f = _rich_font(34)
        tw = int(d.textlength(CORNER_LABEL, font=f))
        d.rounded_rectangle((34, LABEL_Y, 34 + tw + 44, LABEL_Y + LABEL_H - 6),
                            radius=(LABEL_H - 6) // 2, fill=accent)
        d.text((34 + 22, LABEL_Y + (LABEL_H - 6) // 2), CORNER_LABEL, font=f,
               fill=(255, 255, 255), anchor="lm")
        # 問題文（最大2行。入らなければ小さくする）
        size = Q_SIZE + 4
        while True:
            lines = wrap_cjk(quiz["問題"], max_units=(W - 140) * 2 // size)
            if len(lines) <= 2 or size <= 40:
                break
            size -= 4
        for i, line in enumerate(lines[:3]):
            q = rich_text.outlined_line(line, size, "#2B2B2B", inner="white", inner_w=6,
                                        outer_w=2, outline=(255, 255, 255), gradient=0.0,
                                        shadow=False)
            img.alpha_composite(q, ((W - q.width) // 2, Q_Y - 30 + i * (size + 8)))
        return img

    before = base()
    d = ImageDraw.Draw(before)
    for label, y in (("A", CHOICE_A_Y), ("B", CHOICE_B_Y)):
        _choice_card(d, y, label, quiz[f"選択肢{label}"], fill=(255, 255, 255, 245), edge=main,
                     badge=main, badge_text=(255, 255, 255), ink=ink)

    after = base()
    d = ImageDraw.Draw(after)
    correct = quiz["正解"]
    for label, y in (("A", CHOICE_A_Y), ("B", CHOICE_B_Y)):
        if label == correct:
            _choice_card(d, y, label, quiz[f"選択肢{label}"], fill=main + (255,), edge=(255, 255, 255),
                         badge=(255, 255, 255), badge_text=main, ink=(255, 255, 255))
        else:
            _choice_card(d, y, label, quiz[f"選択肢{label}"], fill=(214, 214, 214, 235),
                         edge=(170, 170, 170), badge=(160, 160, 160), badge_text=(255, 255, 255),
                         ink=(130, 130, 130))
    stamp = rich_text.outlined_line("せいかい！", 44, core.TOP_SUB_PEAK_COLOR, inner="#1E2B4F",
                                    inner_w=6, outer_w=6, outline=(255, 255, 255), gradient=0.0,
                                    shadow=False).rotate(8, expand=True, resample=Image.BICUBIC)
    y_ok = CHOICE_A_Y if correct == "A" else CHOICE_B_Y
    after.alpha_composite(stamp, (CHOICE_X + CHOICE_W - stamp.width + 30,
                                  y_ok + CHOICE_H // 2 - stamp.height // 2))

    out_dir = Path(out_dir)
    p1, p2 = out_dir / "quiz_panel_q.png", out_dir / "quiz_panel_a.png"
    before.save(p1)
    after.save(p2)
    return p1, p2
