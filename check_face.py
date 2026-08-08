#!/usr/bin/env python3
"""表情(valence/arousal)と口の開き具合の関係を実測するプローブ。

無音WAVの上で表情だけを一定間隔で切り替えて Unity 録画し、
口の内部（粘膜色）のピクセル面積を区間ごとに集計する。

このモデルの表情プリセットは VRoid の `Fcl_ALL_*`（眉・目・口が一体）に
割り当てられているため、Happy / Surprised の重みがそのまま「口の開き」になる。
表情値を触ったら必ずこれで測ってから本番に入れること。

    python3 check_face.py                        # 補正なしで測る
    python3 check_face.py --mouth-close 1.0      # 無音時の口成分打ち消しつき
    python3 check_face.py --reuse /tmp/x.webm    # 録画済みのwebmを測るだけ

検証スクリプトは .gitignore の `test*.py` に食われるので test_ で始めない。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import core

# 計測する (valence, arousal, ラベル)
# 朝版は quiz_pipeline.EMOTION_VARIANTS の各パートの代表値、
# 夜版は enforce_variance 後によく出る値を参照として並べる。
CASES = [
    (0.70,  0.70, "朝:Q"),
    (0.50,  0.80, "朝:THINK"),
    (0.90,  0.90, "朝:A"),
    (0.65, -0.60, "朝:EXPL"),
    (1.00, -0.30, "朝:AFF"),
    (0.80,  0.15, "朝:END"),
    (0.44, -0.44, "夜:中庸"),
    (-0.11, -0.08, "夜:Sad寄り"),
]

# 円環モデル上のブレンドシェイプ座標（VRM1FaceAnimation.BlendShapeVectors と同じ）
BLEND_VECTORS = [
    ("Happy",     1.0, -0.3),
    ("Relaxed",   0.5, -0.8),
    ("Sad",      -0.8, -0.5),
    ("Angry",    -0.7,  0.8),
    ("Surprised", 0.3,  1.0),
]
MAX_EMOTION_WEIGHT = 0.5   # SampleScene の VRM1FaceAnimation._maxEmotionWeight

FACE_X0, FACE_X1 = 340, 760   # 顔が写っている横方向の範囲


def blend_weights(valence: float, arousal: float) -> dict:
    """Unity 側と同じ内積配分でブレンドシェイプの重みを求める"""
    raw = {k: max(0.0, valence * v + arousal * a) for k, v, a in BLEND_VECTORS}
    total = sum(raw.values())
    if total <= 0:
        return {k: 0.0 for k in raw}
    return {k: w / total * MAX_EMOTION_WEIGHT for k, w in raw.items()}


def record(webm: str, seg: float, camera_offset: float, mouth_close: float) -> None:
    """無音WAV + 表情タイムラインで Unity 録画する"""
    base = webm[:-5] if webm.endswith(".webm") else webm
    wav = f"{base}.wav"
    emo = f"{base}_emotions.json"

    core.make_silence_wav(wav, seg * len(CASES))
    json.dump({
        "emotions": [{"time": round(i * seg, 2), "valence": v, "arousal": a}
                     for i, (v, a, _) in enumerate(CASES)],
        # モーション発火はノイズになるので全部止める（0 だと発火しない）
        "waveTime": 0.0, "thankfulTime": 0.0,
        "greetingTime1": 0.0, "greetingTime2": 0.0,
    }, open(emo, "w"))

    extra = ["-cameraOffsetY", f"{camera_offset}"]
    if mouth_close > 0:
        extra += ["-mouthCloseOnSilence", f"{mouth_close}"]
    core.record_with_unity(wav, webm, emo, extra_args=extra)


def _frames(webm: str, y0: int, h: int, fps: int = 4):
    """指定領域を rawvideo で読み出してフレームごとの bytes を yield する"""
    w = FACE_X1 - FACE_X0
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", webm,
         "-vf", f"fps={fps},crop={w}:{h}:{FACE_X0}:{y0}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode()[:500])
    size = w * h * 3
    buf = proc.stdout
    for i in range(len(buf) // size):
        yield buf[i * size:(i + 1) * size]


def _mouth_pixels(frame: bytes) -> int:
    """口の内部（粘膜色）とみなせるピクセル数。肌は R-B≒37 なので 60 で切れる。"""
    n = 0
    for j in range(0, len(frame), 3):
        if frame[j] > 190 and frame[j] - frame[j + 2] > 60:
            n += 1
    return n


# 口の縦位置。カメラオフセット0で y=1086、0.11 で y=1550 を実測した
# （カメラを上げると LookAt でキャラが見上げるので、単純な平行移動より大きく動く）。
# 画素から自動検出する方式は、口が閉じているときに背景の夕焼けを拾って外れる。
MOUTH_Y_AT_ZERO = 1086
MOUTH_Y_PER_OFFSET = 4218


def mouth_crop_top(camera_offset: float) -> int:
    return max(0, int(MOUTH_Y_AT_ZERO + MOUTH_Y_PER_OFFSET * camera_offset) - 150)


def measure(webm: str, seg: float, camera_offset: float) -> None:
    y0 = mouth_crop_top(camera_offset)
    areas = [_mouth_pixels(f) for f in _frames(webm, y0, 300)]
    print(f"\n口の検出領域: y={y0}〜{y0 + 300}  総フレーム={len(areas)}\n")
    print(f"{'ケース':<12}{'v':>7}{'a':>7}{'口面積':>9}   主要ブレンドシェイプ")
    print("-" * 78)
    for i, (v, a, name) in enumerate(CASES):
        # 表情の補間（_emotionLerpSpeed=2.0 で約0.5秒）が終わってから測る
        lo, hi = int((i * seg + 1.0) * 4), int(((i + 1) * seg) * 4)
        window = areas[lo:hi]
        if not window:
            continue
        w = blend_weights(v, a)
        top = ", ".join(f"{k}{val:.2f}"
                        for k, val in sorted(w.items(), key=lambda x: -x[1]) if val > 0.02)
        print(f"{name:<12}{v:+7.2f}{a:+7.2f}{sum(window) / len(window):9.0f}   {top}")


def main(argv=None):
    p = argparse.ArgumentParser(description="表情→口の開き具合の実測プローブ")
    p.add_argument("--seg", type=float, default=2.5, help="1ケースあたりの秒数")
    p.add_argument("--camera-offset", type=float, default=0.11,
                   help="朝版のカメラYオフセット（夜版と揃えるなら 0）")
    p.add_argument("--mouth-close", type=float, default=0.0,
                   help="無音時に表情の口成分を打ち消す強さ（0で無効）")
    p.add_argument("--webm", default="/tmp/check_face.webm", help="録画の出力先")
    p.add_argument("--reuse", default=None, help="録画済みwebmを測るだけ")
    args = p.parse_args(argv)

    if args.reuse:
        measure(args.reuse, args.seg, args.camera_offset)
        return 0

    print(f"[プローブ] {len(CASES)}ケース × {args.seg}秒 = {len(CASES) * args.seg:.0f}秒ぶんを録画します")
    record(args.webm, args.seg, args.camera_offset, args.mouth_close)
    measure(args.webm, args.seg, args.camera_offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
