"""Irodori-TTS による音声合成（失敗時は VOICEVOX）。

Shorts の収録とライブ配信で共有する。以前は shorts/core.py と live/voice.py に
同じ実装が2つあった。

結合と無音生成は wave モジュールで行う（統合前のライブ配信版）。VOICEVOX の出力は
常に同じフォーマット（24kHz/mono/pcm_s16le）なのでフレームを繋ぐだけでよく、
発話のたびに ffmpeg のプロセスを起こす必要がない。
"""

import io
import json
import os
import struct
import tempfile
import time
import wave
from contextvars import ContextVar

from common import gpu_recovery
from pathlib import Path

import requests

from common.env import env_float, env_int, host_pressure
from common.pronunciation import apply_pronunciations, preload_pronunciations  # noqa: F401

VOICEVOX_URL     = os.getenv("VOICEVOX_URL", "http://localhost:10101")
VOICEVOX_SPEAKER = env_int("VOICEVOX_SPEAKER", 8)   # VOICEVOX: 春日部つむぎ ノーマル

# VOICEVOX はフォールバックと字幕のモーラ時刻に使う。
TTS_ENGINE = os.getenv("TTS_ENGINE", "irodori").lower()
if TTS_ENGINE not in {"irodori", "voicevox"}:
    raise ValueError(f"未対応の TTS_ENGINE: {TTS_ENGINE}")
IRODORI_URL = os.getenv("IRODORI_URL", "http://localhost:10110").rstrip("/")
IRODORI_VOICE = os.getenv("IRODORI_VOICE", "tsumugi")
IRODORI_HOOK_VOICE = os.getenv("IRODORI_HOOK_VOICE", IRODORI_VOICE)
# 聞き取りやすさを優先し、標準速度で合成する。
IRODORI_SPEED = env_float("IRODORI_SPEED", 1.0)
_deadline = ContextVar("irodori_deadline", default=None)

IRODORI_TIMEOUT = (env_float("IRODORI_CONNECT_TIMEOUT_SEC", 3),
                   env_float("IRODORI_READ_TIMEOUT_SEC", 30))

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

# (接続, 読み取り) 秒と、追加で何回まで試すか。用途によって欲しい値が逆になる。
#
# 既定は**配信向けの短い値**にしてある。配信は live/live.py が合成の例外を握って
# その発話だけ打ち切る（配信自体は続く）ので、待たされた分そのまま放送が沈黙する。
# 早く諦めて次の発話へ行くほうがましなので、待たない・再試行は1回だけ。
#
# 録画（Shorts）は逆で、待てる。例外が上がるとパイプラインごと落ちて動画が出ない
# ため、run.sh / run_quiz.sh が VOICEVOX_READ_TIMEOUT_SEC と VOICEVOX_RETRY を
# 伸ばす。LLM_TIMEOUT_SEC を録画側だけ伸ばしているのと同じ切り分け。
#
# 以前は固定の (5, 30) で「1文あたりの合成は実測1秒前後なので30秒は十分に余裕が
# ある」と書いていたが、これは VOICEVOX が GPU だった頃の前提だった。8/29 の GPU
# 交換で CPU 版へ移ったあと、ARDY のモデルロードと重なった1文が30秒を超え、朝版が
# 全滅した（2026-08-31）。CPU 版は同居する負荷次第で1文に数十秒かかりうる。
_TIMEOUT = (env_float("VOICEVOX_CONNECT_TIMEOUT_SEC", 5),
            env_float("VOICEVOX_READ_TIMEOUT_SEC", 15))
_RETRY = env_int("VOICEVOX_RETRY", 1)

# 合成が「遅い」とみなす秒数。読み取りタイムアウトの 1/3（既定5秒、録画時は40秒）。
# 落ちる手前の状態を拾いたいだけなので、これを下回るときは何も出さない。
_SLOW_SEC = _TIMEOUT[1] / 3


class VoicevoxError(RuntimeError):
    pass


def _post_with_retry(url: str, what: str, **kwargs):
    """VOICEVOX へ POST する。一時的な不調のときだけ再試行する。

    再試行するのはタイムアウト・接続断・5xx だけ。4xx は渡したテキストや話者IDが
    原因で、投げ直しても通らないので即座に諦める。

    5xx を拾うのは、GPU 版が推論に失敗して /synthesis が全件 500 を返した実績が
    あるため（2026-08-30）。エンジンの再起動中に当たった場合もここで回復する。

    なお話者IDが存在しない場合もエンジンは 4xx ではなく 500 を返すので、その手の
    設定ミスは再試行を使い切ってから落ちる。話者は固定なので実害はない。
    """
    detail = None
    delay = 1.0
    for attempt in range(_RETRY + 1):
        started = time.monotonic()
        try:
            res = requests.post(url, timeout=_TIMEOUT, **kwargs)
            if res.status_code < 500:
                res.raise_for_status()      # 4xx はここで上げて終わり
                elapsed = time.monotonic() - started
                if elapsed >= _SLOW_SEC:
                    # 落ちてはいないが落ちる手前。次に全滅するときの前触れなので
                    # 拾っておく。閾値未満は無言（毎回出すと配信ログが埋まる）
                    pressure = host_pressure()
                    print(f"[VOICEVOX] {what} に {elapsed:.1f}秒かかりました"
                          + (f"（{pressure}）" if pressure else ""))
                return res
            reason = f"HTTP {res.status_code}"
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            reason = type(e).__name__
        # 後からログで追えるように必ず残す。8/31 の朝版の全滅は何回試して駄目
        # だったのかが分からず切り分けに時間がかかり、9/1 の配信では待った秒数と
        # そのときのホストの状態が分からず、エンジンが遅いのか I/O が詰まった
        # のかを journal と sar を突き合わせて後から推定する羽目になった
        elapsed = time.monotonic() - started
        pressure = host_pressure()
        detail = f"{reason} / {elapsed:.1f}秒" + (f" / {pressure}" if pressure else "")
        if attempt == _RETRY:
            break
        print(f"[VOICEVOX] {what} が返りません（{detail}）。"
              f"{delay:.0f}秒待って再試行します（{attempt + 1}/{_RETRY}）")
        time.sleep(delay)
        delay *= 2
    # このメッセージは live/live.py がそのまま Discord へ流す。配信中に通知を
    # 見た時点で I/O 起因かどうかが分かるように、詳細ごと載せる
    raise VoicevoxError(f"{what} が{_RETRY + 1}回とも失敗しました（{detail}）")


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
                fallback = f"{speaker.get('name')} / {style.get('name')}"
                if TTS_ENGINE == "irodori":
                    try:
                        response = requests.get(f"{IRODORI_URL}/health", timeout=IRODORI_TIMEOUT)
                        response.raise_for_status()
                        info = response.json()
                        if IRODORI_VOICE not in info.get("voices", []):
                            raise ValueError(f"参照音声がありません: {IRODORI_VOICE}")
                        return f"Irodori / {IRODORI_VOICE} (loaded={info.get('loaded')}; fallback={fallback})"
                    except (requests.RequestException, ValueError) as error:
                        print(f"[TTS] Irodori の確認失敗。VOICEVOX で継続: {error}")
                return fallback
    raise VoicevoxError(f"話者ID {VOICEVOX_SPEAKER} がエンジンに存在しません")


def warmup(*, require_irodori: bool = False, recovery_timeout: float = 30) -> float:
    """実際の合成まで通し、Irodoriのモデルロードも待つ。

    /speakers だけでは音声モデルや推論経路に触れない。配信準備中に ARDY・
    Unity を読んだ後、CPU 版 VOICEVOX が swap へ追い出され、21:00 の
    第一声でだけ HDD から大量に swap-in した実績がある。そのため、長い
    準備の後に短い文を1本合成する。戻り値は所要秒。
    """
    output = Path(tempfile.gettempdir()) / f"voicevox_warmup_{os.getpid()}.wav"
    started = time.monotonic()
    try:
        engine = synthesize("今日もよろしくね。", output, wait_load=True,
                            recovery_timeout=recovery_timeout)
        if require_irodori and engine != "irodori":
            raise RuntimeError("Irodori の実合成を確認できませんでした")
        elapsed = time.monotonic() - started
        print(f"[TTS] 実合成のウォームアップ完了: engine={engine} {elapsed:.1f}秒")
        return elapsed
    finally:
        output.unlink(missing_ok=True)


def get_wav_duration(wav_path) -> float:
    """WAVファイルの長さを秒で返す"""
    with wave.open(str(wav_path), "r") as f:
        return f.getnframes() / float(f.getframerate())


def audio_query(text: str) -> dict:
    spoken_text = apply_pronunciations(text)
    res = _post_with_retry(
        f"{VOICEVOX_URL}/audio_query", "audio_query",
        params={"text": spoken_text, "speaker": VOICEVOX_SPEAKER},
    )
    return res.json()


def _synthesize_voicevox(text: str, output_path, extra_params: dict = None) -> None:
    """テキストを1本のWAVに合成する。"""
    query = audio_query(text)
    if extra_params:
        query.update(extra_params)

    synth_res = _post_with_retry(
        f"{VOICEVOX_URL}/synthesis", "synthesis",
        params={"speaker": VOICEVOX_SPEAKER},
        headers={"Content-Type": "application/json"},
        data=json.dumps(query),
    )
    Path(output_path).write_bytes(synth_res.content)


def _irodori_wav(text: str, extra_params: dict, wait_load: bool) -> bytes:
    """読み補正後に300字以内へ分割し、同形式のPCMを結合する。"""
    spoken = apply_pronunciations(text)
    if not spoken.strip():
        raise ValueError("合成するテキストが空です")
    chunks = []
    while spoken:
        end = min(300, len(spoken))
        if len(spoken) > 300:
            boundary = max(spoken.rfind(mark, 0, 300) for mark in "。！？、\n")
            if boundary >= 0:
                end = boundary + 1
        chunks.append(spoken[:end])
        spoken = spoken[end:]
    frames = bytearray()
    for chunk in chunks:
        response = requests.post(
            f"{IRODORI_URL}/synthesize", timeout=_request_timeout(),
            json={"text": chunk,
                  "voice": IRODORI_HOOK_VOICE if extra_params == HOOK_VOICE_PARAMS else IRODORI_VOICE,
                  "speed": IRODORI_SPEED * extra_params.get("speedScale", 1.0),
                  "wait_load": wait_load},
        )
        response.raise_for_status()  # 503も再試行せず、VOICEVOXへ
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            format_ = (wav.getframerate(), wav.getnchannels(),
                       wav.getsampwidth(), wav.getcomptype())
            if format_ != (WAV_RATE, WAV_CHANNELS, 2, "NONE"):
                raise ValueError("Irodori の WAV 形式が不正です")
            pcm = wav.readframes(wav.getnframes())
            if not pcm or len(pcm) != wav.getnframes() * WAV_CHANNELS * 2:
                raise ValueError("Irodori の WAV が空または途切れています")
            frames.extend(pcm)
    volume = extra_params.get("volumeScale", 1.0)
    if volume != 1.0:
        frames = b"".join(struct.pack("<h", max(-32768, min(32767, round(sample * volume))))
                          for (sample,) in struct.iter_unpack("<h", frames))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((WAV_CHANNELS, 2, WAV_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(frames)
    return output.getvalue()


def _request_timeout():
    deadline = _deadline.get()
    if deadline is None:
        return IRODORI_TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout("Irodori の復旧期限を超えました")
    from urllib3.util import Timeout
    return Timeout(total=remaining, connect=min(IRODORI_TIMEOUT[0], remaining),
                   read=remaining)


def _failure_detail(error):
    response = getattr(error, "response", None)
    return response.text if response is not None else str(error)


def synthesize(text: str, output_path, extra_params: dict = None,
               *, wait_load: bool = True, recovery_timeout: float = 30) -> str:
    """Irodoriを優先。Liveは復旧を最大30秒待ち、その文全体だけフォールバックする。"""
    live = gpu_recovery.active()
    token = _deadline.set(time.monotonic() + recovery_timeout if live else None)
    try:
        if TTS_ENGINE == "irodori":
            recovered = False
            while True:
                try:
                    audio = _irodori_wav(text, extra_params or {}, wait_load)
                except (requests.RequestException, ValueError, wave.Error, EOFError) as error:
                    detail = _failure_detail(error)
                    loading = '"loading"' in detail or "読み込み中" in detail
                    if live and not recovered and gpu_recovery.is_oom(detail):
                        recovered = True
                        gpu_recovery.recover("Irodori", detail, deadline=_deadline.get())
                        wait_load = True
                        continue
                    if live and loading and time.monotonic() < _deadline.get():
                        time.sleep(min(0.25, max(0, _deadline.get() - time.monotonic())))
                        continue
                    print(f"[TTS] engine=voicevox fallback={detail} text={text[:40]!r}")
                    break
                else:
                    Path(output_path).write_bytes(audio)
                    print(f"[TTS] engine=irodori text={text[:40]!r}")
                    return "irodori"
        _synthesize_voicevox(text, output_path, extra_params)
        return "voicevox"
    finally:
        _deadline.reset(token)


def unload_irodori() -> bool:
    """GPU処理前に解放し、未ロードになったことまで確認する。"""
    if TTS_ENGINE != "irodori":
        return True
    try:
        response = requests.post(f"{IRODORI_URL}/unload", timeout=IRODORI_TIMEOUT)
        response.raise_for_status()
        response = requests.get(f"{IRODORI_URL}/health", timeout=IRODORI_TIMEOUT)
        response.raise_for_status()
        return response.json().get("loaded") is False
    except (requests.RequestException, ValueError) as error:
        print(f"[TTS] Irodori の解放失敗: {error}")
        return False


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
    """選択したエンジンで文ごとに音声合成し結合する。intro_textがある場合は冒頭一言を先頭に付ける。
    sentences: [{"text": str, "valence": float, "arousal": float}, ...]
    戻り値: 各文の実測尺[秒]（intro は含まない）。

    生成モーションを文に紐づけるのに使う。文字数比で割り当てると、漢字とかなで
    読み上げ速度が違うぶんズレて、話している内容と動きが合わなくなる
    """
    print(f"[TTS] 文ごと音声生成中... (engine: {TTS_ENGINE})")
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
        print(f"[TTS] 音声生成完了: {output_path} ({len(sentences)}文)")
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
        synthesize(text, part, HOOK_VOICE_PARAMS if (hook and i == 0) else None,
                   wait_load=False)
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
