#!/usr/bin/env python3
"""
botたん 朝の勘違いクイズ Shorts 自動投稿パイプライン

JST 6:00 に起動し、約30秒のクイズ動画を生成して YouTube に投稿する。

構成（尺は区間ごとに発話長で決まる。シンキングタイムだけ3秒固定）:
  Q      問題提示       問題文と選択肢A/Bの表示
  THINK  シンキング     カウントダウン音声 + ゲージ（3.000秒固定）
  A      正解発表       正解のハイライト
  EXPL   解説           解説テキスト
  AFF    全肯定コメント 豆知識について全肯定する
  END    エンディング   季節の挨拶 + 固定クロージング

環境変数（core.py のものに加えて）:
  QUIZ_CSV            : クイズCSVのパス (デフォルト: ./data/quiz.csv)
  QUIZ_USED_CSV       : 消費台帳のパス  (デフォルト: ./data/quiz_used.csv)
  QUIZ_COOLDOWN_DAYS  : 再利用までの日数 (デフォルト: 30)
  QUIZ_NO_CONSUME     : true で消費台帳に記録しない（テスト用）
  VRMA_PULLBACK       : シンキングタイム以降カメラを引く量[m] (既定 0.7)
  （生成モーションの調整値 VRMA_GAIN / VRMA_HIPS_Y などは core.py 側を参照）
  MORNING_CAMERA_OFFSET_Y : Unityカメラの上方向オフセット (デフォルト: 0.16)
  MORNING_MOUTH_CLOSE     : 無音時に表情の口成分を打ち消す強さ 0〜1 (デフォルト: 1.0)
  SKIP_YOUTUBE        : true で投稿をスキップ
  KEEP_TEMP           : true で一時ファイルを残す
  YOUTUBE_PRIVACY     : public/private/unlisted
"""

import os
import sys
import json
import time
import argparse
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import core
import quiz_data
import quiz_layout
from quiz_prompts import (
    QUIZ_SYSTEM_PROMPT, QUIZ_SCRIPT_SCHEMA,
    build_quiz_user_prompt, build_fallback_script, validate_script,
)

# シンキングタイムは仕様上ここだけ固定。
# COUNTDOWN_WORDS は1秒間隔に置かれるので、語数 = 秒数にすること
THINK_DURATION = 3.0

# カウントダウンの読み。数字表記だと読み違いが出るので仮名で指定する
COUNTDOWN_WORDS = ["さん", "にー", "いち"]

# 尺が長すぎたときに警告を出す閾値[秒]。目標は30秒。
# 無人実行なので生成は止めず、ログに残してプロンプト調整の材料にする
QUIZ_DURATION_WARN_SEC = 35.0

# パート間に入れる無音（秒）。THINK後の溜めを長めにして「正解は……」を引き立てる
PAD_AFTER = {"Q": 0.35, "THINK": 0.60, "A": 0.40, "EXPL": 0.30, "AFF": 0.30, "END": 0.0}

# AI生成モーションを敷くパート。Mixamoのトリガーが無いところだけを対象にする。
#   Q   → 既定ステートの Blow A Kiss が流れる（4.57秒）。残りもサムネを撮る
#         アップの画なので触らない（下の thumbnail.capture_frame を参照）
#   END → DoGreeting + DoWave
# THINK 開始から END 開始までを、1本の連続モーションで途切れさせずに埋める。
#
# 2026-08-12: AFF を対象に加えた。以前は AFF の頭で DoThankful（Mixamoの一礼）を
# 撃っていたが、生成モーションを止めてまで出すものではないので撃つのをやめた。
VRMA_PARTS = ("THINK", "A", "EXPL", "AFF")
# 生成モーションが始まる THINK からカメラを引く。引く量[m]
VRMA_PULLBACK = float(os.getenv("VRMA_PULLBACK", "0.7"))

# Unityカメラを鉛直に上げる量[m]。quiz_layout.PANEL_H と連動しているので
# 片方だけ変えないこと（実測 3647px/m、PANEL_H=470 → Δy≒0.11）
CAMERA_OFFSET_Y = float(os.getenv("MORNING_CAMERA_OFFSET_Y", "0.11"))
# 表情プリセット(Fcl_ALL_*)は口が開くモーフを含むため、発話していない間も口が開いたまま
# になる。朝版はシンキングタイムなど無音区間が長いので、Unity 側で表情の口成分だけを
# 打ち消す。眉と目の表情は残るので表情が抜けて見えることはない。
# 1.0 で口面積が 639〜3738px → 9〜63px になることを check_face.py で実測した。
# 夜版は引数自体を渡さないので影響を受けない。
MOUTH_CLOSE = float(os.getenv("MORNING_MOUTH_CLOSE", "1.0"))


# ──────────────────────────────────────────────
# Step 1: 台本生成
# ──────────────────────────────────────────────

def _normalize_sentences(sentences: list[dict]) -> list[dict]:
    """1つの text に複数文を詰め込んだ出力を、文ごとの要素にばらす。

    LLMは指示しても改行区切りで1要素にまとめてくることがある。そのまま
    VOICEVOX に渡すと改行入りのテキストを1回で合成することになるので、
    ここで分割しておく。valence/arousal は元の値を引き継ぐ。
    """
    out = []
    for s in sentences or []:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        for line in text.split("\n"):
            for part in core.split_sentences(line):
                part = part.strip()
                if part:
                    out.append({**s, "text": part})
    return out


def generate_quiz_script(quiz: dict) -> dict:
    """LLMでクイズ台本を生成する。全モデル失敗時はCSVだけのテンプレートに落とす。"""
    print("[LLM] クイズ台本生成中...")
    try:
        script = core.llm_json(
            QUIZ_SYSTEM_PROMPT,
            build_quiz_user_prompt(quiz),
            QUIZ_SCRIPT_SCHEMA,
        )
    except Exception as e:
        print(f"[LLM] 台本生成に失敗したためテンプレートにフォールバックします: {e}")
        return build_fallback_script(quiz)

    warnings = validate_script(script, quiz)
    for w in warnings:
        print(f"[LLM] 警告: {w}")
    # 構造が壊れている場合はフォールバック（数字の警告だけなら続行）
    if any("が空です" in w for w in warnings):
        print("[LLM] 台本の構造が不正なためテンプレートにフォールバックします")
        return build_fallback_script(quiz)

    for key in ("question_intro", "answer_reveal", "explanation", "affirmation"):
        script[key] = _normalize_sentences(script[key])

    return script


# ──────────────────────────────────────────────
# Step 2: 音声組み立てとタイムライン確定
# ──────────────────────────────────────────────

def build_countdown_wav(tmp_dir: Path, prefix: str) -> str:
    """ちょうど THINK_DURATION 秒のカウントダウン音声を作る。

    「ご・よん・さん・にー・いち」を1秒間隔に配置し、無限長の anullsrc に
    重ねてから -t で厳密に切る。amix の normalize=0 を外すと音量が
    入力数ぶんの1に減衰してしまうので必須。
    """
    digit_wavs = []
    for i, word in enumerate(COUNTDOWN_WORDS):
        p = str(tmp_dir / f"{prefix}_cd{i}.wav")
        core._synthesize(word, p, {
            "speedScale":      1.0,
            "pitchScale":      0.03,
            "intonationScale": 1.2,
            "volumeScale":     1.1,
        })
        digit_wavs.append(p)

    out = str(tmp_dir / f"{prefix}_countdown.wav")
    cmd = ["ffmpeg", "-y"]
    for p in digit_wavs:
        cmd += ["-i", p]

    n = len(digit_wavs)
    # 各数字を1秒間隔に配置してミックスし、apad で無限に無音を足してから
    # -t で厳密に切る。normalize=0 が無いと音量が入力数ぶんの1に減衰する。
    fc = ";".join(f"[{i}:a]adelay={i * 1000}[a{i}]" for i in range(n))
    fc += ";" + "".join(f"[a{i}]" for i in range(n))
    fc += f"amix=inputs={n}:duration=longest:normalize=0[mix];[mix]apad[out]"

    cmd += ["-filter_complex", fc, "-map", "[out]",
            "-t", f"{THINK_DURATION:.3f}",
            "-ar", str(core.WAV_RATE), "-ac", str(core.WAV_CHANNELS),
            "-c:a", "pcm_s16le", out]
    import subprocess
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for p in digit_wavs:
        Path(p).unlink(missing_ok=True)
    return out


# フック（掴みの一言）の声色を変えるパート・文数。
# 夜版は Thumbnail の一言まるごとだが、朝版の question_intro は
# 「掛け声／問題文／どっちだと思う？」の2〜3文なので、掛け声の1文目だけに当てる。
# 問題文まで speedScale 0.85 にすると間延びするうえ尺が伸びる。
# build_audio（合成）と build_subtitles（字幕の区切り）の両方がこれを見る
HOOK_PART      = "Q"
HOOK_SENTENCES = 1


def build_audio(script: dict, ending_sentences: list[dict],
                wav_path: str, tmp_dir: Path, prefix: str) -> list[dict]:
    """5パート分の音声を合成・結合し、各パートの開始/終了時刻を確定して返す。

    尺は「区間ごとに発話長に従う」方針なので、推定値ではなく
    get_wav_duration の実測値だけを積み上げる。
    """
    part_specs = [
        ("Q",     script["question_intro"]),
        ("THINK", None),
        ("A",     script["answer_reveal"]),
        ("EXPL",  script["explanation"]),
        ("AFF",   script["affirmation"]),
        ("END",   ending_sentences),
    ]

    segments = []
    all_wavs = []
    t = 0.0

    for pid, sentences in part_specs:
        if pid == "THINK":
            cd = build_countdown_wav(tmp_dir, prefix)
            actual = core.get_wav_duration(cd)
            wavs = [(cd, actual)]
            sentences = []
        elif pid == HOOK_PART and len(sentences) > HOOK_SENTENCES:
            # フックだけ声色を変える。prefix を分けないと連番 _000.wav が
            # 衝突して1文目が2文目に上書きされる
            wavs = (core.synthesize_sentences(sentences[:HOOK_SENTENCES], tmp_dir,
                                              f"{prefix}_{pid}hook", core.HOOK_VOICE_PARAMS)
                    + core.synthesize_sentences(sentences[HOOK_SENTENCES:], tmp_dir,
                                                f"{prefix}_{pid}"))
        else:
            wavs = core.synthesize_sentences(sentences, tmp_dir, f"{prefix}_{pid}")

        start = t
        # 文ごとの区間も残す。生成モーションを「その文が読まれている時刻」に
        # 置くために要る（文字数比だと漢字とかなで読み上げ速度が違うぶんズレる）
        spans = []
        for sent, (path, dur) in zip(sentences, wavs):
            spans.append({"start": round(t, 3), "end": round(t + dur, 3),
                          "motion": sent.get("motion")})
            all_wavs.append(path)
            t += dur
        if not sentences:                       # THINK（カウントダウン）
            for path, dur in wavs:
                all_wavs.append(path)
                t += dur
        segments.append({
            "id":        pid,
            "sentences": sentences,
            "spans":     spans,
            "start":     round(start, 3),
            "end":       round(t, 3),
            "duration":  round(t - start, 3),
        })

        pad = PAD_AFTER.get(pid, 0.0)
        if pad > 0:
            sil = core.make_silence_wav(tmp_dir / f"{prefix}_sil_{pid}.wav", pad)
            all_wavs.append(sil)
            t += pad

    core.concat_wavs(all_wavs, wav_path)

    actual_total = core.get_wav_duration(wav_path)
    print(f"[音声] 合計 {actual_total:.3f}秒 (計算値 {t:.3f}秒)")
    if actual_total > QUIZ_DURATION_WARN_SEC:
        print(f"[警告] 尺が長すぎます: {actual_total:.1f}秒 "
              f"(目標30秒 / 警告閾値{QUIZ_DURATION_WARN_SEC}秒)")
    for s in segments:
        print(f"  {s['id']:6s} {s['start']:6.2f}s 〜 {s['end']:6.2f}s ({s['duration']:5.2f}s)")

    if abs(actual_total - t) > 0.15:
        raise RuntimeError(f"タイムライン不整合: 計算{t:.3f}s vs 実測{actual_total:.3f}s")

    think = next(s for s in segments if s["id"] == "THINK")
    if abs(think["duration"] - THINK_DURATION) > 0.02:
        raise RuntimeError(
            f"シンキングタイムが{think['duration']:.3f}秒（{THINK_DURATION}秒固定のはず）")

    # 一時ファイルを掃除（結合済みなので個別WAVは不要）
    for p in all_wavs:
        Path(p).unlink(missing_ok=True)

    return segments


def build_subtitles(segments: list[dict], max_chars: int = 20) -> list[dict]:
    """パートごとに字幕タイミングを作る。

    パート単位で actual_duration を渡すので、パート境界でズレが累積しない。
    """
    subtitles = []
    for seg in segments:
        if not seg["sentences"]:
            continue
        # 声色を変えた文は境界で切って別々に計算する。
        # generate_subtitle_timing は「既定の速さで測ったモーラ時刻」を
        # actual_duration に線形スケールするので、パート内に速さの違う文が
        # 混ざっていると配分がズレる（フックだけ speedScale 0.85）。
        # 切らない場合は spans が seg["start"]/["duration"] と一致するので、
        # 従来とまったく同じ結果になる
        bounds = [0, len(seg["sentences"])]
        if seg["id"] == HOOK_PART and len(seg["sentences"]) > HOOK_SENTENCES:
            bounds.insert(1, HOOK_SENTENCES)

        subs = []
        for lo, hi in zip(bounds, bounds[1:]):
            spans = seg["spans"][lo:hi]
            text = "".join(s["text"] for s in seg["sentences"][lo:hi])
            subs += core.generate_subtitle_timing(
                text,
                time_offset=spans[0]["start"],
                actual_duration=round(spans[-1]["end"] - spans[0]["start"], 3),
                max_chars=max_chars,
                merge_short=True,   # 「萩、」「桔梗、」のような細切れを防ぐ
            )
        for s in subs:
            s["part"] = seg["id"]
        seg["subs"] = subs
        subtitles += subs
    return subtitles


# ──────────────────────────────────────────────
# Step 3: 表情タイムライン（ゆらぎ付き）
# ──────────────────────────────────────────────

# パートごとの表情の方向性。毎回同じ顔にならないよう複数用意して抽選する。
# VRM1FaceAnimation の内積配分:
#   Happy(1.0,-0.3) Relaxed(0.5,-0.8) Sad(-0.8,-0.5) Angry(-0.7,0.8) Surprised(0.3,1.0)
# arousal が高いほど Surprised/Happy が乗って口が開く（check_face.py で実測）。
# Q/A は発話中で口が動いているので高いままでよいが、長い EXPL/AFF/END は
# 落ち着かせて夜版と同じくらいの開き具合にする。
EMOTION_VARIANTS = {
    "Q":     [(0.70, 0.70), (0.50, 0.85), (0.85, 0.55)],   # わくわく問いかけ
    "THINK": [(0.50, 0.80), (0.30, 0.60), (0.60, 0.90)],   # そわそわ待つ
    "A":     [(0.90, 0.90), (0.75, 1.00), (1.00, 0.75)],   # びっくり嬉しい
    "EXPL":  [(0.65, -0.60), (0.55, -0.80), (0.75, -0.45)],  # 落ち着いた解説
    "AFF":   [(1.00, -0.30), (0.90, -0.10), (0.95, -0.50)],  # まっすぐな全肯定
    "END":   [(0.80, 0.15), (0.90, 0.00), (0.70, 0.25)],     # 明るい送り出し
}

JITTER = 0.12        # valence/arousal の微細ゆらぎ幅
TIME_JITTER = 0.25   # 表情が切り替わる時刻のゆらぎ幅（秒）


def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def build_emotions(segments: list[dict], rng) -> list[dict]:
    """パート開始時刻に表情を置く。毎回同じにならないようゆらぎを入れる。

    core.build_emotion_timeline は文字数比で按分するため、無音区間を挟む
    この構成では使えない（必ずズレる）。実尺ベースで自前に組む。
    """
    emotions = []
    for seg in segments:
        base_v, base_a = rng.choice(EMOTION_VARIANTS[seg["id"]])
        t = seg["start"] + rng.uniform(0.0, TIME_JITTER)
        emotions.append({
            "time":    round(max(0.0, t), 2),
            "valence": round(_clamp(base_v + rng.uniform(-JITTER, JITTER)), 2),
            "arousal": round(_clamp(base_a + rng.uniform(-JITTER, JITTER)), 2),
        })

        # 長いパートは中間キーフレームを足して単調さを避ける
        if seg["duration"] >= 8.0:
            for _ in range(rng.randint(1, 2)):
                mid = seg["start"] + rng.uniform(0.35, 0.85) * seg["duration"]
                emotions.append({
                    "time":    round(mid, 2),
                    "valence": round(_clamp(base_v + rng.uniform(-0.20, 0.20)), 2),
                    "arousal": round(_clamp(base_a + rng.uniform(-0.20, 0.20)), 2),
                })

    emotions.sort(key=lambda e: e["time"])
    print(f"[感情] {len(emotions)}件（ゆらぎあり）")
    return emotions


def build_vrma_blocks(segments: list[dict], script: dict) -> list[dict]:
    """THINK〜END直前を丸ごと埋める、連続モーションのブロックを組む。

    夜版と同じく**文ごとのモーションを、その文が読まれる時刻に置く**
    （core.plan_vrma_from_sentences）。パートの尺で按分していた旧実装は
    どの文に対応するかを一切見ていなかったので、ペルソナが最後の文のために
    書いた動きがパートの中盤で再生されることがあった。

    THINK だけは発話が無い（カウントダウン音のみ）ので、台本の motions.think を
    区間に等分して疑似的な文として扱う。
    """
    seg = {x["id"]: x for x in segments}
    if "THINK" not in seg or "END" not in seg:
        return []

    start = float(seg["THINK"]["start"])
    # 末尾の余白は DoGreeting（END.start+0.2）に食い込ませないため
    end = float(seg["END"]["start"]) - core.VRMA_TAIL_PAD
    if end - start < core.VRMA_SEG_MIN_SEC:
        print(f"[モーション] ブロックの尺が足りない({end - start:.1f}秒)のでスキップ")
        return []

    spans = []
    think_motions = [t.strip() for t in ((script.get("motions") or {}).get("think") or [])
                     if (t or "").strip()]
    th = seg["THINK"]
    if think_motions:
        step = (th["end"] - th["start"]) / len(think_motions)
        for i, text in enumerate(think_motions):
            spans.append({"start": th["start"] + step * i,
                          "end":   th["start"] + step * (i + 1),
                          "motion": text})
    for pid in VRMA_PARTS:
        if pid == "THINK":
            continue
        if pid in seg:
            spans += seg[pid].get("spans") or []

    spans.sort(key=lambda sp: sp["start"])
    if not spans:
        print("[モーション] 埋められる区間がありません")
        return []

    # パート間のパディング（PAD_AFTER）を前の文の区間に含めて隙間を無くす。
    # 埋めないと生成モーションが窓より短くなり、そのぶん棒立ちに戻る
    for i, sp in enumerate(spans):
        sp["end"] = spans[i + 1]["start"] if i + 1 < len(spans) else max(sp["end"], end)

    seg_list = core.plan_vrma_from_sentences(spans, start, end)
    if not seg_list:
        print("[モーション] 埋められる区間がありません")
        return []

    n_auth = sum(1 for sp in spans if (sp.get("motion") or "").strip()
                 and sp["end"] > start and sp["start"] < end)
    print(f"[モーション] {start:.1f}〜{end:.1f}秒 → {len(seg_list)}セグメント "
          f"（モーション指定のある文 {n_auth}/{len(spans)}）")
    return [{"name": "body", "time": round(start, 2), "segments": seg_list}]


def build_emotion_file(segments: list[dict], emotions: list[dict], path: str,
                       vrma_motions: list[dict] = None) -> None:
    """Unityへ渡すJSON。

    greetingTime2 は Unity 側に実装済みだが Python が書き出していなかった
    デッドコード。ここで埋めることで Unity 無改修のままエンディングの
    挨拶モーションが発火する。

    greetingTime1 は書き出さない。greetingTime2 と同じ DoGreeting を撃つ重複であり、
    Q.start+0.3 という早さで発火するため Animator の既定ステート
    Blow A Kiss（4.57秒）を0.3秒で断ち切っていた。省くと投げキッスが最後まで流れる。

    thankfulTime も書き出さない。AFF の頭で Mixamo の一礼を撃っていたが、
    そのぶん生成モーションを AFF で止める必要があり、話に合った動きより優先する
    ものではなかった。キーが無ければ VRM1LipSync は DoThankful を撃たない no-op。
    """
    seg = {s["id"]: s for s in segments}

    wave_time = 0.0
    end_subs = seg["END"].get("subs") or []
    wave_time = core._find_subtitle_time(end_subs, "行ってらっしゃい",
                                         start_from=seg["END"]["start"])
    if wave_time is None:
        wave_time = max(0.0, seg["END"]["end"] - 1.5)

    data = {
        "emotions":      emotions,
        # 0だと VRM1LipSync が発火しないので必ず正の値にする
        "greetingTime2": round(seg["END"]["start"] + 0.2, 2),
        "waveTime":      round(wave_time, 2),
    }

    # AI生成モーション。既存のMixamoトリガーとは独立していて干渉しない
    if vrma_motions:
        data["vrmaMotions"] = vrma_motions
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"[感情] 保存: {path} "
          f"(greeting2={data['greetingTime2']}s wave={data['waveTime']}s)")
    for m in data.get("vrmaMotions", []):
        print(f"[モーション] 生成: {m['file']} @{m['time']}s")


# ──────────────────────────────────────────────
# main
# ──────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="朝のクイズShorts生成パイプライン")
    p.add_argument("--quiz-id", type=int, default=None,
                   help="使うクイズのidを固定する（テスト用）")
    p.add_argument("--dry-run", action="store_true",
                   help="台本生成までで止めてJSONを表示する")
    p.add_argument("--stage", choices=["all", "script", "voice", "ffmpeg"], default="all",
                   help="どこまで実行するか")
    p.add_argument("--preview", action="store_true",
                   help="Unity録画の代わりに単色背景を使う（レイアウト確認用・高速）")
    p.add_argument("--webm", default="", help="--stage ffmpeg で使う既存のwebm")
    p.add_argument("--timeline", default="", help="--stage ffmpeg で使う既存のtimeline.json")
    p.add_argument("--keep-temp", action="store_true", help="一時ファイルを残す")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    keep_temp = args.keep_temp or core.env_flag("KEEP_TEMP")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())
    prefix = f"quiz_{ts}"

    wav_path       = str(tmp_dir / f"{prefix}.wav")
    webm_path      = str(tmp_dir / f"{prefix}.webm")
    mp4_path       = str(tmp_dir / f"{prefix}.mp4")
    emotion_path   = str(tmp_dir / f"{prefix}_emotions.json")
    timeline_path  = str(tmp_dir / f"{prefix}_timeline.json")
    frame_path     = str(tmp_dir / f"{prefix}_frame.png")
    thumbnail_path = str(tmp_dir / f"{prefix}_thumbnail.png")

    core.cleanup_old_temp_files()

    total_start = time.time()
    cleanup_targets = []

    try:
        # ── ffmpegステージだけ再実行する場合は保存済みタイムラインを読む
        if args.stage == "ffmpeg" and args.timeline:
            saved = json.loads(Path(args.timeline).read_text(encoding="utf-8"))
            quiz      = saved["quiz"]
            segments  = saved["segments"]
            subtitles = saved["subtitles"]
            source_webm = args.webm or saved.get("webm", "")
            _render(quiz, segments, subtitles, source_webm, mp4_path,
                    preview=args.preview or not source_webm)
            print(f"\n✅ 再合成完了: {mp4_path}  ({time.time()-total_start:.1f}秒)")
            return 0

        # ── Step 1: クイズ選択
        quiz = core._timed("Step1 クイズ選択", quiz_data.next_quiz, quiz_id=args.quiz_id)
        print(f"[クイズ] id={quiz['id']} 「{quiz['問題']}」 正解={quiz['正解']}")

        # ── ARDYサーバーを先に起動しておく。
        # モデル読み込みに4〜5分かかるので、台本生成と音声合成の裏でロードさせる
        ardy_proc = None
        if core.VRMA_MOTION_DIR and not args.preview:
            ardy_proc = core.ardy_start()

        # ── Step 2: 台本生成
        script = core._timed("Step2 台本生成", generate_quiz_script, quiz)
        ending = quiz_data.build_ending_sentences()
        script["ending"] = ending

        if args.dry_run or args.stage == "script":
            print(json.dumps({"quiz": quiz, "script": script}, ensure_ascii=False, indent=2))
            return 0

        # ── Step 3: 音声とタイムライン
        segments = core._timed("Step3 音声生成", build_audio,
                               script, ending, wav_path, tmp_dir, prefix)
        cleanup_targets.append(wav_path)

        subtitles = core._timed("Step3.5 字幕生成", build_subtitles, segments)

        # 再実行用にタイムラインを保存
        Path(timeline_path).write_text(json.dumps({
            "quiz": quiz, "script": script, "segments": segments,
            "subtitles": subtitles, "webm": webm_path,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[タイムライン] 保存: {timeline_path}")

        if args.stage == "voice":
            print(f"\n✅ 音声生成まで完了: {wav_path}")
            print(f"   タイムライン: {timeline_path}")
            cleanup_targets.clear()   # 検証用に残す
            return 0

        # ── Step 3.8: AI生成モーション
        # 失敗しても動画は作る。生成モーションのためにその日の投稿を落とさない
        vrma_motions = []
        if core.VRMA_MOTION_DIR and not args.preview:
            try:
                blocks = build_vrma_blocks(segments, script)
                if blocks and core.ardy_wait_ready():
                    vrma_motions = core._timed(
                        "Step3.8 モーション生成", core.build_vrma_motions,
                        blocks, core.VRMA_MOTION_DIR,
                        f"{datetime.now().strftime('%Y-%m-%d')}-{quiz['id']}")
            except Exception as e:
                print(f"[モーション] 生成に失敗しました（動画は続行）: {e}")
            finally:
                core.ardy_stop(ardy_proc)
                ardy_proc = None

        # ── Step 4: 表情タイムライン
        import random
        rng = random.Random(f"{datetime.now().strftime('%Y-%m-%d')}-{quiz['id']}")
        emotions = build_emotions(segments, rng)
        build_emotion_file(segments, emotions, emotion_path, vrma_motions)
        cleanup_targets.append(emotion_path)

        # ── Step 5: Unity録画
        if args.preview:
            print("[Unity] --preview のためスキップ")
            source_webm = ""
        else:
            extra = ["-cameraOffsetY", f"{CAMERA_OFFSET_Y}",
                     "-mouthCloseOnSilence", f"{MOUTH_CLOSE}"]
            # 生成モーションがあるときだけ、シンキングタイムからカメラを引く。
            # フック(Q)はアップのまま＝サムネもアップで撮れる
            if vrma_motions and VRMA_PULLBACK > 0:
                think_start = next(s["start"] for s in segments if s["id"] == "THINK")
                extra += ["-cameraPullbackAt", f"{think_start:.2f}",
                          "-cameraPullbackZ", f"{VRMA_PULLBACK}"] + core.vrma_unity_args()
            core._retry("Step5 Unity録画", core.record_with_unity,
                        wav_path, webm_path, emotion_path,
                        extra_args=extra,
                        catch=(RuntimeError, TimeoutError), delay=15)
            source_webm = webm_path
            cleanup_targets.append(webm_path)

        # ── Step 6: 合成
        core._timed("Step6 MP4合成", _render,
                    quiz, segments, subtitles, source_webm, mp4_path, args.preview)

        # ── Step 7: サムネイル
        thumb_ok = False
        if source_webm:
            try:
                import thumbnail
                # フック(Q)の終盤から撮る。「AとB、どっちだと思う？」の高揚した表情が出る位置。
                # THINK以降はカメラが引くので、アップの画で撮れるのはここまで
                q = next(s for s in segments if s["id"] == "Q")
                thumbnail.capture_frame(source_webm, frame_path,
                                        max(q["start"] + 0.5, q["end"] - 0.5))
                thumbnail.generate_quiz_thumbnail(frame_path, thumbnail_path, quiz["問題"])
                thumb_ok = True
                cleanup_targets.append(frame_path)
            except Exception as e:
                print(f"[サムネイル] 生成に失敗しました（投稿は続行）: {e}")

        # ── Step 8: 投稿
        if core.env_flag("SKIP_YOUTUBE") or args.preview:
            print("[YouTube] スキップ")
        else:
            _upload(quiz, script, mp4_path, thumbnail_path if thumb_ok else "")

        print(f"\n✅ パイプライン完了: {mp4_path}  (合計: {time.time()-total_start:.1f}秒)")
        return 0

    except Exception as e:
        print(f"\n❌ エラー発生: {e}")
        raise

    finally:
        if not keep_temp:
            for p in cleanup_targets:
                if p and Path(p).exists():
                    Path(p).unlink()
                    print(f"[Cleanup] 削除: {p}")


def _render(quiz, segments, subtitles, source_webm, mp4_path, preview=False):
    """クイズUIを合成してMP4を出力する"""
    seg = {s["id"]: s for s in segments}
    total = max(s["end"] for s in segments) + 1.0

    vf_parts = quiz_layout.build_quiz_filters(quiz, seg, subtitles)

    if preview or not source_webm:
        quiz_layout.render_preview(vf_parts, mp4_path, duration=total)
    else:
        vf_parts = core.base_vf_parts() + vf_parts
        core.run_ffmpeg_finalize(source_webm, mp4_path, vf_parts, timeout=300)


def _upload(quiz, script, mp4_path, thumbnail_path):
    from youtube import upload_to_youtube, save_youtube_upload_to_db, notify_discord
    from description import build_quiz_title, build_quiz_description

    title = build_quiz_title(script.get("title_hook") or quiz["問題"])
    description = build_quiz_description(quiz)

    yt_url = core._timed("Step8 YT投稿", upload_to_youtube,
                         mp4_path, title, description, thumbnail_path)
    if yt_url:
        if os.getenv("YOUTUBE_PRIVACY", "public") == "public":
            save_youtube_upload_to_db(yt_url, title, [
                {"corner_name": "QuizCorner", "quiz_id": quiz["id"],
                 "theme": quiz.get("カテゴリ", "")},
            ])
            notify_discord(yt_url, title)
        if core.env_flag("QUIZ_NO_CONSUME"):
            print("[クイズ] QUIZ_NO_CONSUME=true のため消費を記録しません")
        else:
            quiz_data.mark_used(quiz["id"], yt_url)


if __name__ == "__main__":
    sys.exit(main())
