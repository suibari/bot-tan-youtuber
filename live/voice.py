"""VOICEVOX による音声合成（ライブ配信からの入り口）。

実装は common/voice.py にある。Shorts の収録と同じものを使う。
このモジュールは `import voice` という既存の呼び出しを壊さないための再輸出。
"""

from common.voice import (  # noqa: F401
    HOOK_VOICE_PARAMS,
    VoicevoxError,
    concat_wavs,
    get_wav_duration,
    health_check,
    make_silence_wav,
    preload_pronunciations,
    synthesize,
    synthesize_lines,
)
