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
import unicodedata
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
from common import vrma_style as _vrma_style
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


SUBTITLE_GAP     = 0.06   # 隣り合う字幕の間に必ず空ける秒数
SUBTITLE_MIN_SEC = 0.40   # 1ブロックの最短表示時間。これを下回るものは隣と結合する

# 行頭・行末に置けない文字（禁則処理）。wrap_cjk と split_subtitle_chunks で共有する。
_NO_LINE_START = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮーぐんゝゞ、。，．・？！」』）】〕〉》")
_NO_LINE_END   = set("「『（【〔〈《")

# 途中で切ってはいけない文字（ラテン文字・数字とその内部に現れる記号）。
# 「Don't ever give up.」が「Don't eve」「r give up.」に割れるのを防ぐ。
_WORD_CHAR = re.compile(r"[0-9A-Za-z'’\-.]")


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in "WFA" else 1


def wrap_cjk(text: str, max_units: int) -> list[str]:
    """全角=2 / 半角=1 で数えて折り返す。textwrap は CJK の幅を扱えないため自前。

    行頭に小書き仮名や句読点が来ないよう、1文字ぶん前の行に送る。
    朝版（quiz_layout）と夜版（build_subtitle_filters）で共有する。
    """
    lines: list[str] = []
    cur = ""
    width = 0
    for ch in text:
        cw = _char_width(ch)
        if width + cw > max_units and cur:
            # 次の文字が行頭に来られないなら、この文字は今の行に残す
            if ch in _NO_LINE_START:
                cur += ch
                lines.append(cur)
                cur, width = "", 0
                continue
            # 行末に来られない文字で終わるなら1文字繰り越す
            if cur[-1] in _NO_LINE_END:
                carry = cur[-1]
                lines.append(cur[:-1])
                cur, width = carry, _char_width(carry)
            else:
                lines.append(cur)
                cur, width = "", 0
        cur += ch
        width += cw
    if cur:
        lines.append(cur)
    return lines


def wrap_subtitle_lines(text: str, max_units: int, max_lines: int = 2) -> list[str]:
    """字幕1枚を行に割る。**2行になるときは行の長さを均す。**

    素の wrap_cjk は先頭行を目一杯詰めるので、19文字が「18文字」＋「も、」のように
    泣き別れる。読みにくいだけでなく、帯の中で下の行だけ極端に短く見える。
    幅を半分から広げていって、max_lines に収まる最小の幅を採る。

    朝版（quiz_layout.build_caption_filters）と夜版（build_subtitle_filters）で共有。
    """
    lines = wrap_cjk(text, max_units)
    if len(lines) <= 1:
        return lines
    total = sum(_char_width(ch) for ch in text)
    for units in range(-(-total // max_lines), max_units + 1):
        balanced = wrap_cjk(text, units)
        if len(balanced) <= max_lines:
            return balanced
    return lines[:max_lines]


# 語を割らないために max_chars からはみ出してよい文字数。
# 2行に折り返して描くので、多少長くても帯には収まる（20文字=40単位に対して
# 2行ぶんの max_units は 72単位ある）。
_CUT_SLACK = 4


def _snap_cut(text: str, cut: int, lo: int, hi: int) -> int:
    """cut がラテン文字・数字の連続の内側なら、語の境界へ寄せる。

    [lo, hi] は「そこで切れば前後のチャンクが max_chars に収まる」範囲で、
    語を割らないためだけに _CUT_SLACK ぶんまで外へはみ出してよい。
    手前の語頭に戻すのを優先し、駄目なら語尾へ送る。どちらも届かない
    （1語が max_chars より長い）なら諦めて cut のまま切る。
    """
    if cut <= 0 or cut >= len(text):
        return cut
    if not (_WORD_CHAR.match(text[cut - 1]) and _WORD_CHAR.match(text[cut])):
        return cut
    lo = max(1, lo - _CUT_SLACK)
    hi = min(len(text) - 1, hi + _CUT_SLACK)
    back = cut
    while back > 0 and _WORD_CHAR.match(text[back - 1]):
        back -= 1
    if lo <= back <= hi:
        return back
    fwd = cut
    while fwd < len(text) and _WORD_CHAR.match(text[fwd]):
        fwd += 1
    if lo <= fwd <= hi:
        return fwd
    return cut


def split_subtitle_chunks(sentence: str, max_chars: int) -> list[str]:
    """1文を字幕1枚ぶんのチャンクに割る。

    - まず読点・中点で切る（意味の切れ目が最優先）
    - 短い断片は max_chars まで**先に**結合する。「萩、」「桔梗、」のように
      1語だけの字幕が並ぶのを防ぐ。**結合は分割より先**でないと効かない
      （以前は分割の後にやっていたので、`15文字+1文字` に割れたあと
      `15+1=16 > 15` で判定に落ち、1文字の字幕が必ず残っていた）
    - それでも max_chars を超える断片は**均等割り**にする。固定長スライスだと
      16文字 / max_chars=15 が必ず「15文字 + 1文字」になる。均等割りなら「8+8」
    - 切れ目がラテン文字・数字の内側に落ちたら語の境界へ寄せる
      （`Don't eve` / `r give up.` を防ぐ）。ただし寄せた結果どれかの
      チャンクが max_chars を超えるなら寄せない。
    """
    fragments = [c for c in re.split(r"(?<=[、,，・])", sentence) if c.strip()]
    if not fragments:
        return []

    chunks = [fragments[0]]
    for frag in fragments[1:]:
        if len(chunks[-1]) + len(frag) <= max_chars:
            chunks[-1] += frag
        else:
            chunks.append(frag)

    out: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            out.append(chunk)
            continue
        n = -(-len(chunk) // max_chars)          # ceil
        pos = 0
        for i in range(n - 1, 0, -1):
            remain = len(chunk) - pos
            target = pos + max(1, -(-remain // (i + 1)))
            # 残り i ピースが max_chars に収まる最小位置 〜 現ピースが収まる最大位置
            lo = max(pos + 1, len(chunk) - i * max_chars)
            hi = min(pos + max_chars, len(chunk) - 1)
            cut = min(max(target, lo), hi)
            out.append(chunk[pos:_snap_cut(chunk, cut, lo, hi)])
            pos += len(out[-1])
        out.append(chunk[pos:])
    return [c for c in out if c]


def _chunk_mora_bounds(chunks: list[str], n_sent_moras: int) -> list[tuple]:
    """各チャンクが文のモーラ列のどこからどこまでかを返す [(s_idx, e_idx), ...]。

    **文字数比では割らない。** 「1文字あたりのモーラ数は文中で一定」という前提は
    ラテン文字が混ざると崩れる。`Don't ever give up.` は19文字あるがモーラは
    ずっと少ないので、分母の文字数だけが膨らみ、同じ文の日本語チャンクに
    割り当てられるモーラが本来の 1/3 以下になる（「実はね、」が 0.179秒 に潰れた）。

    そこでチャンクごとに audio_query を投げて**実モーラ数**を数え、その累積で
    境界を決める。読点で切っただけなら合計は文全体のモーラ数と一致するが、
    アクセント句の切れ方で前後することがあるので比率で正規化する。
    """
    counts = [len(_query_mora_times(c)[0]) for c in chunks]
    total = sum(counts)
    if total <= 0 or n_sent_moras <= 0:
        return [(0, max(n_sent_moras - 1, 0)) for _ in chunks]

    bounds = []
    acc = 0
    for c in counts:
        s_idx = min(int(round(acc * n_sent_moras / total)), n_sent_moras - 1)
        acc += c
        idx_end = int(round(acc * n_sent_moras / total))
        # 終端は「次のチャンクの先頭モーラの1つ手前」。-1 を忘れると
        # e_idx(N) == s_idx(N+1) となり、前後の字幕が境界で2枚描画される。
        e_idx = max(s_idx, min(idx_end, n_sent_moras) - 1)
        bounds.append((s_idx, e_idx))
    return bounds


def generate_subtitle_timing(script: str, time_offset: float = 0.0,
                             actual_duration: float = None,
                             max_chars: int = 20) -> list[dict]:
    """文ごとにaudio_queryを発行してタイミングを取得し、字幕データを生成する。

    文単位で独立したモーラ計測を行うことで、漢字とかなの混在による
    文字数比率ずれを排除する。
    actual_duration: 実際の音声WAVの本編部分の長さ（秒）。渡された場合はそれを
    スケーリング基準にする。各文個別合成+concatの場合はこれを使うと正確になる。
    max_chars: 1字幕ブロックの最大文字数。朝版・夜版とも 20。
               夜版は build_subtitle_filters が2行に折り返して描く。
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

        final_chunks = split_subtitle_chunks(sentence, max_chars)
        if not final_chunks:
            current_time += scaled_dur
            continue

        if n_sent_moras > 0:
            bounds = _chunk_mora_bounds(final_chunks, n_sent_moras)
            for chunk, (s_idx, e_idx) in zip(final_chunks, bounds):
                start_t = current_time + sent_moras[s_idx]["start"] * mora_scale
                end_t   = current_time + (sent_moras[e_idx]["start"]
                                          + sent_moras[e_idx]["duration"]) * mora_scale + 0.05
                subtitles.append({"start": round(start_t + time_offset, 3),
                                  "end":   round(end_t + time_offset, 3),
                                  "text":  chunk})
        else:
            sentence_chars = sum(len(c) for c in final_chunks)
            char_offset = 0
            for chunk in final_chunks:
                ratio_s = char_offset / max(sentence_chars, 1)
                ratio_e = (char_offset + len(chunk)) / max(sentence_chars, 1)
                subtitles.append({
                    "start": round(current_time + ratio_s * scaled_dur + time_offset, 3),
                    "end":   round(current_time + ratio_e * scaled_dur + 0.05 + time_offset, 3),
                    "text":  chunk})
                char_offset += len(chunk)

        current_time += scaled_dur

    _merge_short_subtitles(subtitles, max_chars)
    _dedupe_subtitle_overlaps(subtitles)
    _close_subtitle_gaps(subtitles)

    print(f"[字幕] {len(subtitles)}ブロック生成完了")
    return subtitles


def _merge_short_subtitles(subtitles: list[dict], max_chars: int) -> None:
    """SUBTITLE_MIN_SEC より短い表示になったブロックを隣と結合する（in-place）。

    分割と割り当てを直したのでほとんど出ないが、モーラが極端に短い語
    （「ねぇ、」など）では残りうる。読めない一瞬の点滅は「字幕が消えた」に見えるので、
    最後の安全網として潰す。**2行に折り返せる前提**なので、結合の上限は
    max_chars の2倍まで許す。
    """
    limit = max_chars * 2
    i = 0
    while i < len(subtitles):
        cur = subtitles[i]
        if cur["end"] - cur["start"] >= SUBTITLE_MIN_SEC:
            i += 1
            continue
        prev = subtitles[i - 1] if i > 0 else None
        nxt  = subtitles[i + 1] if i + 1 < len(subtitles) else None
        # 時間的に近いほうへ寄せる。文の切れ目には読点のポーズが入るので、
        # 素直に「間隔が短いほう」を選べば同じ文の中で結合される。
        # 前後どちらも長すぎて入らないなら、その1枚は諦めてそのままにする。
        cand = []
        if prev is not None and len(prev["text"]) + len(cur["text"]) <= limit:
            cand.append(("prev", cur["start"] - prev["end"]))
        if nxt is not None and len(cur["text"]) + len(nxt["text"]) <= limit:
            cand.append(("next", nxt["start"] - cur["end"]))
        if not cand:
            i += 1
            continue
        side = min(cand, key=lambda c: c[1])[0]
        if side == "prev":
            prev["text"] += cur["text"]
            prev["end"] = cur["end"]
            subtitles.pop(i)
        else:
            nxt["text"] = cur["text"] + nxt["text"]
            nxt["start"] = cur["start"]
            subtitles.pop(i)
        i = max(i - 1, 0)


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


SUBTITLE_MAX_HOLD = 1.2   # 空白を埋めるために字幕を伸ばしてよい上限[秒]


def _close_subtitle_gaps(subtitles: list[dict], max_hold: float = SUBTITLE_MAX_HOLD) -> None:
    """字幕どうしの空白を、前の字幕を伸ばして埋める（in-place）。

    モーラ列には読点のポーズ（pause_mora）が要素として入らないので、素で作ると
    「ねぇ、」のあとに 0.6秒 の空白ができる。字幕はその間だけ消え、読んでいる側には
    点滅に見える。実際の字幕は文が続くあいだ出しっぱなしにするのが普通なので、
    次の字幕の直前まで伸ばす。

    ただし無音が長い箇所（セクションの切れ目など）で古い字幕が居座らないよう、
    伸ばす量は max_hold で頭打ちにする。
    """
    for cur, nxt in zip(subtitles, subtitles[1:]):
        limit = round(nxt["start"] - SUBTITLE_GAP, 3)
        if cur["end"] < limit:
            cur["end"] = round(min(limit, cur["end"] + max_hold), 3)


def _find_subtitle_time(subtitles: list[dict], keyword: str, start_from: float = 0.0) -> float | None:
    """keyword を含む最初の字幕の start を返す。見つからなければ None。

    **チャンクを跨いだ場合も拾う。** 字幕は max_chars で機械的に割られるので、
    「高評価」「行ってらっしゃい」が2枚に割れることがある。1枚ずつしか見ないと
    見つからず、呼び出し側の thankfulTime が 0 に落ちてモーションが変わる。
    跨いだときは keyword が始まったほうのブロックの start を返す。
    """
    cands = [s for s in subtitles if s["start"] >= start_from]
    for i, sub in enumerate(cands):
        if keyword in sub["text"]:
            return sub["start"]
        # 後続を連結して、このブロックから始まる出現を探す
        joined = sub["text"]
        for nxt in cands[i + 1:]:
            joined += nxt["text"]
            if len(joined) >= len(sub["text"]) + len(keyword):
                break
        hit = joined.find(keyword)
        if 0 <= hit < len(sub["text"]):
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


# セグメントのつなぎ目のクロスフェード長[秒]。配信側も同じ値を使うので
# common/ardy.py が原典（解説はあちらのコメントを読むこと）
ARDY_BLEND_SEC = _ardy.ARDY_BLEND_SEC

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
# 分割された .vrma を重ねて配置する量[秒]。common/vrma_style.py に移した
# （配信側も次のモーションを投げる間隔として同じ値を使うため）。
# VrmaMotionPlayer.FadeDuration と必ず一致させること。ずれると継ぎ目で
# 生成モーションが Idle に引き戻され、棒立ちが一瞬挟まる
VRMA_CHUNK_OVERLAP = _vrma_style.VRMA_CHUNK_OVERLAP
# ブロック末尾に残す余白[秒]。次のMixamoモーションに食い込ませない
VRMA_TAIL_PAD = 0.4

# Unity(VrmaMotionPlayer) に渡す再生時の調整値は common/vrma_style.py にある。
# 配信（live/unity_live.py）も同じ値で Unity を起動するので、片方だけ直して
# 食い違わないよう共有している。ここは後方互換の再輸出。
VRMA_GAIN            = _vrma_style.VRMA_GAIN
VRMA_HIPS_Y          = _vrma_style.VRMA_HIPS_Y
VRMA_BODY_TILT       = _vrma_style.VRMA_BODY_TILT
VRMA_YAW_LIMIT       = _vrma_style.VRMA_YAW_LIMIT
VRMA_HEAD_YAW        = _vrma_style.VRMA_HEAD_YAW
VRMA_HEAD_COUNTER    = _vrma_style.VRMA_HEAD_COUNTER
VRMA_KEEP_IDLE_HANDS = _vrma_style.VRMA_KEEP_IDLE_HANDS
VRMA_ELBOW_BEND      = _vrma_style.VRMA_ELBOW_BEND
VRMA_WRIST_BEND      = _vrma_style.VRMA_WRIST_BEND
VRMA_HEAD_TILT       = _vrma_style.VRMA_HEAD_TILT
VRMA_SMOOTH          = _vrma_style.VRMA_SMOOTH
vrma_unity_args      = _vrma_style.vrma_unity_args


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
      - `'` はシングルクォート内には書けない。いったん閉じ、**バックスラッシュ3個**を
        付けた `\\\\\\'` を置いてから開き直す（`'\\\\\\''`）。
        アンエスケープが2段（フィルタグラフ → オプション値）かかるため、
        `'\\''`（1個）だと**その drawtext だけ静かに何も描かれない**。
        ffmpeg は 0 で終了し、後続フィルタも動くのでログにも出ない。
        実測: 「Nagiで「Don't ever」の1枚が丸ごと透明になっていた
      - `%{...}` は `\\%` でエスケープしても展開される。`expansion=none` でしか止まらない
    """
    return (s.replace("\\", "\\\\\\\\")
             .replace("'", "'\\\\\\''")
             .replace(":", "\\:"))


def base_vf_parts() -> list[str]:
    """縦型Shortsへのスケール+パディング"""
    return [
        f"scale={W}:{H}:force_original_aspect_ratio=decrease",
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black",
    ]


SUBTITLE_FONT_SIZE = 52
SUBTITLE_LINE_H    = 62      # fontsize=52 の行送り
SUBTITLE_BAND_Y    = H - 420  # 帯の上端。**動かさないこと**（下げると口が隠れる）
SUBTITLE_BAND_H    = 160      # 1行のときの帯の高さ
SUBTITLE_TEXT_Y    = H - 370  # 1行目のベースライン位置
SUBTITLE_COLOR     = "0x00A88A"


def build_subtitle_filters(subtitles: list[dict]) -> list[str]:
    """下部のミント帯 + 白文字の字幕フィルタを生成する（夜版レイアウト）。

    朝版（quiz_layout.build_caption_filters）と同じく wrap_cjk で最大2行に
    折り返す。1行に収める前提だと max_chars を 15 まで下げるしかなく、
    読点の無い長い句が機械的に割れて 1文字だけの字幕が出ていた。

    **帯（drawbox）は字幕ごとに出さない。** 以前は帯にも字幕と同じ enable を
    付けていたので、ブロックの切れ目（SUBTITLE_GAP=0.06秒、実測では最大0.6秒）
    ごとに帯が丸ごと消えてチカチカしていた。字幕がある区間を通しで1枚塗る。

    帯の高さは**動画を通して一定**にする（いちばん長い字幕の行数で決める）。
    字幕ごとに変えると帯が上下して目立つ。伸ばすのは下方向だけ。上端を下げると
    カメラを上げたときの口元にかかる（quiz_layout.py の冒頭を参照）。

    行ごとに drawtext を分けるのは build_target_filters と同じ理由。1つの
    drawtext に改行を入れると ffmpeg はブロック全体の外接矩形を中央に置くだけで、
    短いほうの行が左に寄って見える（text_align は ffmpeg 7 以降にしか無い）。
    """
    if not subtitles:
        return []

    max_units = (W - 120) * 2 // SUBTITLE_FONT_SIZE
    wrapped = [(s, wrap_subtitle_lines(s["text"], max_units)) for s in subtitles]
    n_lines = max((len(lines) for _, lines in wrapped), default=1)

    band_h = SUBTITLE_BAND_H + SUBTITLE_LINE_H * (n_lines - 1)
    band_start, band_end = subtitles[0]["start"], subtitles[-1]["end"]
    parts = [
        f"drawbox=x=0:y={SUBTITLE_BAND_Y}:w={W}:h={band_h}"
        f":color={SUBTITLE_COLOR}@0.92:t=fill"
        f":enable='between(t,{band_start},{band_end})'"
    ]
    for sub, lines in wrapped:
        start, end = sub["start"], sub["end"]
        # 行数の少ない字幕は帯の中で縦中央に置く
        top = SUBTITLE_TEXT_Y + SUBTITLE_LINE_H * (n_lines - len(lines)) // 2
        for i, line in enumerate(lines):
            parts.append(
                f"drawtext=fontfile={FONT_PATH}:text='{esc_drawtext(line)}'"
                f":expansion=none"
                f":fontcolor=white:fontsize={SUBTITLE_FONT_SIZE}"
                f":x=(w-text_w)/2:y={top + SUBTITLE_LINE_H * i}"
                f":enable='between(t,{start},{end})'"
            )
    return parts


# ターゲットテロップ（夜版）。「誰に向けた動画か」を動画全体に出し続ける。
# Shorts はスワイプで途中から入るので、冒頭だけに出しても届かない。
TARGET_FONT_SIZE   = 72
TARGET_LINE_H      = 92     # fontsize=72 の行送り（実測に合わせた概算）
TARGET_MAX_CHARS   = 11     # 72px × 11文字 = 792px。W=1080 に収まる最大
TARGET_COLOR       = "0x00A88A"


def wrap_target_text(text: str, max_chars: int = TARGET_MAX_CHARS) -> list[str]:
    """テロップを最大2行に折り返す。

    Thumbnail の一言は20文字以内なので 11文字 × 2行 で必ず収まる。
    句読点で切れると読みやすいので、後半に句読点があればそこを優先する。
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    # 折り返し位置の候補: 前半すぎない位置にある句読点の直後
    cut = max_chars
    for i in range(min(max_chars, len(text) - 1), max_chars // 2, -1):
        if text[i - 1] in "、。！？":
            cut = i
            break
    return [text[:cut], text[cut:cut + max_chars]]


def build_target_filters(text: str) -> list[str]:
    """画面上部に出し続ける大テロップ（白箱 + カラー下線 + カラー文字）。

    以前ここにあった左上のコーナーテロップ（「今日のNagi」等）の意匠を踏襲しつつ、
    中央寄せ・大サイズにしたもの。
    enable を付けないので動画全体に出る（Shorts はスワイプで途中から入るため）。

    複数行は drawtext を行ごとに分けて出す。1つの drawtext に改行を入れると
    ffmpeg はブロック全体の外接矩形を中央に置くだけなので、短いほうの行が
    左に寄って見える（text_align は ffmpeg 7 以降にしか無い）。
    """
    lines = wrap_target_text(text)
    if not lines or not lines[0]:
        return []

    box_h  = TARGET_LINE_H * len(lines) + 28
    box_w  = min(max(len(l) for l in lines) * TARGET_FONT_SIZE + 56, W - 40)
    box_x  = (W - box_w) // 2
    box_y  = 40

    parts = [
        f"drawbox=x={box_x}:y={box_y}:w={box_w}:h={box_h}:color=white@0.9:t=fill",
        f"drawbox=x={box_x}:y={box_y + box_h}:w={box_w}:h=6:color={TARGET_COLOR}@1.0:t=fill",
    ]
    for i, line in enumerate(lines):
        parts.append(
            f"drawtext=fontfile={FONT_PATH}:text='{esc_drawtext(line)}'"
            f":expansion=none"
            f":fontcolor={TARGET_COLOR}:fontsize={TARGET_FONT_SIZE}"
            f":x=(w-text_w)/2:y={box_y + 14 + TARGET_LINE_H * i}"
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
