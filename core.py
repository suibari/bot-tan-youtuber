#!/usr/bin/env python3
"""
botたん動画パイプライン 共通処理

夜版 (pipeline.py) と朝版 (quiz_pipeline.py) が共有する処理をまとめる。

- 設定 / LLMクライアント
- VOICEVOX 音声合成
- 字幕タイミング生成 (モーラベース)
- 感情タイムライン
- Unity ヘッドレス録画
- ffmpeg 仕上げ (フィルタ生成 + エンコード)

環境変数:
  GEMINI_API_KEY      : Gemini APIキー (USE_LOCAL_LLM=false時)
  USE_LOCAL_LLM       : true でOllama使用、false でGemini使用 (デフォルト: false)
  LOCAL_LLM_MODEL     : Ollamaで使うモデル名
  LOCAL_LLM_CTX       : OllamaのコンテキストサイズOverride (デフォルト: 8192)
  GEMINI_MODEL        : Geminiのモデル名 (カンマ区切りで複数指定可、左から順にフォールバック)
  VOICEVOX_URL        : VOICEVOXのURL (デフォルト: http://localhost:10101)
  VOICEVOX_SPEAKER    : VOICEVOXのスピーカーID (デフォルト: 8)
  UNITY_EXE           : Unityエディタのパス
  UNITY_PROJECT       : Unityプロジェクトのパス
  VRMA_MOTION_DIR     : AI生成モーション(.vrma)の出力先 (省略時は従来のMixamoモーションのみ)
  ARDY_ENGINE_ROOT    : ARDYエンジンの導入先 (既定 /media/suibari/.../ardy-engine)
  ARDY_REPO           : text-to-vrma のリポジトリパス
  ARDY_PORT           : ARDYサーバーのポート (既定 2337)
  BGM_PATH            : BGM音声ファイルのパス (省略可)
  DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD : PostgreSQL接続情報
"""

import os
import re
import json
import time
import wave
import signal
import hashlib
import subprocess
import tempfile
from datetime import timezone, timedelta
from pathlib import Path

import requests
from openai import OpenAI, BadRequestError as _OpenAIBadRequestError

from dotenv import load_dotenv
load_dotenv()

_JST = timezone(timedelta(hours=9))

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────

VOICEVOX_URL     = os.getenv("VOICEVOX_URL", "http://localhost:10101")
VOICEVOX_SPEAKER = int(os.getenv("VOICEVOX_SPEAKER", "8"))
UNITY_EXE        = os.getenv("UNITY_EXE", "/home/suibari/Unity/Hub/Editor/6000.0.76f1/Editor/Unity")
UNITY_PROJECT    = os.getenv("UNITY_PROJECT", "/home/suibari/bottan-video")
# 指定するとUnityへ -vrmaMotionDir が渡り、VrmaMotionPlayer が該当モーションを差し替える。
# 未指定なら Unity 側は完全な no-op で、従来のMixamoモーションのまま。
VRMA_MOTION_DIR  = os.getenv("VRMA_MOTION_DIR", "")
BGM_PATH         = os.getenv("BGM_PATH", "")
USE_LOCAL_LLM    = os.getenv("USE_LOCAL_LLM", "false").lower() == "true"

# 動画の出力仕様（Unity Recorder の設定と一致させること）
W, H = 1080, 1920
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

# VOICEVOX の出力フォーマット。無音WAVを作って concat -c copy で混ぜるため一致させる
WAV_RATE     = 24000
WAV_CHANNELS = 1

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "192.168.1.200"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", ""),
    "user":     os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
}

# LLMクライアント初期化
if USE_LOCAL_LLM:
    llm_client = OpenAI(
        api_key="ollama",
        base_url="http://localhost:11434/v1"
    )
    LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M")
    LLM_MODELS = [LLM_MODEL]
    print(f"[LLM] Ollama ({LLM_MODEL}) を使用します")
else:
    llm_client = OpenAI(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    LLM_MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").split(",") if m.strip()]
    LLM_MODEL = LLM_MODELS[0]
    print(f"[LLM] Gemini ({', '.join(LLM_MODELS)}) を使用します")


# ──────────────────────────────────────────────
# 汎用ヘルパー
# ──────────────────────────────────────────────

def _timed(label, fn, *args, **kwargs):
    print(f"[{label}] 開始...")
    start = time.time()
    result = fn(*args, **kwargs)
    print(f"[{label}] 完了 ({time.time()-start:.1f}s)")
    return result


def _retry(label: str, fn, *args, attempts: int = 3, catch=(Exception,), delay: float = 0, **kwargs):
    """最大 attempts 回リトライする共通ヘルパー"""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except catch as e:
            if attempt == attempts:
                raise
            print(f"[{label}] 試行{attempt}失敗、リトライします... ({e})")
            if delay > 0:
                time.sleep(delay)


def _llm_create(attempts_per_model: int = 3, **kwargs):
    """LLM_MODELS を左から順に attempts_per_model 回ずつ試す。
    400/422 などリクエスト不正エラーは即時再送しても意味がないので1回で失敗させる。
    """
    last_exc = None
    for model in LLM_MODELS:
        for attempt in range(1, attempts_per_model + 1):
            try:
                return llm_client.chat.completions.create(model=model, **kwargs)
            except _OpenAIBadRequestError:
                # 400系はリトライ不要（プロンプトやスキーマの問題）
                raise
            except Exception as e:
                last_exc = e
                if attempt < attempts_per_model:
                    print(f"[LLM] {model} 試行{attempt}失敗、リトライします... ({e})")
                else:
                    print(f"[LLM] {model} {attempts_per_model}回失敗、次のモデルへ移行します ({e})")
    raise last_exc


def parse_script_json(raw_script: str) -> dict:
    """LLM構造化出力のJSONをパースする。```json フェンスを剥がしてから json.loads する。"""
    cleaned = re.sub(r"```json|```", "", raw_script).strip()
    return json.loads(cleaned)


def llm_json(system_prompt: str, user_prompt: str, schema: dict) -> dict:
    """system + user + スキーマ を渡して JSON を得る汎用呼び出し。

    Gemini は response_format(json_schema)、Ollama(Gemma) は構造化出力非対応なので
    extra_body でコンテキストサイズだけ調整する（pipeline.generate_script と同じ分岐）。
    """
    extra_kwargs: dict = {}
    if USE_LOCAL_LLM:
        extra_kwargs["extra_body"] = {
            "options": {"num_ctx": int(os.getenv("LOCAL_LLM_CTX", "8192"))},
            "think": False,
        }
    else:
        extra_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "script", "schema": schema},
        }

    response = _llm_create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        **extra_kwargs,
    )
    print(f"[DEBUG] finish_reason: {response.choices[0].finish_reason}")
    return parse_script_json(response.choices[0].message.content.strip())


# ──────────────────────────────────────────────
# 感情タイムライン
# ──────────────────────────────────────────────

def _rescale_va(values: list[float], target_min: float, target_max: float) -> list[float]:
    v_min, v_max = min(values), max(values)
    if v_max == v_min:
        mid = (target_min + target_max) / 2
        return [mid] * len(values)
    return [
        target_min + (v - v_min) / (v_max - v_min) * (target_max - target_min)
        for v in values
    ]


def enforce_variance(sentences: list[dict]) -> list[dict]:
    """valence/arousalを線形スケーリングして分散を強制する。LLMの第1象限偏りを補正。"""
    valences = [s.get("valence", 0.0) for s in sentences]
    arousals = [s.get("arousal", 0.0) for s in sentences]
    new_v = _rescale_va(valences, target_min=-0.3, target_max=1.0)
    new_a = _rescale_va(arousals, target_min=-0.8, target_max=1.0)
    return [
        {**s, "valence": round(v, 2), "arousal": round(a, 2)}
        for s, v, a in zip(sentences, new_v, new_a)
    ]


def build_emotion_timeline(
    sentences: list[dict],
    subtitles: list[dict],
    intro_duration: float = 0.0
) -> tuple[list[dict], float]:
    """文ごとのvalence/arousalから感情タイムラインを生成する。
    戻り値: (emotions, wave_time)

    NOTE: 文字数比で時刻を割り当てるため、無音区間を含む構成では使えない。
          朝版はパートの実尺から直接タイムラインを組むこと。
    """
    total_duration = subtitles[-1]["end"] if subtitles else 90
    total_chars = sum(len(s["text"]) for s in sentences)

    emotions = []
    char_offset = 0
    for sentence in sentences:
        ratio = char_offset / total_chars if total_chars > 0 else 0
        emotions.append({
            "time":    round(total_duration * ratio, 2),
            "valence": sentence.get("valence", 0.0),
            "arousal": sentence.get("arousal", 0.0),
        })
        char_offset += len(sentence["text"])

    wave_time = subtitles[-1]["start"] if subtitles else 0

    print(f"[感情] {len(emotions)}件のタイムライン生成完了, waveTime: {wave_time}s")
    return emotions, wave_time


# ──────────────────────────────────────────────
# VOICEVOX 音声合成
# ──────────────────────────────────────────────

def get_wav_duration(wav_path: str) -> float:
    """WAVファイルの長さを秒で返す"""
    with wave.open(wav_path, 'r') as f:
        return f.getnframes() / float(f.getframerate())


def _synthesize(text: str, output_path: str, extra_params: dict = None) -> None:
    """VOICEVOXでテキストを音声合成してファイルに保存する"""
    query_res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": VOICEVOX_SPEAKER}
    )
    query_res.raise_for_status()
    query = query_res.json()
    if extra_params:
        query.update(extra_params)
    synth_res = requests.post(
        f"{VOICEVOX_URL}/synthesis",
        params={"speaker": VOICEVOX_SPEAKER},
        headers={"Content-Type": "application/json"},
        data=json.dumps(query)
    )
    synth_res.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(synth_res.content)


def valence_arousal_to_voicevox_params(valence: float, arousal: float) -> dict:
    """valence/arousalをVOICEVOXの音声パラメータに変換する。
    speedScaleは一定に保つ（YouTube視聴体験のため速さの変動を避ける）。

    NOTE: 本番では未使用（聞き取りやすさのため発話の感情ぶれは無効化されている）。
    """
    return {
        "pitchScale":      round(arousal  * 0.08, 3),   # 興奮→高め
        "intonationScale": round(1.0 + valence * 0.3, 3),  # ポジティブ→抑揚強め
        "volumeScale":     round(1.0 + arousal * 0.1, 3),  # 興奮→大きめ
    }


def synthesize_sentences(sentences: list[dict], out_dir, prefix: str,
                         extra_params: dict = None) -> list[tuple[str, float]]:
    """文ごとに合成し [(wav_path, duration), ...] を返す。

    duration は get_wav_duration による実測値。呼び出し側はこれを積み上げて
    パート開始時刻を確定する（推定値を使わない）。
    """
    out_dir = Path(out_dir)
    results = []
    for i, sentence in enumerate(sentences):
        path = str(out_dir / f"{prefix}_{i:03d}.wav")
        _synthesize(sentence["text"], path, extra_params)
        results.append((path, get_wav_duration(path)))
    return results


def make_silence_wav(output_path, duration: float) -> str:
    """VOICEVOX と同一フォーマット (24kHz/mono/pcm_s16le) の無音WAVを作る。

    フォーマットを合わせることで concat demuxer の -c copy にそのまま乗る。
    """
    output_path = str(output_path)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"anullsrc=r={WAV_RATE}:cl=mono",
         "-t", f"{duration:.3f}",
         "-ar", str(WAV_RATE), "-ac", str(WAV_CHANNELS), "-c:a", "pcm_s16le",
         output_path],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return output_path


def concat_wavs(paths: list[str], output_path: str) -> None:
    """WAVを concat demuxer で結合する（全て同一フォーマットである必要がある）"""
    list_file = output_path + ".concat_list.txt"
    try:
        with open(list_file, "w") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", output_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    finally:
        if Path(list_file).exists():
            Path(list_file).unlink()


def generate_voice(sentences: list[dict], output_path: str, intro_text: str = "") -> None:
    """VOICEVOXで文ごとに音声合成し結合する。intro_textがある場合は冒頭一言を先頭に付ける。
    sentences: [{"text": str, "valence": float, "arousal": float}, ...]
    """
    print(f"[VOICEVOX] 文ごと音声生成中... (speaker: {VOICEVOX_SPEAKER})")
    tmp_dir = Path(tempfile.gettempdir())
    part_paths = []
    intro_wav = output_path.replace(".wav", "_intro.wav")

    try:
        if intro_text:
            _synthesize(intro_text, intro_wav, {
                "speedScale":      0.85,
                "intonationScale": 1.4,
                "volumeScale":     1.3,
                "pitchScale":      0.05,
            })
            part_paths.append(intro_wav)

        for i, sentence in enumerate(sentences):
            part_path = str(tmp_dir / f"{Path(output_path).stem}_part{i:03d}.wav")
            _synthesize(sentence["text"], part_path)
            part_paths.append(part_path)

        concat_wavs(part_paths, output_path)
        print(f"[VOICEVOX] 音声生成完了: {output_path} ({len(sentences)}文)")

    finally:
        for p in part_paths:
            if p != intro_wav:  # intro_wavは呼び出し元でcleanup
                if Path(p).exists():
                    Path(p).unlink()


# ──────────────────────────────────────────────
# 字幕タイミング生成
# ──────────────────────────────────────────────

def _query_mora_times(text: str) -> tuple[list[dict], float]:
    """audio_queryからモーラタイミングリストと総尺(秒)を返す"""
    res = requests.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": VOICEVOX_SPEAKER}
    )
    res.raise_for_status()
    query = res.json()

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


def split_sentences(script: str) -> list[str]:
    """台本を句読点・改行で文に分割する（共通ユーティリティ）"""
    parts = re.split(r"(?<=[。！？\n])", script)
    return [p.strip() for p in parts if p.strip()]


SUBTITLE_GAP = 0.06   # 隣り合う字幕の間に必ず空ける秒数


def generate_subtitle_timing(script: str, time_offset: float = 0.0,
                             actual_duration: float = None,
                             max_chars: int = 15,
                             merge_short: bool = False) -> list[dict]:
    """文ごとにaudio_queryを発行してタイミングを取得し、字幕データを生成する。

    文単位で独立したモーラ計測を行うことで、漢字とかなの混在による
    文字数比率ずれを排除する。
    actual_duration: 実際の音声WAVの本編部分の長さ（秒）。渡された場合はそれを
    スケーリング基準にする。各文個別合成+concatの場合はこれを使うと正確になる。
    max_chars: 1字幕ブロックの最大文字数。
    merge_short: 読点で切れた短い断片を max_chars まで結合する。
                 「萩、」「桔梗、」のように1語だけの字幕が並ぶのを防ぐ。
                 夜版の見た目を変えないよう既定はFalse。
    """
    print("[字幕] タイミング情報取得中...")

    # フルスクリプトのクエリで合計尺を取得（actual_duration未指定時のフォールバック用）
    _, total_duration = _query_mora_times(script)

    # 台本を句読点・改行で文に分割
    sentences = split_sentences(script)

    # 各文のモーラタイミングと尺を取得
    sentence_data = []
    for sentence in sentences:
        mora_times, sent_dur = _query_mora_times(sentence)
        sentence_data.append((sentence, mora_times, sent_dur))

    total_sent = sum(d for _, _, d in sentence_data)
    # actual_durationが渡された場合は実際のWAV長さを基準にスケーリング
    # （各文個別合成+concatでは total_sent が実際の音声長さに近い）
    # フルスクリプトクエリは文境界の pause_mora を含むため total_duration > total_sent になり
    # スケールが1.0を超えて字幕が遅れる問題が起きるため、actual_durationで補正する
    if actual_duration is not None:
        scale = actual_duration / total_sent if total_sent > 0 else 1.0
    else:
        scale = total_duration / total_sent if total_sent > 0 else 1.0

    subtitles = []
    current_time = 0.0

    for sentence, sent_moras, sent_dur in sentence_data:
        scaled_dur = sent_dur * scale
        n_sent_moras = len(sent_moras)
        mora_scale = scaled_dur / sent_dur if sent_dur > 0 else 1.0

        # 読点・中点でさらに分割 → max_chars 上限チャンク
        chunks = re.split(r"(?<=[、,，・])", sentence)
        chunks = [c for c in chunks if c.strip()]
        final_chunks = []
        for chunk in chunks:
            if len(chunk) <= max_chars:
                final_chunks.append(chunk)
            else:
                for i in range(0, len(chunk), max_chars):
                    final_chunks.append(chunk[i:i+max_chars])

        if merge_short and final_chunks:
            merged = [final_chunks[0]]
            for chunk in final_chunks[1:]:
                if len(merged[-1]) + len(chunk) <= max_chars:
                    merged[-1] += chunk
                else:
                    merged.append(chunk)
            final_chunks = merged

        sentence_chars = sum(len(c) for c in final_chunks)
        char_offset = 0

        for chunk in final_chunks:
            if n_sent_moras > 0 and sentence_chars > 0:
                s_idx = min(int(char_offset * n_sent_moras / sentence_chars), n_sent_moras - 1)
                # 終端は「次のチャンクの先頭モーラの1つ手前」。
                # -1 を忘れると e_idx(N) == s_idx(N+1) となり、前後の字幕が
                # そのモーラ長ぶん重なって同じ位置に2枚描画される。
                idx_end = int((char_offset + len(chunk)) * n_sent_moras / sentence_chars)
                e_idx   = max(s_idx, min(idx_end, n_sent_moras) - 1)
                start_t = current_time + sent_moras[s_idx]["start"] * mora_scale
                end_t   = current_time + (sent_moras[e_idx]["start"] + sent_moras[e_idx]["duration"]) * mora_scale + 0.05
            else:
                ratio_s = char_offset / max(sentence_chars, 1)
                ratio_e = (char_offset + len(chunk)) / max(sentence_chars, 1)
                start_t = current_time + ratio_s * scaled_dur
                end_t   = current_time + ratio_e * scaled_dur + 0.05
            subtitles.append({
                "start": round(start_t + time_offset, 3),
                "end":   round(end_t   + time_offset, 3),
                "text":  chunk,
            })
            char_offset += len(chunk)

        current_time += scaled_dur

    _dedupe_subtitle_overlaps(subtitles)

    print(f"[字幕] {len(subtitles)}ブロック生成完了")
    return subtitles


def _dedupe_subtitle_overlaps(subtitles: list[dict]) -> None:
    """隣り合う字幕の表示時間が重ならないよう end を切り詰める（in-place）。

    drawtext の enable='between(t,s,e)' は両端を含むため、隣接するだけでも
    境界フレームで2枚描画される。SUBTITLE_GAP ぶん明示的に空ける。
    モーラ配分の丸めで再発しうるので、インデックス修正とは別に安全網として置く。
    """
    for cur, nxt in zip(subtitles, subtitles[1:]):
        limit = nxt["start"] - SUBTITLE_GAP
        if cur["end"] > limit:
            # 詰まったチャンクが表示0秒に潰れないよう下限を設ける
            cur["end"] = round(max(cur["start"] + 0.15, limit), 3)


def _find_subtitle_time(subtitles: list[dict], keyword: str, start_from: float = 0.0) -> float | None:
    """keyword を含む最初の字幕の start を返す。見つからなければ None。"""
    for sub in subtitles:
        if sub["start"] >= start_from and keyword in sub["text"]:
            return sub["start"]
    return None


# ──────────────────────────────────────────────
# Unity ヘッドレス録画
# ──────────────────────────────────────────────

def _start_xvfb() -> tuple:
    """空きディスプレイ番号でXvfbを起動し (proc, display) を返す"""
    import shutil
    if not shutil.which("Xvfb"):
        raise RuntimeError("Xvfb が見つかりません。`sudo apt install -y xvfb` でインストールしてください")
    for n in range(99, 200):
        lock = Path(f"/tmp/.X{n}-lock")
        sock = Path(f"/tmp/.X11-unix/X{n}")
        if lock.exists() or sock.exists():
            continue
        display = f":{n}"
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if sock.exists():
                print(f"[Xvfb] 起動完了: DISPLAY={display}")
                return proc, display
            time.sleep(0.2)
        proc.kill()
    raise RuntimeError("Xvfb: 空きディスプレイ番号が見つかりません (99-199)")


def record_with_unity(wav_path: str, output_webm: str, emotion_path: str,
                      extra_args: list = None) -> None:
    """Unityを起動してVRM口パク録画を行う。

    extra_args: Unityへ追加で渡すCLI引数（例: ["-cameraOffsetY", "0.16"]）。
                None のとき既存の夜版と完全に同一のコマンドになる。

    NOTE: 冒頭で全Unityプロセスを pkill -9 するため、夜版と朝版を同時に走らせてはならない。
          起動スクリプト側で flock により直列化すること。
    """
    print(f"[Unity] 録画開始...")
    # 既存のUnityプロセスとXvfbを終了
    import subprocess as sp
    sp.run(["pkill", "-9", "-f", "Unity -projectPath"], capture_output=True)
    sp.run(["pkill", "-9", "-f", "Xvfb :"], capture_output=True)
    time.sleep(3)
    # 残留Xvfbロックファイルを削除
    for _lock_file in Path("/tmp").glob(".X*-lock"):
        _lock_file.unlink(missing_ok=True)
    # Unityプロジェクトのロックファイル・一時ファイルを削除
    for _unity_lock in [
        Path(UNITY_PROJECT) / "Temp" / "UnityLockFile",
        Path(UNITY_PROJECT) / "Library" / "ArtifactDB-lock",
    ]:
        _unity_lock.unlink(missing_ok=True)

    output_base = output_webm.replace(".webm", "")

    env = os.environ.copy()
    env.pop("XAUTHORITY", None)  # Xvfb は XAUTHORITY 不要

    xvfb_proc = None
    unity_proc = None
    try:
        xvfb_proc, display = _start_xvfb()
        env["DISPLAY"] = display

        cmd = [
            UNITY_EXE,
            "-projectPath", UNITY_PROJECT,
            "-wavFile", wav_path,
            "-outputFile", output_base,
            "-emotionFile", emotion_path,
        ]
        if VRMA_MOTION_DIR:
            cmd += ["-vrmaMotionDir", VRMA_MOTION_DIR]
        if extra_args:
            cmd += list(extra_args)
        print(f"[Unity] コマンド: {' '.join(cmd)}")

        unity_proc = subprocess.Popen(cmd, env=env, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        # 「書き込み完了を確認してから自分でkillした」= 録画は成功、を表すフラグ。
        # この経路を通った場合、Unityの終了コードは我々がkillした結果でしかなく、判定に使えない。
        write_completed = False
        deadline = time.time() + 300
        while time.time() < deadline:
            if Path(output_webm).exists() and os.path.getsize(output_webm) > 0:
                print(f"[Unity] ファイル検出: {output_webm}")
                # ファイルサイズが安定するまで待つ
                prev_size = 0
                while True:
                    time.sleep(2)
                    current_size = os.path.getsize(output_webm)
                    print(f"[Unity] ファイルサイズ: {current_size} bytes")
                    if current_size == prev_size and current_size > 0:
                        print(f"[Unity] 書き込み完了を確認")
                        break
                    prev_size = current_size
                try:
                    os.killpg(os.getpgid(unity_proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    unity_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(unity_proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        unity_proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                write_completed = True
                break
            if unity_proc.poll() is not None:
                break
            time.sleep(2)
        else:
            try:
                os.killpg(os.getpgid(unity_proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                unity_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            # タイムアウト時にeditor.logを出力（診断用）
            # プロジェクト名から導出する（bottan-video / bottan-video-dev の両対応）
            _editor_log = (Path.home() / ".config/unity3d/DefaultCompany"
                           / Path(UNITY_PROJECT).name / "Editor.log")
            if _editor_log.exists():
                _lines = _editor_log.read_text(errors="replace").splitlines()
                print("[Unity] Editor.log (最後50行):")
                for _line in _lines[-50:]:
                    print(f"  {_line}")
            raise TimeoutError("Unity録画タイムアウト (300秒)")

        # 判定は終了コードではなく成果物で行う。
        # Unityは録画完了後のシャットダウンで mono が SIGSEGV し、恒常的に 255 を返す
        # （VRMA無効のベースラインでも再現。Editor.log に "Exiting early due to double fault"）。
        # 以前はこれを失敗扱いにしていたため、毎回リトライで録画を2回走らせていた。
        if not Path(output_webm).exists():
            raise FileNotFoundError(f"録画ファイルが見つかりません: {output_webm}")
        if not write_completed:
            # 書き込み完了を確認する前にUnityが自滅した = 出力が途中の可能性がある
            raise RuntimeError(
                f"Unity録画失敗 (書き込み完了を確認できず, returncode: {unity_proc.returncode})"
            )
        # SIGTERM(-15), SIGKILL(-9) は我々がkillした結果なので正常
        if unity_proc.returncode not in (0, None, -15, -9):
            print(f"[Unity] 終了コード {unity_proc.returncode} "
                  f"(録画完了後のシャットダウン時クラッシュ。出力は正常なので続行)")

        print(f"[Unity] 録画完了: {output_webm}")
        time.sleep(5)  # GPU メモリ解放待ち

    finally:
        if unity_proc:
            if unity_proc.poll() is None:
                try:
                    os.killpg(os.getpgid(unity_proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    unity_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(unity_proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            # poll() is not None でも wait() でゾンビを回収する
            try:
                unity_proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
        if xvfb_proc:
            if xvfb_proc.poll() is None:
                xvfb_proc.terminate()
                try:
                    xvfb_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    xvfb_proc.kill()
            try:
                xvfb_proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass


# ──────────────────────────────────────────────
# AI生成モーション (ARDY ローカルエンジン)
# ──────────────────────────────────────────────
#
# text-to-vrma の ARDY エンジンをローカルHTTPサーバーとして起動し、
# 英語の動作説明文からモーションspecを生成して .vrma に変換する。
#
#   英文 → POST /generate → spec JSON → spec2vrma.mjs → .vrma → Unity
#
# サーバーは RSS 約9.5GB を占有するため常駐させず、パイプライン実行中だけ起動する。
# モデル読み込みに4〜5分かかるので、台本生成・音声合成の前に起動しておくこと。

# /mnt/data は sda1(ext4, 1.2TB) で /etc/fstab に nofail 付きで登録済み。
# 以前は NTFS(sda2) 上にあったが、udisks2 の自動マウントはデスクトップセッション依存で
# systemd timer からの無人実行では未マウントになり生成が丸ごとスキップされていた。
# ext4 化で FUSE のオーバーヘッドも外れる（ただし同じHDDなので速度改善は限定的）
ARDY_ENGINE_ROOT   = os.getenv("ARDY_ENGINE_ROOT", "/mnt/data/ardy-engine")
ARDY_REPO          = os.getenv("ARDY_REPO", "/home/suibari/work/text-to-vrma")
ARDY_PORT          = int(os.getenv("ARDY_PORT", "2337"))
# true にすると既にポートで動いているサーバーをそのまま使う（開発時用）。
# 既定は false で、古いサーバーは落として起動し直す
ARDY_REUSE         = os.getenv("ARDY_REUSE", "false").lower() == "true"
ARDY_READY_TIMEOUT = float(os.getenv("ARDY_READY_TIMEOUT", "600"))
# 3秒のモーションで実測3〜4秒。ただしGPUが混んでいると20秒、稀に140秒まで伸びる。
# さらに長時間アイドルだったサーバーは最初のリクエストで固まることがある
# （GPU使用率0%のまま返らない）ため、待ち続けずに諦めて動画を優先する。
# パイプラインは生成後にサーバーを落とすので、途中で切って壊れても影響はない。
ARDY_GEN_TIMEOUT   = float(os.getenv("ARDY_GEN_TIMEOUT", "300"))


def _ardy_url(path: str) -> str:
    return f"http://127.0.0.1:{ARDY_PORT}{path}"


# ARDYサーバーは RSS 約15GB を使う（CPU側に載せる8Bエンコーダが大半）。
# 空きがこれを下回るとスワップスラッシングを起こし、生成がGPU使用率0%のまま返らなくなる。
# 実測: ollama の llama-server が 9.2GB 常駐していた状態で 300秒タイムアウトした
# swap を増設した場合はスワップで吸収できるので低めでよい。
# swap 2GB のときは 18GB 必要だったが、17GB に増設したため 13GB まで下げている
ARDY_MIN_AVAIL_GB = float(os.getenv("ARDY_MIN_AVAIL_GB", "13"))

# classifier-free guidance（1.0〜6.0）。テキスト追従の強さ。
# 実測: 3.0→4.5→6.0 と上げても腕の振れ幅は 1.8→1.4→1.0度 とむしろ減った。
# 「動かないプロンプト」を追従で救うことはできないので server.py の既定3.0のまま使う。
# 動きの有無を決めるのはプロンプトの語彙（具体的な身体動作か抽象語か）だった
ARDY_CFG = float(os.getenv("ARDY_CFG", "3.0"))
# 腕の開き具合[度]（0〜20）。モーションではなく静的なオフセットで、
# 実測で 6→12→18 が腕の角度 70→64→58度（体側から離れる方向）に対応した。
# 腕が体に張り付いて見えるのを緩和するため既定より少し開く
ARDY_ARM_SPREAD = float(os.getenv("ARDY_ARM_SPREAD", "12"))


# 空きメモリが足りないとき、ここまで待つ[秒]。
# ollama は既定5分のkeep_aliveでモデルを自動解放するので、待てば空くことが多い
ARDY_MEM_WAIT_SEC = float(os.getenv("ARDY_MEM_WAIT_SEC", "360"))
# true にすると生成前に ollama のモデルをアンロードさせる。
# ollama は次のリクエストで自動的に読み直すので停止はしないが、
# そちらのサービスの次回応答が数秒遅くなる。既定は無効（他サービスに触らない）
ARDY_FREE_OLLAMA = os.getenv("ARDY_FREE_OLLAMA", "false").lower() == "true"
OLLAMA_URL       = os.getenv("OLLAMA_URL", "http://localhost:11434")


def _free_ollama() -> None:
    """ollama に読み込み済みモデルを解放させる（keep_alive=0）。失敗しても無視する。"""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5)
        for m in r.json().get("models", []):
            name = m.get("name")
            if not name:
                continue
            requests.post(f"{OLLAMA_URL}/api/generate",
                          json={"model": name, "keep_alive": 0}, timeout=30)
            print(f"[ARDY] ollama のモデルを解放しました: {name} "
                  f"({m.get('size', 0) / 1e9:.1f}GB) — 次のリクエストで自動的に読み直されます")
    except Exception as e:
        print(f"[ARDY] ollama の解放をスキップ（無視）: {e}")


def ardy_wait_memory(timeout: float = None) -> bool:
    """空きメモリが閾値を超えるまで待つ。超えたら True。"""
    if ARDY_FREE_OLLAMA:
        _free_ollama()

    limit = ARDY_MEM_WAIT_SEC if timeout is None else timeout
    deadline = time.time() + limit
    warned = False
    while True:
        avail = _mem_available_gb()
        if avail >= ARDY_MIN_AVAIL_GB:
            return True
        if time.time() >= deadline:
            print(f"[ARDY] 空きメモリが回復しませんでした ({avail:.1f}GB < {ARDY_MIN_AVAIL_GB}GB)。"
                  f"生成モーションをスキップします")
            return False
        if not warned:
            print(f"[ARDY] 空きメモリ待ち ({avail:.1f}GB < {ARDY_MIN_AVAIL_GB}GB, 最大{limit:.0f}秒)")
            warned = True
        time.sleep(15)


def _mem_available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    return float("inf")   # 読めないなら判定しない


def ardy_available() -> bool:
    """エンジン一式が揃っているか。NTFS未マウント時などに False になる。"""
    root = Path(ARDY_ENGINE_ROOT)
    ok = ((root / "venv/bin/python").exists()
          and (root / "llm2vec-base-merged").is_dir()
          and (Path(ARDY_REPO) / "tools/ardy-engine/server.py").exists()
          and (Path(ARDY_REPO) / "tools/spec2vrma.mjs").exists())
    if not ok:
        print(f"[ARDY] エンジンが見つかりません（{ARDY_ENGINE_ROOT}）。生成モーションはスキップします")
    return ok


def ardy_health() -> dict | None:
    try:
        return requests.get(_ardy_url("/health"), timeout=5).json()
    except Exception:
        return None


def _kill_stray_server() -> None:
    """ポートを掴んでいる ARDY サーバーを落とす。自分が起動したものでなくても止める。"""
    marker = f"{ARDY_ENGINE_ROOT}/venv/bin/python"
    try:
        out = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True).stdout
    except Exception:
        return
    for line in out.splitlines():
        if "server.py" in line and ("--port" in line) and (marker in line or "ardy-engine" in line):
            pid = line.split(None, 1)[0]
            if not pid.isdigit():
                continue
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f"[ARDY] 既存サーバー PID={pid} を停止しました")
            except (ProcessLookupError, PermissionError, ValueError):
                pass
    # ポートが解放されるまで少し待つ
    for _ in range(10):
        if ardy_health() is None:
            return
        time.sleep(1)


def ardy_start():
    """ARDYサーバーを起動する。

    既に起動済みならそれを再利用し None を返す（そのサーバーは ardy_stop で落とさない）。
    エンジンが無い場合も None を返す。起動できたかどうかは ardy_wait_ready() で判定すること。
    """
    if not ardy_available():
        return None

    # サーバーは約15GB必要。足りないまま起動すると読み込み自体がスワップで
    # 10分以上かかる（実測: 600秒待っても準備完了にならず）ので、空くまで待つ
    if not ardy_wait_memory():
        return None

    h = ardy_health()
    if h is not None:
        if ARDY_REUSE:
            print(f"[ARDY] 既存のサーバーを再利用します (status={h.get('status')})。"
                  f"このサーバーはパイプライン終了時に停止しません")
            return None
        # 既定では再利用しない。長く生きたサーバーは生成が返らなくなることがあり
        # （GPU使用率0%のままタイムアウト）、それを掴むと丸ごと生成を落とすため、
        # 落として自分で起動し直す。ポートはこのパイプライン専用とみなす
        print("[ARDY] 既存のサーバーを停止して起動し直します "
              "（古いサーバーは生成が返らないことがあるため。ARDY_REUSE=true で再利用可）")
        _kill_stray_server()

    root = Path(ARDY_ENGINE_ROOT)
    env = os.environ.copy()
    # これが無いと 8B のテキストエンコーダが GPU に載って CUDA OOM になる。
    # Electron版も同じ値を渡している (electron/ardy-client.cjs)
    env["TEXT_ENCODER_DEVICE"] = "cpu"
    env["HF_HOME"] = str(root / "hf-cache")

    cmd = [str(root / "venv/bin/python"),
           str(Path(ARDY_REPO) / "tools/ardy-engine/server.py"),
           "--port", str(ARDY_PORT),
           "--merged-base", str(root / "llm2vec-base-merged")]
    print(f"[ARDY] サーバー起動: {' '.join(cmd)}")
    return subprocess.Popen(cmd, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=os.setsid)


def ardy_wait_ready(timeout: float = None) -> bool:
    """GET /health が status=ok になるまで待つ。モデル読み込みに4〜5分かかる。"""
    # サーバーも立っておらずエンジンも無いなら、待っても上がらない。
    # ここで即抜けないと NTFS 未マウント時に毎回10分止まる
    if ardy_health() is None and (not ardy_available()
                                  or _mem_available_gb() < ARDY_MIN_AVAIL_GB):
        return False

    deadline = time.time() + (ARDY_READY_TIMEOUT if timeout is None else timeout)
    last = None
    while time.time() < deadline:
        h = ardy_health()
        if h is not None:
            status = h.get("status")
            if status == "ok":
                print(f"[ARDY] 準備完了 (model={h.get('model')} device={h.get('device')})")
                return True
            if status == "error":
                print(f"[ARDY] 起動に失敗しました: {h.get('error')}")
                return False
            cur = (h.get("stage"), round(h.get("progress") or 0, 2))
            if cur != last:
                print(f"[ARDY] 読み込み中... stage={cur[0]} progress={cur[1]}")
                last = cur
        time.sleep(5)
    print(f"[ARDY] 準備完了になりませんでした（{ARDY_READY_TIMEOUT if timeout is None else timeout:.0f}秒待機）")
    return False


def ardy_generate(text: str, duration: float, seed: int, out_json: str) -> bool:
    """英語の動作説明文からモーションspec JSONを生成する。"""
    try:
        r = requests.post(_ardy_url("/generate"),
                          json={"text": text, "duration": float(duration), "seed": int(seed),
                                "cfg": ARDY_CFG, "armSpread": ARDY_ARM_SPREAD},
                          timeout=ARDY_GEN_TIMEOUT)
        spec = r.json()
    except Exception as e:
        print(f"[ARDY] 生成に失敗: {e}")
        return False

    if "tracks" not in spec:
        print(f"[ARDY] 生成結果が不正です: {str(spec)[:200]}")
        return False

    Path(out_json).write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    print(f"[ARDY] 生成: {spec.get('duration')}秒 / {len(spec['tracks'])}ボーン / "
          f"seed={seed} cfg={ARDY_CFG} armSpread={ARDY_ARM_SPREAD}")
    return True


def ardy_to_vrma(spec_json: str, out_vrma: str) -> bool:
    """spec JSON を .vrma (GLB) に変換する。three.js しか使わない純JSなのでNodeだけで動く。"""
    try:
        subprocess.run(
            ["node", str(Path(ARDY_REPO) / "tools/spec2vrma.mjs"), spec_json, out_vrma],
            check=True, capture_output=True, timeout=120)
        return True
    except Exception as e:
        detail = getattr(e, "stderr", b"")
        print(f"[ARDY] .vrma 変換に失敗: {e} {detail[:200] if detail else ''}")
        return False


def ardy_stop(proc) -> None:
    """ardy_start が起動したサーバーを落とす。None（再利用時）なら何もしない。"""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)
        except Exception:
            pass
    except Exception as e:
        print(f"[ARDY] 停止時のエラー（無視）: {e}")
    print("[ARDY] サーバーを停止しました")


def build_vrma_motions(specs: list[dict], out_dir: str, seed_base: str) -> list[dict]:
    """モーションを一括生成して emotions.json の vrmaMotions 用リストを返す。

    specs: [{"name": str, "time": float, "text": str, "duration": float}, ...]
           text は英語の動作説明文。日本語だと FuguMT の英訳が崩れて品質が落ちる。
    戻り値: [{"time": float, "file": str}, ...]  生成に失敗したものは黙って除く。

    生成モーションはビルド成果物なので out_dir を毎回上書きする。
    """
    if not specs:
        return []

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.gettempdir())
    result = []

    for spec in specs:
        name = spec["name"]
        # 同じ台本でも日付が変われば別の動きになるよう seed_base を混ぜる
        seed = int(hashlib.sha1(f"{seed_base}-{name}".encode()).hexdigest()[:8], 16) % (2 ** 31)
        spec_json = str(tmp_dir / f"ardy_spec_{name}.json")
        vrma_path = out / f"{name}.vrma"

        t0 = time.time()
        if not ardy_generate(spec["text"], spec["duration"], seed, spec_json):
            continue
        if not ardy_to_vrma(spec_json, str(vrma_path)):
            continue
        Path(spec_json).unlink(missing_ok=True)

        result.append({"time": round(float(spec["time"]), 2), "file": vrma_path.name})
        print(f"[ARDY] {name}.vrma @{spec['time']:.2f}s ({time.time() - t0:.1f}秒) "
              f"← {spec['text'][:60]}")

    return result


# ──────────────────────────────────────────────
# ffmpeg 仕上げ
# ──────────────────────────────────────────────

def esc_drawtext(s: str) -> str:
    """drawtext の text='...' に埋め込むためのエスケープ。

    呼び出し側は必ず text='{esc_drawtext(x)}' のようにシングルクォートで囲み、
    `:expansion=none` を付けること。

    ffmpeg 6.1.1 での実測に基づく:
      - リテラルの `\\` を出すには 4個必要（1個・2個だと文字ごと消える）
      - `'` は `\\'` ではエスケープできない。シングルクォート内に `'` は書けないので
        いったん閉じて `\\'` を置き、また開く（`'\\''`）必要がある。
        単体では通ってしまうがフィルタを連結すると後続フィルタの解析が壊れる。
      - `%{...}` は `\\%` でエスケープしても展開される。`expansion=none` でしか止まらない
    """
    return (s.replace("\\", "\\\\\\\\")
             .replace("'", "'\\''")
             .replace(":", "\\:"))


def base_vf_parts() -> list[str]:
    """縦型Shortsへのスケール+パディング"""
    return [
        f"scale={W}:{H}:force_original_aspect_ratio=decrease",
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black",
    ]


def build_subtitle_filters(subtitles: list[dict]) -> list[str]:
    """下部のミント帯 + 白文字の字幕フィルタを生成する（夜版レイアウト）"""
    parts = []
    for sub in subtitles:
        start = sub["start"]
        end   = sub["end"]
        text  = esc_drawtext(sub["text"])
        parts.append(
            f"drawbox=x=0:y={H-420}:w={W}:h=160:color=0x00A88A@0.92:t=fill"
            f":enable='between(t,{start},{end})'"
        )
        parts.append(
            f"drawtext=fontfile={FONT_PATH}:text='{text}'"
            f":expansion=none"
            f":fontcolor=white:fontsize=52:x=(w-text_w)/2:y={H-370}"
            f":enable='between(t,{start},{end})'"
        )
    return parts


def build_corner_filters(corners: list[dict]) -> list[str]:
    """左上のコーナーテロップ（白箱 + カラー下線 + カラー文字）"""
    parts = []
    for corner in corners:
        start = corner["start"]
        end   = corner["end"]
        label = esc_drawtext(corner["label"])
        color = corner["color"].replace("#", "0x")
        box_w = min(len(corner["label"]) * 38 + 40, W - 40)
        parts.append(
            f"drawbox=x=20:y=40:w={box_w}:h=70:color=white@0.9:t=fill"
            f":enable='between(t,{start},{end})'"
        )
        parts.append(
            f"drawbox=x=20:y=108:w={box_w}:h=6:color={color}@1.0:t=fill"
            f":enable='between(t,{start},{end})'"
        )
        parts.append(
            f"drawtext=fontfile={FONT_PATH}:text='{label}'"
            f":expansion=none"
            f":fontcolor={color}:fontsize=36:x=30:y=52"
            f":enable='between(t,{start},{end})'"
        )
    return parts


def run_ffmpeg_finalize(input_webm: str, output_mp4: str, vf_parts: list[str],
                        bgm_volume: float = 0.05, timeout: int = 120) -> None:
    """フィルタチェーンを適用してMP4に変換する。BGMがあれば amix でミックスする。"""
    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_webm,
    ]

    if BGM_PATH and Path(BGM_PATH).exists():
        cmd += ["-i", BGM_PATH,
                "-filter_complex",
                f"[0:v]{vf}[v];[1:a]volume={bgm_volume}[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]",
                "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-vf", vf]

    cmd += [
        # "-c:v", "h264_nvenc", #NOTE: UnityスクショとGPUエンコードは両立不可
        "-c:v", "libx264",
        "-preset", "fast",
        "-c:a", "aac",
        "-shortest",
        output_mp4
    ]

    # デバッグ用にコマンドを保存できるようにする（リファクタ前後の差分検証に使う）
    dump = os.getenv("FFMPEG_CMD_DUMP", "")
    if dump:
        Path(dump).write_text(json.dumps(cmd, ensure_ascii=False, indent=1))
        print(f"[FFmpeg] コマンドを保存: {dump}")

    subprocess.run(cmd, check=True, timeout=timeout)
    print(f"[FFmpeg] 変換完了: {output_mp4}")


# ──────────────────────────────────────────────
# 一時ファイルの掃除
# ──────────────────────────────────────────────

def cleanup_old_temp_files(patterns=("bottan_*", "quiz_*"), max_age_days: int = 7) -> None:
    """/tmp に溜まった過去の生成物を削除する。

    既存パイプラインは mp4/png/emotions.json を消さずに残すため、
    放置するとディスクを圧迫する（動画1本あたり約30MB）。
    """
    tmp_dir = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    freed = 0
    for pattern in patterns:
        for p in tmp_dir.glob(pattern):
            try:
                if not p.is_file() or p.stat().st_mtime >= cutoff:
                    continue
                freed += p.stat().st_size
                p.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[Cleanup] 古い一時ファイル {removed}件を削除 ({freed / 1024 / 1024:.1f}MB)")
