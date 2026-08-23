"""共有DBの発話表記を、VOICEVOXへ渡すテキストだけに適用する。"""

import re
import threading
import time
from typing import Callable, Iterable

from common.env import env_flag, env_float

TTS_PRONUNCIATIONS_ENABLED = env_flag("TTS_PRONUNCIATIONS_ENABLED", True)
TTS_PRONUNCIATION_REFRESH_SEC = max(
    10.0, env_float("TTS_PRONUNCIATION_REFRESH_SEC", 60.0))

# 表記ゆれとして無視する区切り。
#
# 登録どおりの一字一句でしか置換できないと、実際にはまず当たらない。
# 2026-08-23 の配信では `ファイアーエムブレム万紫千紅` を登録してあったのに
# 「ファイアーエムブレム 万紫千紅」と喋って素読みした。LLM は作品名に空白や
# 読点を入れて書くほうが自然なので、登録側の表記に賭けても外れる。
_SEPARATORS = " \t　・･,、"
_SEP_PATTERN = f"[{re.escape(_SEPARATORS)}]*"


def _norm_key(text: str) -> str:
    """区切りだけを落とした引き当て用のキー。大小は保つ。

    大小まで潰すと `Halo`(ヘイロー) と `halo`(ハロー)、`Uma`(ウーマ) と
    `uma`(ウマ) のように**別語として登録されているもの**が混ざる。
    表記ゆれの吸収は、これで引けなかったときの小文字での引き直しで行う。
    """
    return "".join(ch for ch in text if ch not in _SEPARATORS)


def _char_class(ch: str) -> str:
    """文字種。区切りを差し込んでよい位置を決めるのに使う。

    長音記号 `ー`(U+30FC) はカタカナブロックにあるので、`ファイアー` の途中が
    境界にならない。読みの一部なので、ここで切ってはいけない。
    """
    code = ord(ch)
    if 0x30A0 <= code <= 0x30FF or code == 0xFF70:
        return "katakana"
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF:
        return "kanji"
    if ch.isascii() and (ch.isalnum() or ch == "_"):
        return "ascii"
    return "other"


def _surface_pattern(surface: str) -> str:
    """surface を、区切りをまたいでも当たる正規表現にする。

    区切りを許すのは **文字種が変わる位置と、登録側に区切りがあった位置だけ**。
    どこにでも許すと `アニメ` が「アニ、メート」に当たってしまう。
    """
    parts = []
    prev = ""
    pending_sep = False
    for ch in surface:
        if ch in _SEPARATORS:
            pending_sep = True
            continue
        if parts and (pending_sep or _char_class(prev) != _char_class(ch)):
            parts.append(_SEP_PATTERN)
        parts.append(re.escape(ch))
        prev = ch
        pending_sep = False
    return "".join(parts)


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
    """辞書を1本の正規表現に畳む。

    引き当て表には**大小を保ったキーと、小文字に潰したキーの両方**を入れる。
    先に小文字のぶんを敷き、あとから大小を保ったぶんで上書きするので、
    `Halo`/`halo` のように大小で意味が違う登録はそのまま引けて、
    `Youtube` のような未登録の表記ゆれだけが小文字側に落ちる。

    正規表現は同じものが二度並ばないようにするだけで、登録は全部載せる。
    `Blue Sky` と `Bluesky` のように、区切りの有無だけが違う登録が両方あっても
    どちらの書かれ方でも拾えるようにしておきたい。
    """
    ordered = sorted(entries, key=lambda item: (-len(item[0]), item[0]))
    cleaned = []
    for surface, spoken in ordered:
        surface = surface.strip()
        spoken = spoken.strip()
        key = _norm_key(surface)
        if surface and spoken and key:
            cleaned.append((surface, spoken, key))

    replacements = {}
    for _, spoken, key in cleaned:            # 先に小文字のぶんを敷く
        replacements.setdefault(key.lower(), spoken)
    for _, spoken, key in cleaned:            # 大小を保ったぶんで上書きする
        replacements[key] = spoken

    parts = []
    seen = set()
    for surface, _, _ in cleaned:
        body = _surface_pattern(surface)
        # 英数字だけの surface は前後に語境界を要求する。`AI` が `AIR` に
        # 当たると読みが壊れる
        if surface.isascii() and all(
                ch.isalnum() or ch == "_" or ch in _SEPARATORS for ch in surface):
            body = rf"(?<![A-Za-z0-9_]){body}(?![A-Za-z0-9_])"
        if body not in seen:
            seen.add(body)
            parts.append(body)

    # ASCII の表記ゆれ（YouTube / youtube / Youtube）を拾う。日本語には効かない
    pattern = re.compile("|".join(parts), re.IGNORECASE) if parts else None
    return (pattern, replacements)


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
            entries = list(self.loader())
            pattern, replacements = _compile(entries)
            with self._lock:
                self._pattern = pattern
                self._replacements = replacements
                self._warned = False
            # 数えるのは登録の件数。replacements には大小2通りのキーが入るので、
            # そちらを数えると実際の倍近くに見える
            print(f"[tts-pronunciation] 読み辞書を更新しました: {len(entries)}件")
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
        # マッチした文字列と登録キーは一致しない（区切りと大小を吸収しているため）。
        # 区切りを落として引き、駄目なら小文字で引き直す。どちらでも引けなければ
        # 元の表記のまま返す（配信を止めない）
        def replace(match):
            found = match.group(0)
            key = _norm_key(found)
            return replacements.get(key) or replacements.get(key.lower()) or found

        return pattern.sub(replace, text)


_CACHE = PronunciationCache()


def preload_pronunciations() -> bool:
    return _CACHE.preload()


def apply_pronunciations(text: str) -> str:
    return _CACHE.apply(text)
