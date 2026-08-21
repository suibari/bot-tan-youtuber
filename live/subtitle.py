"""日英字幕の書き出し。

Unity 側では描画せず、OBS のテキストソースに出す。フォントや位置を OBS 側で
調整できて、Unity の工事がゼロで済む。

出し方は2経路ある。

1. **obs-websocket で直接流し込む（速い。これが本命）**
   `set_sink()` で送信関数を登録すると、`show()` がそちらへも流す。反映は即時。
2. **ファイルに書いて OBS に読ませる（保険）**
   常に併用する。websocket が繋がらない・落ちた場合はこちらだけが残る。

2 だけだと発話より字幕が遅れる。OBS の `text_ft2_source_v2` は
`video_tick` の中で**約1秒に1回しか** `stat()` せず、間隔を指定する
プロパティも無い（0〜1.0秒の遅れ）。さらに `st_mtime` は秒解像度なので、
同じ秒に2回差し替えると2回目が丸ごと落ちる。

OBS のテキストソース（ファイル経路）は
  - エンコーディングに厳密（UTF-8 / BOMなし で書くこと）
  - 書き込み途中の中途半端な内容を読むことがある（アトミックに差し替えること）
"""

import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from config import SUBTITLE_JA, SUBTITLE_EN, COMMENTS_TXT, CLOCK_TXT

# obs-websocket へ字幕を流す関数。live.py が OBS に繋がったら差し込む。
# 差し込まれていなければファイル書き込みだけになる（従来どおり動く）
_sink = None


def set_sink(fn) -> None:
    """字幕の即時反映先を登録する。None を渡すと外れる。

    fn は fn(ja: str, en: str) -> None。中で例外を出しても配信は止めない
    （呼び出し側が握りつぶす）。
    """
    global _sink
    _sink = fn


def _write_atomic(path: Path, text: str) -> None:
    """一時ファイルに書いてから os.replace で差し替える。

    直接 open("w") すると、OBS が truncate 直後の空ファイルを読んで
    字幕が一瞬消える。os.replace は同一ファイルシステム上で原子的。
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def show(ja: str = "", en: str = "") -> None:
    """字幕を差し替える。空文字を渡せば消える。

    websocket があればそちらを先に叩く。ファイルは保険として必ず書く
    （OBS が読むのは最大1秒後なので、websocket が生きていれば意味を持たない）。
    """
    ja, en = ja or "", en or ""
    fn = _sink
    if fn is not None:
        try:
            fn(ja, en)
        except Exception as e:
            print(f"[字幕] OBS へ送れません（ファイル経由に任せます）: {e}")
    _write_atomic(SUBTITLE_JA, ja)
    _write_atomic(SUBTITLE_EN, en)


def clear() -> None:
    show("", "")


def write_comments(comments: list, limit: int = 5) -> None:
    """画面右のコメント欄。視聴者が「自分のコメントが読まれた」と分かるようにする。

    comments: [{"author": str, "text": str}, ...]（新しいものが後ろ）
    """
    lines = []
    for c in comments[-limit:]:
        author = (c.get("author") or "").strip()
        text = (c.get("text") or "").strip()
        if text:
            lines.append(f"{author}\n  {text}" if author else f"  {text}")
    _write_atomic(COMMENTS_TXT, "\n".join(lines))


def write_clock(now: datetime = None) -> None:
    """OBS 単体では現在時刻を出せないので、こちらから書く。"""
    _write_atomic(CLOCK_TXT, (now or datetime.now()).strftime("%H:%M"))


class SubtitleScheduler:
    """発話に合わせて字幕を文単位で切り替える。

    voice.synthesize_lines が返す実測尺をそのまま使う。文字数比で割り当てると
    漢字とかなで読み上げ速度が違うぶんズレて、音声と字幕が合わなくなる。

    文は後から足せる（push）。配信では喋り出しを早くするため1文ずつ合成して
    Unity へ流し込んでおり、発話を始める時点では2文目以降の尺がまだ分からない。

    1本の発話につき1スレッド。次の発話が始まるときは前のスレッドを止める
    （止めないと前の字幕が新しい発話に被る）。
    """

    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self._queue = None
        self._done = None

    def begin(self, offset: float = 0.0) -> None:
        """字幕の進行を開始する。呼び出しは発話の開始と同時にすること。

        offset は Unity が実際に再生を始めるまでの遅延ぶん。/status を見て
        発話開始を検知してから呼ぶなら 0 でよい。
        """
        self.stop()
        self._stop = threading.Event()
        self._queue = queue.Queue()
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(self._queue, self._done, offset, self._stop),
            daemon=True, name="subtitle",
        )
        self._thread.start()

    def push(self, line: dict, duration: float) -> None:
        """喋る順に1文ずつ足す。"""
        if self._queue is not None:
            self._queue.put((line, duration))

    def finish(self) -> None:
        """もう文は増えないことを伝える。最後の文が終わったら字幕は消える。"""
        if self._done is not None:
            self._done.set()

    def start(self, lines: list, durations: list, offset: float = 0.0) -> None:
        """全文ぶんまとめて渡す従来の入口。"""
        self.begin(offset)
        for i, line in enumerate(lines):
            self.push(line, durations[i] if i < len(durations) else 3.0)
        self.finish()

    def stop(self) -> None:
        """進行を止めて字幕を消す。"""
        if self._thread and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=1.0)
        self._thread = None
        self._queue = None
        self._done = None

    @staticmethod
    def _run(q: "queue.Queue", done: threading.Event,
             offset: float, stop: threading.Event) -> None:
        if offset > 0 and stop.wait(offset):
            return

        start = time.monotonic()
        elapsed = 0.0
        while not stop.is_set():
            try:
                line, duration = q.get(timeout=0.2)
            except queue.Empty:
                if done.is_set():
                    break
                continue

            show(line.get("ja", ""), line.get("en", ""))

            # 累積で待つ。1文ずつ sleep すると書き込みのぶんだけ後ろへずれていく。
            # 合成が音声の再生に間に合わなかった場合は累積が過去になるので、
            # そのぶんは詰めずに次の文の尺だけ待つ
            elapsed = max(elapsed + duration, time.monotonic() - start + duration)
            remaining = elapsed - (time.monotonic() - start)
            if remaining > 0 and stop.wait(remaining):
                break

        # 発話が終わったら消す。字幕枠が出しっぱなしだと画面がうるさい
        clear()
