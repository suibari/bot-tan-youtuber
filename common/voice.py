"""VOICEVOX による音声合成。

Shorts の収録とライブ配信で共有する。以前は shorts/core.py と live/voice.py に
同じ実装が2つあった。

結合と無音生成は wave モジュールで行う（統合前のライブ配信版）。VOICEVOX の出力は
常に同じフォーマット（24kHz/mono/pcm_s16le）なのでフレームを繋ぐだけでよく、
発話のたびに ffmpeg のプロセスを起こす必要がない。
"""

import json
import os
import tempfile
import wave
from pathlib import Path

import requests

from common.env import env_int

VOICEVOX_URL     = os.getenv("VOICEVOX_URL", "http://localhost:10101")
VOICEVOX_SPEAKER = env_int("VOICEVOX_SPEAKER", 8)   # VOICEVOX: 春日部つむぎ ノーマル

# VOICEVOX の出力フォーマット。無音WAVを作って結合するため一致させること
WAV_RATE     = 24000
WAV_CHANNELS = 1

# 掴みの一言（動画冒頭のフック / 配信オープニングの第一声）だけに使う合成パラメータ。
# ゆっくり・抑揚強め・大きめ・少し高めにして、本編の語りと声色を変える。
# 話者(VOICEVOX_SPEAKER)は変えない。同一キャラなので声そのものは共通。
HOOK_VOICE_PARAMS = {
    "speedScale":      0.85,
    "intonationScale": 1.4,
    "volumeScale":     1.3,
    "pitchScale":      0.05,
}

# (接続, 読み取り) 秒。統合前の Shorts 側は無制限だったが、エンジンが固まったときに
# パイプラインごと止まるのを避けるため、ライブ配信側の値に揃えた。
# 1文あたりの合成は実測1秒前後なので 30秒は十分に余裕がある。
_TIMEOUT = (5, 30)


class VoicevoxError(RuntimeError):
    pass


def health_check() -> str:
    """エンジンが応答するか確かめ、話者名を返す。

    既定ポート 10101 は AivisSpeech Engine のものでもあるので、
    実際にどちらが動いているかを起動時に確認できるようにしてある。
    """
    try:
        res = requests.get(f"{VOICEVOX_URL}/speakers", timeout=_TIMEOUT)
        res.raise_for_status()
    except Exception as e:
        raise VoicevoxError(f"{VOICEVOX_URL} に接続できません: {e}") from e

    for speaker in res.json():
        for style in speaker.get("styles", []):
            if style.get("id") == VOICEVOX_SPEAKER:
                return f"{speaker.get('name')} / {style.get('name')}"
    raise VoicevoxError(f"話者ID {VOICEVOX_SPEAKER} がエンジンに存在しません")


def get_wav_duration(wav_path) -> float:
    """WAVファイルの長さを秒で返す"""
    with wave.open(str(wav_path), "r") as f:
        return f.getnframes() / float(f.getframerate())


def audio_query(text: str) -> dict:
    res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": VOICEVOX_SPEAKER},
        timeout=_TIMEOUT,
    )
    res.raise_for_status()
    return res.json()


def synthesize(text: str, output_path, extra_params: dict = None) -> None:
    """テキストを1本のWAVに合成する。"""
    query = audio_query(text)
    if extra_params:
        query.update(extra_params)

    synth_res = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": VOICEVOX_SPEAKER},
        headers={"Content-Type": "application/json"},
        data=json.dumps(query),
        timeout=_TIMEOUT,
    )
    synth_res.raise_for_status()
    Path(output_path).write_bytes(synth_res.content)


def concat_wavs(paths: list, output_path) -> None:
    """同一フォーマットのWAVを1本に結合する（`-c copy` 相当）。"""
    if not paths:
        raise ValueError("結合するWAVがありません")

    with wave.open(str(output_path), "wb") as out:
        params = None
        for p in paths:
            with wave.open(str(p), "rb") as w:
                if params is None:
                    params = w.getparams()
                    out.setparams(params)
                elif (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (
                    params.nchannels, params.sampwidth, params.framerate
                ):
                    # フォーマットが混ざると再生が壊れる。黙って壊すより落とす
                    raise VoicevoxError(f"WAVのフォーマットが揃っていません: {p}")
                out.writeframes(w.readframes(w.getnframes()))


def make_silence_wav(output_path, duration: float) -> str:
    """VOICEVOX と同一フォーマットの無音WAVを作る。文間・パート間の余白に使う。"""
    frames = int(WAV_RATE * duration)
    with wave.open(str(output_path), "wb") as out:
        out.setnchannels(WAV_CHANNELS)
        out.setsampwidth(2)          # pcm_s16le
        out.setframerate(WAV_RATE)
        out.writeframes(b"\x00\x00" * frames * WAV_CHANNELS)
    return str(output_path)


def valence_arousal_to_voicevox_params(valence: float, arousal: float) -> dict:
    """valence/arousalをVOICEVOXの音声パラメータに変換する。
    speedScaleは一定に保つ（YouTube視聴体験のため速さの変動を避ける）。

    NOTE: 本番では未使用（聞き取りやすさのため発話の感情ぶれは無効化されている）。
    """
    return {
        "pitchScale":      round(arousal  * 0.08, 3),      # 興奮→高め
        "intonationScale": round(1.0 + valence * 0.3, 3),  # ポジティブ→抑揚強め
        "volumeScale":     round(1.0 + arousal * 0.1, 3),  # 興奮→大きめ
    }


def query_mora_times(text: str) -> tuple:
    """audio_queryからモーラタイミングリストと総尺(秒)を返す"""
    query = audio_query(text)

    t = float(query.get("prePhonemeLength", 0.1))
    mora_times = []
    for phrase in query["accent_phrases"]:
        for mora in phrase["moras"]:
            dur = (mora.get("consonant_length") or 0) + (mora.get("vowel_length") or 0)
            mora_times.append({"start": t, "duration": dur})
            t += dur
        if phrase.get("pause_mora"):
            p = phrase["pause_mora"]
            t += (p.get("consonant_length") or 0) + (p.get("vowel_length") or 0)
    total = t + float(query.get("postPhonemeLength", 0.1))
    return mora_times, total


# ── Shorts 収録用 ─────────────────────────────────────

def synthesize_sentences(sentences: list, out_dir, prefix: str,
                         extra_params: dict = None) -> list:
    """文ごとに合成し [(wav_path, duration), ...] を返す。

    duration は get_wav_duration による実測値。呼び出し側はこれを積み上げて
    パート開始時刻を確定する（推定値を使わない）。
    """
    out_dir = Path(out_dir)
    results = []
    for i, sentence in enumerate(sentences):
        path = str(out_dir / f"{prefix}_{i:03d}.wav")
        synthesize(sentence["text"], path, extra_params)
        results.append((path, get_wav_duration(path)))
    return results


def generate_voice(sentences: list, output_path: str, intro_text: str = "") -> list:
    """VOICEVOXで文ごとに音声合成し結合する。intro_textがある場合は冒頭一言を先頭に付ける。
    sentences: [{"text": str, "valence": float, "arousal": float}, ...]
    戻り値: 各文の実測尺[秒]（intro は含まない）。

    生成モーションを文に紐づけるのに使う。文字数比で割り当てると、漢字とかなで
    読み上げ速度が違うぶんズレて、話している内容と動きが合わなくなる
    """
    print(f"[VOICEVOX] 文ごと音声生成中... (speaker: {VOICEVOX_SPEAKER})")
    tmp_dir = Path(tempfile.gettempdir())
    part_paths = []
    intro_wav = output_path.replace(".wav", "_intro.wav")

    try:
        if intro_text:
            synthesize(intro_text, intro_wav, HOOK_VOICE_PARAMS)
            part_paths.append(intro_wav)

        durations = []
        for i, sentence in enumerate(sentences):
            part_path = str(tmp_dir / f"{Path(output_path).stem}_part{i:03d}.wav")
            synthesize(sentence["text"], part_path)
            part_paths.append(part_path)
            durations.append(get_wav_duration(part_path))

        concat_wavs(part_paths, output_path)
        print(f"[VOICEVOX] 音声生成完了: {output_path} ({len(sentences)}文)")
        return durations

    finally:
        for p in part_paths:
            if p != intro_wav:  # intro_wavは呼び出し元でcleanup
                if Path(p).exists():
                    Path(p).unlink()


# ── ライブ配信用 ──────────────────────────────────────

def synthesize_lines(lines: list, out_dir, prefix: str,
                     gap_sec: float = 0.25,
                     hook: bool = False,
                     tail_gap: float = 0.0) -> tuple:
    """文のリストを合成し、(結合WAVのパス, 各文の実測尺[秒]) を返す。

    lines は [{"ja": ..., "en": ...}, ...]。読み上げるのは ja だけで、
    en は字幕にしか使わない。

    尺は get_wav_duration による実測値。文字数比で割り当てると、漢字とかなで
    読み上げ速度が違うぶんズレて、字幕と音声が合わなくなる。

    gap_sec は文と文のあいだに挟む無音。返す尺にはこの無音を含めるので、
    呼び出し側は尺を積み上げるだけで字幕の切り替え時刻が出る。

    tail_gap は末尾に足す無音。配信では1文ずつ別々に合成して Unity のキューへ
    流し込むので、文と文の間はこちらで作らないと詰まって聞こえる。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: list = []
    durations: list = []
    silence = None
    if gap_sec > 0:
        silence = make_silence_wav(out_dir / f"{prefix}_gap.wav", gap_sec)

    for i, line in enumerate(lines):
        text = (line.get("ja") or "").strip()
        if not text:
            continue
        part = out_dir / f"{prefix}_{i:03d}.wav"
        # 掴みの声色は第一声にだけ当てる
        synthesize(text, part, HOOK_VOICE_PARAMS if (hook and i == 0) else None)
        dur = get_wav_duration(part)
        parts.append(str(part))

        if silence and i < len(lines) - 1:
            parts.append(silence)
            dur += gap_sec
        durations.append(dur)

    if not parts:
        raise VoicevoxError("合成できる文がありません")

    if tail_gap > 0:
        parts.append(make_silence_wav(out_dir / f"{prefix}_tail.wav", tail_gap))
        durations[-1] += tail_gap

    combined = out_dir / f"{prefix}.wav"
    concat_wavs(parts, combined)
    return str(combined), durations
