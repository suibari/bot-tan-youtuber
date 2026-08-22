"""共有DBの発話表記を、VOICEVOXへ渡すテキストだけに適用する。"""

import re
import threading
import time
from typing import Callable, Iterable

from common.env import env_flag, env_float

TTS_PRONUNCIATIONS_ENABLED = env_flag("TTS_PRONUNCIATIONS_ENABLED", True)
TTS_PRONUNCIATION_REFRESH_SEC = max(
    10.0, env_float("TTS_PRONUNCIATION_REFRESH_SEC", 60.0))


def _load_active_pronunciations() -> list[tuple[str, str]]:
    # import時にDBへ接続しない。Shortsのffmpeg再実行など音声を使わない経路を
    # 読み辞書の障害に巻き込まないため、必要になった時点で初めて読む。
    from common.db import connect
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT surface, spoken_form
                FROM affirmative_bot.bot_memory_pronunciations
                WHERE status = 'active' AND spoken_form IS NOT NULL
                ORDER BY char_length(surface) DESC, surface
            """)
            return [(str(surface), str(spoken)) for surface, spoken in cur.fetchall()]


def _compile(entries: Iterable[tuple[str, str]]):
    replacements = {}
    parts = []
    for surface, spoken in sorted(entries, key=lambda item: (-len(item[0]), item[0])):
        surface = surface.strip()
        spoken = spoken.strip()
        if not surface or not spoken or surface in replacements:
            continue
        replacements[surface] = spoken
        escaped = re.escape(surface)
        if surface.isascii() and all(ch.isalnum() or ch == "_" for ch in surface):
            parts.append(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])")
        else:
            parts.append(escaped)
    return (re.compile("|".join(parts)) if parts else None, replacements)


class PronunciationCache:
    def __init__(self, loader: Callable = None, enabled: bool = None,
                 refresh_sec: float = None, clock: Callable = None):
        self.loader = loader or _load_active_pronunciations
        self.enabled = TTS_PRONUNCIATIONS_ENABLED if enabled is None else enabled
        self.refresh_sec = (TTS_PRONUNCIATION_REFRESH_SEC
                            if refresh_sec is None else refresh_sec)
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._pattern = None
        self._replacements = {}
        self._last_attempt = 0.0
        self._attempted = False
        self._refreshing = False
        self._warned = False

    def _refresh(self) -> bool:
        try:
            pattern, replacements = _compile(self.loader())
            with self._lock:
                self._pattern = pattern
                self._replacements = replacements
                self._warned = False
            print(f"[tts-pronunciation] 読み辞書を更新しました: {len(replacements)}件")
            return True
        except Exception as error:
            with self._lock:
                warn = not self._warned
                self._warned = True
            if warn:
                print(f"[tts-pronunciation] 読み辞書を取得できません（既存キャッシュで継続）: {error}")
            return False
        finally:
            with self._lock:
                self._last_attempt = self.clock()
                self._attempted = True
                self._refreshing = False

    def preload(self) -> bool:
        """起動時に同期ロードする。失敗しても呼び出し側を止めない。"""
        if not self.enabled:
            return False
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
        return self._refresh()

    def _refresh_if_stale(self) -> None:
        if not self.enabled:
            return
        now = self.clock()
        with self._lock:
            if self._refreshing or (self._attempted and
                    now - self._last_attempt < self.refresh_sec):
                return
            # 最初の発話だけは読みを確実に適用する。ライブはprepareで先読み済み、
            # Shortsは数秒待てるため、初回だけ同期ロードする。
            blocking = not self._attempted
            self._refreshing = True
        if blocking:
            self._refresh()
        else:
            threading.Thread(
                target=self._refresh, daemon=True,
                name="tts-pronunciation-refresh").start()

    def apply(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        self._refresh_if_stale()
        with self._lock:
            pattern = self._pattern
            replacements = dict(self._replacements)
        if pattern is None:
            return text
        return pattern.sub(lambda match: replacements[match.group(0)], text)


_CACHE = PronunciationCache()


def preload_pronunciations() -> bool:
    return _CACHE.preload()


def apply_pronunciations(text: str) -> str:
    return _CACHE.apply(text)
