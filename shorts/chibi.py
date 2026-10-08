#!/usr/bin/env python3
"""山場の演出: ちびキャラのイラストへの切り替えと効果音（夜版）。

参考にした伸びている Shorts は、普段はほぼ同じ画角で喋り、山場の台詞だけ
画面全体をちびキャラ＋集中線の背景に切り替えて、効果音を鳴らしていた。
どの文が山場か・どの差分を出すかは、台本の各文の "reaction"（LLM が選ぶ）で決める。

素材:
  data/chibi/<差分名>.png   背景透過。大きさはそろっていなくてよい（ここで縮めてそろえる）
  data/se/<ファイル名>.mp3  効果音ラボの効果音。**再配布禁止なので git に入れない**
                            （.gitignore 済み）。無ければ効果音なしで続ける
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

import core

CHIBI_DIR = core._REPO_ROOT / "data" / "chibi"
SE_DIR    = core._REPO_ROOT / "data" / "se"

# 差分名 → (背景の地の色, 集中線の色, 効果音)。
# 効果音は効果音ラボ（https://soundeffect-lab.info/sound/anime/）の項目
REACTIONS = {
    "surprising": ("#ff5a3c", "#ff8466", "surprise1.mp3"),     # 驚く（頭の上に「！」）
    "joyful":     ("#ffa62b", "#ffc163", "cute-pose1.mp3"),    # 可愛く輝く1
    "smug":       ("#ff7f1f", "#ffa15a", "eye-shine1.mp3"),    # きらーん1
    "shame":      ("#ff7aa2", "#ff9fbd", "heart2.mp3"),        # 心臓の鼓動2 ドキッ
    "down":       ("#6d8db6", "#8aa6c9", "cute-sad1.mp3"),     # しょげる
    "thinking":   ("#3fb79d", "#6fcdb8", "recollection1.mp3"), # 回想
    "sitting":    ("#ffcc33", "#ffdb6b", "pa1.mp3"),           # パッ
}
# ちびキャラを出さずに強く寄る山場（決め台詞など）で鳴らす音（キラッ2）
EMPHASIS_SE = "kira2.mp3"

MAX_INSERTS   = 3      # 1本あたりの差し込みの上限。多いと山場が山場でなくなる
INSERT_MIN    = 1.0    # 1回の差し込みの長さ[秒]
INSERT_MAX    = 3.2    # 強調語句が字幕2枚にわたるとき、言い終わりまで出す
CHIBI_W       = 940    # 画面上のちびキャラの幅[px]（髪の広がりまで含めた外接矩形）
CHIBI_BOTTOM  = core.H - 300   # 足元の位置。下端 300px は Shorts のタイトル等が重なる
BURST_CENTER  = (core.W // 2, 1150)
BURST_RAYS    = 18
POP_PX        = 70     # 出てくるときに下から跳ね上がる量[px]
POP_SEC       = 0.15


def available() -> set[str]:
    return {n for n in REACTIONS if (CHIBI_DIR / f"{n}.png").exists()}


def normalized(name: str, out_dir: Path) -> Path:
    """外接矩形で切り抜き、幅を CHIBI_W にそろえる。

    素材ごとにキャンバスの大きさと余白が違うので、キャンバスではなく描かれている
    部分の幅でそろえる（全差分とも座りポーズで、髪の広がりまでの幅がほぼ同じ）。
    ドット絵風だが格子がそろっていないので、nearest ではなく LANCZOS で縮める。
    """
    out = Path(out_dir) / f"chibi_{name}.png"
    im = Image.open(CHIBI_DIR / f"{name}.png").convert("RGBA")
    box = im.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox()
    im = im.crop(box)
    scale = CHIBI_W / im.width
    im = im.resize((CHIBI_W, round(im.height * scale)), Image.LANCZOS)
    im.save(out)
    return out


def burst_background(name: str, out_dir: Path) -> Path:
    """差分ごとの色で、中心から放射する集中線の背景を描く。"""
    out = Path(out_dir) / f"burst_{name}.png"
    base, ray, _ = REACTIONS[name]
    im = Image.new("RGB", (core.W, core.H), base)
    d = ImageDraw.Draw(im)
    cx, cy = BURST_CENTER
    r = math.hypot(core.W, core.H)
    step = 2 * math.pi / BURST_RAYS
    for i in range(BURST_RAYS):
        a0, a1 = i * step, i * step + step / 2
        d.polygon([(cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0)),
                   (cx + r * math.cos(a1), cy + r * math.sin(a1))], fill=ray)
    im.save(out)
    return out


def subtitles_in(span: dict, subtitles: list[dict]) -> list[dict]:
    return [s for s in subtitles
            if s["start"] >= span["start"] - 0.05 and s["end"] <= span["end"] + 0.1]


def plan_inserts(spans: list[dict], subtitles: list[dict]) -> list[dict]:
    """reaction の付いた文ごとに、差し込む区間を決める。

    強調する語句（台本の "peak"）の字幕に合わせる。字幕はその語句で区切ってあるので
    （core.split_with_peaks）、ちょうどその語句を言っている間だけイラストになる。
    peak が見つからなければ文の最後の字幕1枚に合わせる。
    短すぎれば手前へ広げ、長すぎれば頭から INSERT_MAX 秒だけにする。
    """
    have = available()
    out = []
    for sp in spans:
        name = sp.get("reaction")
        if not name or name not in have:
            continue
        inside = subtitles_in(sp, subtitles)
        peak = [s for s in inside if s.get("peak")]
        if peak:
            start, end = peak[0]["start"], peak[-1]["end"]
        elif inside:
            start, end = inside[-1]["start"], inside[-1]["end"]
        else:
            start, end = sp["start"], sp["end"]
        if end - start < INSERT_MIN:
            start = max(sp["start"], end - INSERT_MIN)
        if end - start > INSERT_MAX:
            end = start + INSERT_MAX
        out.append({"start": round(start, 3), "end": round(end, 3), "reaction": name})
        if len(out) >= MAX_INSERTS:
            break
    return out


def build_overlays(inserts: list[dict], out_dir: Path) -> list[dict]:
    """core.run_ffmpeg_finalize の overlays。背景を敷いてから、ちびキャラを跳ね上げて出す。"""
    overlays, cache = [], {}
    for ins in inserts:
        name = ins["reaction"]
        if name not in cache:
            cache[name] = (burst_background(name, out_dir), normalized(name, out_dir))
        bg, fg = cache[name]
        h = Image.open(fg).height
        y0 = CHIBI_BOTTOM - h
        s = ins["start"]
        overlays.append({"path": bg, "start": s, "end": ins["end"], "x": 0, "y": 0})
        overlays.append({
            "path": fg, "start": s, "end": ins["end"],
            "x": (core.W - CHIBI_W) // 2,
            "y": f"{y0}+{POP_PX}*pow(max(0,1-(t-{s})/{POP_SEC}),2)",
        })
    return overlays


def build_sfx(inserts: list[dict], emphasis_starts: list[float] = ()) -> list[dict]:
    """差し込みの頭と、ちびキャラを出さない山場の寄りの頭で鳴らす。ファイルが無ければ鳴らさない。"""
    out = [{"path": SE_DIR / REACTIONS[i["reaction"]][2], "start": i["start"]} for i in inserts]
    out += [{"path": SE_DIR / EMPHASIS_SE, "start": t} for t in emphasis_starts]
    missing = sorted({e["path"].name for e in out if not e["path"].exists()})
    if missing:
        print(f"[効果音] 見つからないので鳴らしません: {', '.join(missing)}（{SE_DIR}）")
    return [e for e in out if e["path"].exists()]
