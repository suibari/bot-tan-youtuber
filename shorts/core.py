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
  ARDY_ENGINE_ROOT    : ARDYエンジンの導入先 (既定 /mnt/data/ardy-engine)
  ARDY_MERGED_BASE    : テキストエンコーダ(15GB)の置き場。読み込み速度が
                        パイプライン全体を左右するのでSSDを指すこと
  ARDY_REPO           : text-to-vrma のリポジトリパス
  ARDY_PORT           : ARDYサーバーのポート (既定 2337)
  ARDY_BLEND_SEC      : 生成モーションのセグメント境界のクロスフェード長[秒] (既定 0.7)
  VRMA_BIG_SEC        : 山場のジェスチャー1本の目安の長さ[秒] (既定 3.0)
  VRMA_SMALL_SEC      : つなぎの待機動作1本の目安の長さ[秒] (既定 2.2)
  VRMA_SEG_MIN_SEC    : 1セグメントの下限[秒]。これ未満は動きとして成立しない (既定 2.0)
  VRMA_GAIN           : 生成モーションの腕の振幅ゲイン (既定 1.0=無効。実測で有害だった)
  VRMA_HIPS_Y         : 生成モーションの腰の上下移動の反映倍率 (既定 0=無効)
  VRMA_BODY_TILT      : 生成モーションの腰の傾きの反映倍率 (既定 1.0)
  VRMA_YAW_LIMIT      : 上体の向き(ヨー)の上限[度] (既定 35。0で正面固定)
  VRMA_HEAD_YAW       : クリップ由来の首の横振りの上限[度] (既定 15。0で無効)
  VRMA_HEAD_COUNTER   : 上体が向いたぶんを首で打ち消す割合 (既定 0.8)
  VRMA_KEEP_IDLE_HANDS: 指・親指・つま先を生成モーションで上書きしない (既定 1=有効)
  VRMA_ELBOW_BEND     : 肘を常時わずかに曲げる量[度] (既定 8)
  VRMA_WRIST_BEND     : 手首を常時わずかに曲げる量[度] (既定 6)
  VRMA_HEAD_TILT      : 首を常時わずかに傾ける量[度] (既定 4)
  VRMA_SMOOTH         : 生成モーションの平滑化の時定数[秒] (既定 0.10。0で無効)
  BGM_PATH            : BGM音声ファイルのパス (省略可)
  （真偽値の環境変数は env_flag() で読むこと。TRUE / 1 / yes も真として扱う）
  DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD : PostgreSQL接続情報
"""

import os
import re
import sys
import json
import time
import signal
import hashlib
import subprocess
import tempfile
from datetime import timezone, timedelta
from pathlib import Path


# `python shorts/pipeline.py` のように直接起動されると sys.path[0] が shorts/ に
# なるので、リポジトリのルートを足して common/ を読めるようにする。
# run.sh / run_quiz.sh も PYTHONPATH を渡すが、手で叩いたときのために両方入れておく
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common import ardy as _ardy, llm as _llm, motion_safety, voice as _voice, xvfb as _xvfb
from common.db import DB_CONFIG                                        # noqa: F401
from common.env import env_flag, env_float, env_int, env_float_opt     # noqa: F401
from common.env import LOGS_DIR, DATA_DIR, ROOT as REPO_ROOT           # noqa: F401

_JST = timezone(timedelta(hours=9))

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────

# VOICEVOX まわりは common/voice.py に集約した。ここは後方互換の再輸出。
# 夜版は Thumbnail の一言（generate_voice の intro_text）、
# 朝版は question_intro の1文目（掛け声）に HOOK_VOICE_PARAMS を当てる。
VOICEVOX_URL      = _voice.VOICEVOX_URL
VOICEVOX_SPEAKER  = _voice.VOICEVOX_SPEAKER
HOOK_VOICE_PARAMS = _voice.HOOK_VOICE_PARAMS
UNITY_EXE        = os.getenv("UNITY_EXE", "/home/suibari/Unity/Hub/Editor/6000.0.76f1/Editor/Unity")
UNITY_PROJECT    = os.getenv("UNITY_PROJECT", "/home/suibari/bottan-video")
# 指定するとUnityへ -vrmaMotionDir が渡り、VrmaMotionPlayer が該当モーションを差し替える。
# 未指定なら Unity 側は完全な no-op で、従来のMixamoモーションのまま。
VRMA_MOTION_DIR  = os.getenv("VRMA_MOTION_DIR", "")
BGM_PATH         = os.getenv("BGM_PATH", "")
USE_LOCAL_LLM    = _llm.USE_LOCAL_LLM

# 動画の出力仕様（Unity Recorder の設定と一致させること）
W, H = 1080, 1920
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

# VOICEVOX の出力フォーマット。無音WAVを作って結合するため一致させる
WAV_RATE     = _voice.WAV_RATE
WAV_CHANNELS = _voice.WAV_CHANNELS

# LLMクライアントは common/llm.py で初期化済み。ここは後方互換の再輸出
llm_client = _llm.client
LLM_MODELS = _llm.LLM_MODELS
LLM_MODEL  = _llm.LLM_MODEL


# ──────────────────────────────────────────────
# 汎用ヘルパー
# ──────────────────────────────────────────────

def _timed(label, fn, *args, **kwargs):
    print(f"[{label}] 開始...")
    start = time.time()
    result = fn(*args, **kwargs)
    print(f"[{label}] 完了 ({time.time()-start:.1f}s)")
    return result


# LLM 呼び出しは common/llm.py に集約した。ここは後方互換の薄い再輸出。
_retry          = _llm.retry
_llm_create     = _llm.create
parse_script_json = _llm.parse_json


def llm_json(system_prompt: str, user_prompt: str, schema: dict) -> dict:
    """system + user + スキーマ を渡して JSON を得る汎用呼び出し。

    temperature は渡さない（統合前と同じリクエストになる）。
    """
    return _llm.generate_json(system_prompt, user_prompt, schema, schema_name="script")


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

# 実装は common/voice.py にある。ここは後方互換の再輸出。
get_wav_duration = _voice.get_wav_duration
_synthesize      = _voice.synthesize
synthesize       = _voice.synthesize
valence_arousal_to_voicevox_params = _voice.valence_arousal_to_voicevox_params
synthesize_sentences = _voice.synthesize_sentences
make_silence_wav = _voice.make_silence_wav
concat_wavs      = _voice.concat_wavs
generate_voice   = _voice.generate_voice
voicevox_health_check = _voice.health_check


# ──────────────────────────────────────────────
# 字幕タイミング生成
# ──────────────────────────────────────────────

_query_mora_times = _voice.query_mora_times


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
    """空きディスプレイ番号でXvfbを起動し (proc, display) を返す。

    ライブ配信の GPU 仮想ディスプレイ（既定 :99）は除外する。奪うと Xorg が
    起動できず、systemd が Restart=always で無限にリトライする状態になる。
    """
    return _xvfb.start_xvfb(reserved=(os.getenv("LIVE_DISPLAY", ":99"),))


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
    # 残留Xvfbロックファイルを削除。
    # /tmp は sticky bit 付きなので、gdm の greeter (:1024/:1025) や bottan-live の
    # Xorg :99 (root) が置いたロックは suibari では消せず EPERM になる。
    # missing_ok=True は「無いとき」しか救わないため、握りつぶさないと録画前に落ちる。
    # 他人のロックは触らなくて当然なので、消せないものは黙って飛ばす。
    for _lock_file in Path("/tmp").glob(".X*-lock"):
        try:
            _lock_file.unlink(missing_ok=True)
        except OSError:
            pass
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
# ARDY エンジンの設定と起動・生成は common/ardy.py に集約した。
# ここは後方互換の再輸出（既存の呼び出し名を変えないため）。
ARDY_ENGINE_ROOT   = _ardy.ARDY_ENGINE_ROOT
ARDY_MERGED_BASE   = _ardy.ARDY_MERGED_BASE
ARDY_REPO          = _ardy.ARDY_REPO
ARDY_PORT          = _ardy.ARDY_PORT
ARDY_REUSE         = _ardy.ARDY_REUSE
ARDY_READY_TIMEOUT = _ardy.ARDY_READY_TIMEOUT
ARDY_GEN_TIMEOUT   = _ardy.ARDY_GEN_TIMEOUT
ARDY_MIN_AVAIL_GB  = _ardy.ARDY_MIN_AVAIL_GB
ARDY_CFG           = _ardy.ARDY_CFG
ARDY_ARM_SPREAD    = _ardy.ARDY_ARM_SPREAD
_ardy_url          = _ardy.url


# セグメントのつなぎ目のクロスフェード長[秒]。server.py の既定は6フレーム(20fps=0.3秒)で、
# 独立生成された別ポーズ同士を繋ぐには短く、「スッと切り替わった」ように見えていた。
# server.py 側は smoothstep で混ぜるので窓の両端で速度が0になる。
# 未パッチのサーバーはこのフィールドを無視するだけなので送っても壊れない
ARDY_BLEND_SEC = float(os.getenv("ARDY_BLEND_SEC", "0.7"))

# 1リクエストで渡せるセグメント数の上限。server.py の _resolve_segments が
# segments_req[:12] で黙って切り捨てるので、こちら側で必ず守る
ARDY_MAX_SEGMENTS = 12
# 生成1秒あたりの所要[秒]の見積り。タイムアウト算出にしか使わないので多めに取る。
# 実測はサーバー起動後の1本目が約10秒/秒（CUDAカーネルの初回コンパイル込み）、
# 2本目以降は約1.6秒/秒（24.75秒の生成に38.9秒）。
# ARDY_GEN_TIMEOUT の既定300秒では長いブロックの1本目が溢れるので、尺から動的に伸ばす
ARDY_GEN_SEC_PER_SEC = float(os.getenv("ARDY_GEN_SEC_PER_SEC", "10"))

# 1セグメントの目安の長さ[秒]。big は明確なジェスチャー、small は待機動作。
# この比で実尺を按分するだけなので、合計が尺に合わなくても構わない。
#
# 短くしてあるのは、ARDYの生成モーションが尺の後半で必ず止まるため。
# 実測（5秒生成・全ボーンの角速度[度/フレーム]、前半→後半）:
#   raises one hand to their chin        28.3 → 8.0
#   repeatedly sways their upper body    20.3 → 10.4
#   rocks from one foot to the other     19.7 → 11.4
# 「反復する動作」と書いても後半は持続しなかったので、
# 1本を短くして高エネルギーな前半だけを使うほうが効く
VRMA_BIG_SEC   = float(os.getenv("VRMA_BIG_SEC", "3.0"))
VRMA_SMALL_SEC = float(os.getenv("VRMA_SMALL_SEC", "2.2"))
# ARDYは20fpsなので、これより短いと数十フレームしか無く動きとして成立しない。
# 実測: 1.15秒のモーションはIdleとほぼ見分けがつかなかった
VRMA_SEG_MIN_SEC = float(os.getenv("VRMA_SEG_MIN_SEC", "2.0"))
# 1セグメントの目標尺[秒]。ブロック全体のセグメント本数はこれで決める
VRMA_SEG_TARGET_SEC = float(os.getenv("VRMA_SEG_TARGET_SEC", "2.6"))
# 1ブロックのセグメント総数の上限。ARDY_MAX_SEGMENTS(12) を超えるぶんは
# build_vrma_motions が .vrma を分割し、Unity 側がクリップ同士をクロスフェードする。
# 24 = 2チャンク。増やすほど生成時間が伸びるので、ここで頭を打たせる
VRMA_MAX_SEGMENTS_TOTAL = int(os.getenv("VRMA_MAX_SEGMENTS_TOTAL", "24"))
# 分割された .vrma を重ねて配置する量[秒]。
# VrmaMotionPlayer.FadeDuration と必ず一致させること。ずれると継ぎ目で
# 生成モーションが Idle に引き戻され、棒立ちが一瞬挟まる
VRMA_CHUNK_OVERLAP = 0.5
# ブロック末尾に残す余白[秒]。次のMixamoモーションに食い込ませない
VRMA_TAIL_PAD = 0.4

# ここから下は Unity(VrmaMotionPlayer) に渡す再生時の調整値。朝版・夜版で共通。
# 生成モーションの振幅ゲイン。Unity側で Idleポーズからの偏差を増幅する倍率（腕のみ）。
# 既定は 1.0 = 無効。実測で 1.2 も 1.35 も、ARDYが出す「手を頭の近くに上げる」動きを
# 引き伸ばして腕(袖)が顔を覆ってしまい、倍率を下げても改善しなかった。
# 動きを大きくするのはプロンプト側（ジャンプ・大振りの許可）の役目で、
# 生成済みポーズを事後に引き伸ばすこの経路は割に合わない。
# 仕組みは残してあるので、試すときは VRMA_GAIN=1.2 のように環境変数で上書きする
VRMA_GAIN     = float(os.getenv("VRMA_GAIN", "1.0"))
# 生成モーションの腰の上下移動を反映する倍率。既定0＝無効。
# ジャンプを画に出すための仕組みだが、そのジャンプ自体を VRMA_BANNED_RE で禁止した
# （スカートなので、ARDYが作る予備動作の深いしゃがみで下着が映る）。
# 跳ぶ動作が無い以上ここを有効にする意味がないので0にしてある。
# 衣装が変わって跳べるようになったら VRMA_HIPS_Y=1.0 で復活できる
VRMA_HIPS_Y   = float(os.getenv("VRMA_HIPS_Y", "0"))

# 生成モーションの「体の向き・傾き」をどこまで画に出すか。すべて Unity 側の引数。
#
# ARDY は体のワールド回転（向きも傾きも）を出していて .vrma にもそのまま入っているが、
# VrmaMotionPlayer が既定で全部捨てていた（顔のアップで横を向くと困るため）。
# 捨てるのをやめて、代わりに上限をかけて通す。
#
# VRMA_YAW_LIMIT: 上体をどこまで横に向けてよいか[度]。Idle からの絶対角で切る。
#   ARDY のセグメント連結は前セグメントの終端ヨーに合わせて回転を積み上げるので、
#   相対量で制限すると一度横を向いたまま戻らなくなる。絶対角なら構造的に起きない。
# VRMA_HEAD_COUNTER: 上体が向いたぶんを首で逆に回して顔をカメラに残す割合。
#   1.0 で顔が完全に正面。体は斜め・顔はこちら＝「肩越しに振り返る」画になる。
# VRMA_HEAD_YAW: クリップ由来の首の横振りを通す上限[度]。
#   従来は首のヨーも殺していた。ただし実測では ARDY に「首を横に振れ」と書いても
#   対照より小さい振幅しか出ない（下の VRMA_IDLE_MOTIONS のコメント参照）ので、
#   ここは意図した首振りのためではなく自然さのぶんだけ通す。小さめにしてある。
# VRMA_BODY_TILT: 腰の傾き(前後左右)の反映倍率。VRMA_GAIN は腕にしか効かないので、
#   上体の傾きの大きさを戻せるのはここだけ。
VRMA_BODY_TILT    = float(os.getenv("VRMA_BODY_TILT", "1.0"))
VRMA_YAW_LIMIT    = float(os.getenv("VRMA_YAW_LIMIT", "35"))
VRMA_HEAD_YAW     = float(os.getenv("VRMA_HEAD_YAW", "15"))
VRMA_HEAD_COUNTER = float(os.getenv("VRMA_HEAD_COUNTER", "0.8"))

# ── 女の子らしい所作にするための調整（2026-08-15）
#
# VRMA_KEEP_IDLE_HANDS: 指・親指・つま先をクリップで上書きせず、VRMモデルの
#   Idle ポーズを残す。1=有効。
#   .vrma の骨格には親指とつま先のボーンが無く（vrmaBuilder.js の SKELETON）、
#   人差し指〜小指も ARDY が出力しないので固定カール(14/17/10度)が焼き込まれている
#   だけ。Unity は全95 muscle を無条件に混ぜていたので、生成モーション区間
#   （＝ほぼ全編）でモデル本来の手のポーズが汎用の固定ポーズに置き換わり、
#   親指は Unity 既定(=0)で伸びたまま固定されていた。
#
# VRMA_ELBOW_BEND / VRMA_WRIST_BEND / VRMA_HEAD_TILT: 常時かける姿勢のバイアス[度]。
#   肘がピンと伸びた腕・真っ直ぐな手首・傾かない首は男性的に見える。
#   ARDY 側にこれを指示する手段が無い（プロンプトに書いても動かない）ので、
#   再生側で足す。符号付きなので、向きが逆なら負値を入れる。
#
# VRMA_SMOOTH: クリップのポーズを時間方向に平滑化する時定数[秒]（一次ローパス）。
#   角ばった・キビキビしすぎる動きの角を丸める。
#   実測（0.10秒・フレーム間差分）: 動き量 1.659→1.626（-2%）に対して
#   二階差分＝カクつきは 0.733→0.265（-64%）。**振幅はほぼ落ちない。**
#   代償は位相の遅れで、0.10秒 のとき約0.13秒ぶんモーションが後ろにずれる
#   （遅れは時定数に比例する）。話す内容との同期が気になるなら下げること。
VRMA_KEEP_IDLE_HANDS = int(os.getenv("VRMA_KEEP_IDLE_HANDS", "1"))
VRMA_ELBOW_BEND      = float(os.getenv("VRMA_ELBOW_BEND", "8"))
VRMA_WRIST_BEND      = float(os.getenv("VRMA_WRIST_BEND", "6"))
VRMA_HEAD_TILT       = float(os.getenv("VRMA_HEAD_TILT", "4"))
VRMA_SMOOTH          = float(os.getenv("VRMA_SMOOTH", "0.10"))


def vrma_unity_args() -> list[str]:
    """生成モーションの見た目を決める Unity 引数をまとめて作る。

    朝版・夜版で同じ値を使うので1か所にまとめてある（別々に書くと片方だけ
    直して食い違う）。VrmaMotionPlayer 側の既定値はすべて「従来どおり」なので、
    切り分けたいときはこの戻り値を渡さなければ改修前の見た目に戻る。
    """
    return [
        # カメラを引いた画に見合う大きさにする
        "-vrmaGain", f"{VRMA_GAIN}",
        "-vrmaHipsY", f"{VRMA_HIPS_Y}",
        # 体の向き・傾き。0 にすれば従来どおり正面固定に戻る
        "-vrmaBodyTilt", f"{VRMA_BODY_TILT}",
        "-vrmaYawLimit", f"{VRMA_YAW_LIMIT}",
        "-vrmaHeadYaw", f"{VRMA_HEAD_YAW}",
        "-vrmaHeadCounter", f"{VRMA_HEAD_COUNTER}",
        # 女の子らしい所作にするための調整。0 にすればそれぞれ無効になる
        "-vrmaKeepIdleHands", f"{VRMA_KEEP_IDLE_HANDS}",
        "-vrmaElbowBend", f"{VRMA_ELBOW_BEND}",
        "-vrmaWristBend", f"{VRMA_WRIST_BEND}",
        "-vrmaHeadTilt", f"{VRMA_HEAD_TILT}",
        "-vrmaSmooth", f"{VRMA_SMOOTH}",
    ]

# モーションの安全化（禁止語・主語の正規化・待機動作）は common/motion_safety.py に
# 集約した。実測で事故った履歴に基づく値なので、緩めるときはあちらのコメントを読むこと。
VRMA_BANNED_RE      = motion_safety.BANNED_MOTION_RE
VRMA_IDLE_MOTIONS   = motion_safety.IDLE_MOTIONS
reject_unsafe_motions = motion_safety.reject_unsafe_motions


# ARDY サーバーの起動・待機・停止・生成は common/ardy.py にある。
# ここは後方互換の再輸出。
ARDY_MEM_WAIT_SEC = _ardy.ARDY_MEM_WAIT_SEC
ARDY_FREE_OLLAMA  = _ardy.ARDY_FREE_OLLAMA
OLLAMA_URL        = _ardy.OLLAMA_URL

_free_ollama       = _ardy._free_ollama
ardy_wait_memory   = _ardy.wait_memory
_mem_available_gb  = _ardy.mem_available_gb
ardy_available     = _ardy.available
ardy_health        = _ardy.health
_kill_stray_server = _ardy.kill_stray_server
ardy_start         = _ardy.start
ardy_wait_ready    = _ardy.wait_ready
ardy_to_vrma       = _ardy.to_vrma
ardy_stop          = _ardy.stop


def ardy_generate(text: str, duration: float, seed: int, out_json: str) -> bool:
    """英語の動作説明文からモーションspec JSONを生成する。"""
    return _ardy.generate_spec(out_json, text=text, duration=duration,
                               seed=seed) is not None


def ardy_generate_segments(segments: list[dict], seed: int, out_json: str) -> float | None:
    """複数の動作説明文をつないだ1本の連続モーションspecを生成する。

    segments: [{"text": 英文, "duration": 秒}, ...]
    戻り値: 生成された長さ[秒]。失敗時は None。

    サーバー側は segments_req[:12] で黙って切り捨てるので、こちらで必ず守る。
    タイムアウトは実測の倍を見ておく（既定300秒では50秒のブロックが必ず溢れる）。
    """
    if not segments:
        return None
    segments = segments[:ARDY_MAX_SEGMENTS]
    total = sum(float(s["duration"]) for s in segments)
    timeout = max(ARDY_GEN_TIMEOUT, total * ARDY_GEN_SEC_PER_SEC * 2)
    return _ardy.generate_spec(out_json, segments=segments, seed=seed,
                               blend_sec=ARDY_BLEND_SEC, timeout=timeout)


# 【呼び出し元なし】2026-08-12 に朝版を文ベース（plan_vrma_from_sentences）へ移したため、
# この関数と dedupe_vrma_segments / merge_vrma_spans / VRMA_BIG_SEC / VRMA_SMALL_SEC は
# 使われていない。文ベースが安定するまで戻せるように残してある。
def plan_vrma_segments(motions: list[dict], available_sec: float,
                       max_segments: int = ARDY_MAX_SEGMENTS,
                       tail_pad: float = VRMA_TAIL_PAD,
                       used_idles: set | None = None) -> list[dict]:
    """台本の motions と区間の尺から、ardy_generate_segments 用の segments を組む。

    motions: [{"text": 英文, "emphasis": "big"|"small"}, ...]（空でもよい）
    available_sec: この区間に使える秒数
    max_segments: 分割数の上限。1ブロックを複数区間で分け合うときに配分する
    tail_pad: 末尾に残す余白[秒]。区間が連続していて余白が要らないなら0
    used_idles: 既に使った待機動作。コーナーをまたいで共有すると同じ動作が並ばない。
        渡さないとコーナーごとにリストの先頭から舐め直すので、実測で
        clasps both hands が23セグメント中5回出た
    戻り値: [{"text": str, "duration": float}, ...]  尺が足りなければ空リスト。

    台本のモーションだけでは尺が埋まらないので、足りない分は VRMA_IDLE_MOTIONS の
    待機動作で埋める。これが「棒立ちを作らない」ための本体。

    スカートで破綻する動作（しゃがむ・跳ぶ）は reject_unsafe_motions がここで落とす。
    """
    avail = float(available_sec) - tail_pad
    max_segments = max(1, min(int(max_segments), ARDY_MAX_SEGMENTS))
    if avail < VRMA_SEG_MIN_SEC:
        return []

    # 呼び出し側の漏れを防ぐため、危険な動作の除外はここで必ず通す
    items = [{"text": text, "emphasis": (m.get("emphasis") or "small")}
             for m in reject_unsafe_motions(motions)
             if (text := (m.get("text") or "").strip())]

    def target(it):
        return VRMA_BIG_SEC if it["emphasis"] == "big" else VRMA_SMALL_SEC

    # 目安の尺に届くまで待機動作を足す。使い回しを避けて一巡させる
    if used_idles is None:
        used_idles = set()
    while sum(target(it) for it in items) < avail and len(items) < max_segments:
        fresh = [m for m in VRMA_IDLE_MOTIONS if m not in used_idles]
        if not fresh:                      # 一巡したら使用済みを空にしてもう一周
            used_idles.clear()
            fresh = list(VRMA_IDLE_MOTIONS)
        pick = fresh[0]
        used_idles.add(pick)
        items.append({"text": pick, "emphasis": "small"})
    if not items:
        return []
    items = items[:max_segments]

    # 1本が短すぎると動きとして成立しないので、そうなる分は末尾から落とす
    while len(items) > 1:
        weights = [target(it) for it in items]
        if avail * min(weights) / sum(weights) >= VRMA_SEG_MIN_SEC:
            break
        items.pop()

    weights = [target(it) for it in items]
    total_w = sum(weights)
    return [{"text": it["text"], "duration": round(avail * w / total_w, 2)}
            for it, w in zip(items, weights)]


# 【呼び出し元なし】ペルソナが選んだ動きを待機動作で上書きしてしまうので、
# 文ベースへの移行にあわせて外した（plan_vrma_segments の項を参照）
def dedupe_vrma_segments(segments: list[dict], window: int = 4) -> list[dict]:
    """近い位置に同じ動作が並ばないよう、後から出たほうを待機動作に差し替える。

    LLMは同じプロンプトでも同じポーズを何度も選ぶ（実測: 23セグメント中
    clasps both hands が5回・nods their head が4回）。プロンプトの指示では
    下限を保証できないので、組み上がったあとにここで必ず均す。

    window: 直近この本数のなかに同じ動作があれば差し替える。
    """
    out: list[dict] = []
    for seg in segments:
        recent = {x["text"] for x in out[-window:]}
        if seg["text"] in recent:
            allused = {x["text"] for x in out}
            alt = (next((m for m in VRMA_IDLE_MOTIONS if m not in allused), None)
                   or next((m for m in VRMA_IDLE_MOTIONS if m not in recent), None))
            if alt:
                print(f"[モーション] 重複を差し替え: ...{seg['text'][45:75]}"
                      f" → ...{alt[45:75]}")
                seg = {**seg, "text": alt}
        out.append(seg)
    return out


# 指示文の先頭を "A woman stands in place and ..." に揃える正規表現。
# 「A <なにか> stands in place [facing forward] [and|.]」までを丸ごと拾う
_MOTION_PREFIX_RE    = motion_safety._MOTION_PREFIX_RE
MOTION_SUBJECT       = motion_safety.MOTION_SUBJECT
normalize_motion_text = motion_safety.normalize_motion_text


def plan_vrma_from_sentences(spans: list[dict], window_start: float, window_end: float,
                             max_total: int = VRMA_MAX_SEGMENTS_TOTAL) -> list[dict]:
    """文ごとのモーションを、その文が読まれる時刻に置くセグメント列にする。

    spans: [{"start": 秒, "end": 秒, "motion": 英文 or None}, ...]（文の順）
    戻り値: [{"text": str, "duration": float}, ...]

    尺で按分する plan_vrma_segments と違い、**どの文のときにどう動くか**が保たれる。
    台詞と動きが合っていないと、ただ動いているだけで見ていて不安になる。

    1文が長い（実測で平均8秒）ときは同じ指示文を2〜3本に分けて連続生成する。
    ARDYはセグメントごとに独立生成するので、同じ指示でも毎回新しい動きが出て、
    意図を保ったまま尺の後半で動きが止まるのを防げる。
    短すぎる文は直前のセグメントに吸収させる（1本が短いとIdleと見分けがつかない）。
    """
    def build(max_split: int, quiet: bool) -> list[dict]:
        out: list[dict] = []
        idle_i = 0

        def add(text: str, dur: float):
            if out and dur < VRMA_SEG_MIN_SEC:
                out[-1]["duration"] = round(out[-1]["duration"] + dur, 2)
            else:
                out.append({"text": text, "duration": round(dur, 2)})

        for sp in spans:
            start = max(float(sp["start"]), window_start)
            end = min(float(sp["end"]), window_end)
            if end - start <= 0.05:
                continue
            text = normalize_motion_text(sp.get("motion"))
            if not text or VRMA_BANNED_RE.search(text):
                if text and not quiet:
                    print(f"[モーション] 除外: {text[:70]}")
                text = VRMA_IDLE_MOTIONS[idle_i % len(VRMA_IDLE_MOTIONS)]
                idle_i += 1
            span = end - start
            n = max(1, min(max_split, round(span / VRMA_SEG_TARGET_SEC)))
            for _ in range(n):
                add(text, span / n)

        # 先頭の余りをIdleで埋める（フックの直後など、文が始まるまでの隙間）
        first = min((float(sp["start"]) for sp in spans if float(sp["end"]) > window_start),
                    default=window_end)
        lead = max(0.0, min(first, window_end) - window_start)
        if lead >= VRMA_SEG_MIN_SEC:
            out.insert(0, {"text": VRMA_IDLE_MOTIONS[0], "duration": round(lead, 2)})
        elif out and lead > 0:
            out[0]["duration"] = round(out[0]["duration"] + lead, 2)
        return out

    # 本数が上限を超えたら、まず「1文を何本に割るか」を減らして収める。
    # ここを削らずに末尾を切ると、台本が長い日に終盤が丸ごとIdle（棒立ち）に戻る。
    # 1文1本にしても超えるときだけ、短いセグメントを隣に吸収させて減らす
    for split in (3, 2, 1):
        out = build(split, quiet=(split != 3))
        if len(out) <= max_total:
            break
    if len(out) > max_total:
        print(f"[モーション] セグメントが{len(out)}本になったので"
              f"短いものを隣に吸収して{max_total}本にします")
        while len(out) > max_total:
            i = min(range(len(out)), key=lambda k: out[k]["duration"])
            j = i - 1 if i > 0 else 1
            out[j]["duration"] = round(out[j]["duration"] + out[i]["duration"], 2)
            out.pop(i)
    return out


# 【呼び出し元なし】plan_vrma_segments の項を参照
def merge_vrma_spans(spans: list[tuple]) -> list[tuple]:
    """連続した (label, start, end, motions) から、短すぎる区間を隣に吸収して穴を無くす。

    コーナーやパート単位で切ると、朝版の A（正解発表・実測1.8秒）のように
    1本のモーションとして成立しない区間が出る。落とすと連続再生に穴が空くので、
    後ろの区間とまとめてしまう（吸収された区間の motions もそのまま引き継ぐ）。
    区間は隙間なく並んでいる前提。
    """
    out, cur = [], None
    for label, start, end, motions in spans:
        if cur is None:
            cur = [label, start, end, list(motions or [])]
        else:
            cur[0] += f"+{label}"
            cur[2] = end
            cur[3] += list(motions or [])
        if cur[2] - cur[1] >= VRMA_SEG_MIN_SEC:
            out.append(tuple(cur))
            cur = None
    if cur is not None and out:
        # 末尾の余りは直前の区間に足す（新しく区間を作ると短すぎる）
        label, start, end, motions = out[-1]
        out[-1] = (f"{label}+{cur[0]}", start, cur[2], motions + cur[3])
    return out


def build_vrma_motions(blocks: list[dict], out_dir: str, seed_base: str) -> list[dict]:
    """ブロックごとに連続モーションを生成して emotions.json の vrmaMotions 用リストを返す。

    blocks: [{"name": str, "time": float, "segments": [{"text", "duration"}, ...]}, ...]
            time はブロックの開始時刻[秒]。text は英語の動作説明文
            （日本語だと FuguMT の英訳が崩れて品質が落ちる）。
    戻り値: [{"time": float, "file": str}, ...]  生成に失敗したブロックは黙って除く。

    ARDY_MAX_SEGMENTS を超えるブロックは複数の .vrma に分け、2本目以降は
    VRMA_CHUNK_OVERLAP だけ手前に重ねて置く。Unity(VrmaMotionPlayer) はこの重なりで
    2クリップを混ぜてから Idle に乗せるので、継ぎ目で棒立ちに引き戻されない。
    生成モーションはビルド成果物なので out_dir を毎回作り直す。
    """
    if not blocks:
        return []

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # 消さないと emotions.json に載らない過去の .vrma が溜まり続ける
    for old in out.glob("*.vrma"):
        old.unlink()
    tmp_dir = Path(tempfile.gettempdir())
    result = []

    for block in blocks:
        name = block["name"]
        segments = block["segments"]
        cursor = float(block["time"])
        chunks = [segments[i:i + ARDY_MAX_SEGMENTS]
                  for i in range(0, len(segments), ARDY_MAX_SEGMENTS)]

        for idx, segs in enumerate(chunks):
            part = name if len(chunks) == 1 else f"{name}{idx + 1}"
            # 同じ台本でも日付が変われば別の動きになるよう seed_base を混ぜる
            seed = int(hashlib.sha1(f"{seed_base}-{part}".encode()).hexdigest()[:8], 16) % (2 ** 31)
            spec_json = str(tmp_dir / f"ardy_spec_{part}.json")
            vrma_path = out / f"{part}.vrma"

            t0 = time.time()
            length = ardy_generate_segments(segs, seed, spec_json)
            if length is None or not ardy_to_vrma(spec_json, str(vrma_path)):
                # 続きを繋げる位置が決まらないので、このブロックはここで打ち切る
                break
            Path(spec_json).unlink(missing_ok=True)

            result.append({"time": round(cursor, 2), "file": vrma_path.name})
            print(f"[ARDY] {vrma_path.name} @{cursor:.2f}s "
                  f"({length:.1f}秒 / {len(segs)}セグメント / 生成{time.time() - t0:.1f}秒)")
            for seg in segs:
                print(f"[ARDY]   {seg['duration']:.1f}s ← {seg['text'][:70]}")
            # 次のチャンクは重ねて置く。Unity 側がこの重なりで2本をクロスフェードする
            cursor += length - VRMA_CHUNK_OVERLAP

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
