"""botたんの記憶をDBから引く。

「botたんRAG」という呼び方をしているが、pgvector も埋め込みテーブルも実在しない。
bsky-affirmative-bot の埋め込みは packages/bot_brain/src/gemini/embeddingTexts.ts で
その場で計算して捨てており、永続化されていない。記憶の実体は affirmative_bot スキーマの
素のRDB なので、ここでは SQL で引いてプロンプトに載せる。

配信ログだけは新設する。置き場所は affirmative_bot ではなく bottan_live スキーマ:
bsky-affirmative-bot の scripts/deploy.sh が drizzle-kit push を自動実行しており、
schemaFilter が ['public','affirmative_bot'] なので、drizzle の定義に無いテーブルを
affirmative_bot に置くと DROP 候補になってしまう。別スキーマなら触られない。
"""

import hashlib
import json
import queue
import threading

from psycopg2.extras import Json, RealDictCursor

# 接続とスキーマ名は common/db.py が原典（Shorts の DB アクセスと共通）
from common.db import LIVE_SCHEMA, connect  # noqa: F401


class BotMemoryWriter:
    """配信のホットパスを止めずに共通bot memoryへ書き込む。"""

    def __init__(self, executor=None):
        self._executor = executor or self._execute
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._warned = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bot-memory-writer")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.join()
        self._stop.set()
        self._queue.put(None)
        self._thread.join(timeout=5)

    def ingest_comment(self, message_id: str, broadcast_id: str, author_id: str,
                       author_name: str, content: str, metadata: dict = None) -> None:
        if message_id and content:
            self._queue.put(("upsert", {
                "message_id": message_id,
                "broadcast_id": broadcast_id,
                "author_id": author_id,
                "content": content,
                "metadata": {"broadcastId": broadcast_id,
                             "authorName": author_name, **(metadata or {})},
            }))

    def update_response(self, message_id: str, response: str) -> None:
        if message_id and response:
            self._queue.put(("response", {"message_id": message_id, "response": response}))

    def tombstone(self, message_id: str) -> None:
        if message_id:
            self._queue.put(("delete", {"message_id": message_id}))

    def _run(self) -> None:
        while True:
            action = self._queue.get()
            try:
                if action is None:
                    return
                self._executor(*action)
                self._warned = False
            except Exception as e:
                if not self._warned:
                    print(f"[memory] 共通記憶へ保存できません（配信は継続します）: {e}")
                    self._warned = True
            finally:
                self._queue.task_done()

    @staticmethod
    def _execute(kind: str, payload: dict) -> None:
        with connect() as conn:
            with conn.cursor() as cur:
                if kind == "upsert":
                    digest = hashlib.sha256(payload["content"].encode("utf-8")).hexdigest()
                    cur.execute("""
                        INSERT INTO affirmative_bot.bot_memory_documents
                            (source_type, source_id, author_id, content, occurred_at,
                             metadata, content_hash, created_at, updated_at)
                        VALUES ('youtube_live_comment', %s, %s, %s, now(),
                                %s, %s, now(), now())
                        ON CONFLICT (source_type, source_id) DO UPDATE SET
                            author_id = EXCLUDED.author_id,
                            content = EXCLUDED.content,
                            metadata = EXCLUDED.metadata,
                            content_hash = EXCLUDED.content_hash,
                            embedding = CASE
                                WHEN affirmative_bot.bot_memory_documents.content_hash
                                     = EXCLUDED.content_hash
                                THEN affirmative_bot.bot_memory_documents.embedding
                                ELSE NULL
                            END,
                            embedding_model = CASE
                                WHEN affirmative_bot.bot_memory_documents.content_hash
                                     = EXCLUDED.content_hash
                                THEN affirmative_bot.bot_memory_documents.embedding_model
                                ELSE NULL
                            END,
                            deleted_at = NULL,
                            updated_at = now()
                    """, (payload["message_id"], payload["author_id"], payload["content"],
                          Json(payload["metadata"]), digest))
                elif kind == "response":
                    cur.execute("""
                        UPDATE affirmative_bot.bot_memory_documents
                        SET bot_response = %s, updated_at = now()
                        WHERE source_type = 'youtube_live_comment' AND source_id = %s
                    """, (payload["response"], payload["message_id"]))
                elif kind == "delete":
                    cur.execute("""
                        UPDATE affirmative_bot.bot_memory_documents
                        SET deleted_at = COALESCE(deleted_at, now()), updated_at = now()
                        WHERE source_type = 'youtube_live_comment' AND source_id = %s
                    """, (payload["message_id"],))
            conn.commit()


def ensure_schema() -> None:
    """配信ログのテーブルを用意する。起動時に1回呼ぶ。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {LIVE_SCHEMA}")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {LIVE_SCHEMA}.comments (
                    id                 serial PRIMARY KEY,
                    broadcast_id       text,
                    author_name        text,
                    author_channel_id  text,
                    comment            text,
                    reply              text,
                    energy_at          integer,
                    created_at         timestamptz NOT NULL DEFAULT now()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS comments_created_at_idx
                ON {LIVE_SCHEMA}.comments (created_at DESC)
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {LIVE_SCHEMA}.broadcasts (
                    id            serial PRIMARY KEY,
                    broadcast_id  text UNIQUE,
                    url           text,
                    title         text,
                    started_at    timestamptz,
                    ended_at      timestamptz,
                    comment_count integer NOT NULL DEFAULT 0,
                    created_at    timestamptz NOT NULL DEFAULT now()
                )
            """)
        conn.commit()
    print(f"[memory] {LIVE_SCHEMA} スキーマを確認しました")


# ── botたんの現在の状態 ──────────────────────────────

def get_biorhythm() -> dict:
    """いまの気分・行動・energy。

    bot_state の energy は 0〜10000 の内部スケールなので 0〜100 に直して返す
    （biorhythm_history 側は最初から 0〜100 で入っており、スケールが揃っていない）。
    """
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT value FROM affirmative_bot.bot_state WHERE key = 'biorhythm'")
            row = cur.fetchone()

    value = (row or {}).get("value") or {}
    if isinstance(value, str):
        value = json.loads(value)

    raw = value.get("energy", 5000)
    return {
        "energy": max(0.0, min(100.0, float(raw) / 100.0)),
        "mood":    value.get("mood", ""),
        "mood_en": value.get("mood_en", ""),
        "status":  value.get("status", ""),
    }


def get_today_activities(limit: int = 8) -> list:
    """今日の行動ログ。フリートークのネタ元になる。"""
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT status, mood, energy, created_at
                FROM affirmative_bot.biorhythm_history
                WHERE created_at >= NOW() - INTERVAL '18 hours'
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]


# ── Bluesky でのやりとり ─────────────────────────────

def get_recent_replies(limit: int = 5) -> list:
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT reply, created_at
                FROM affirmative_bot.replies
                WHERE created_at >= NOW() - INTERVAL '1 days'
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]


def get_nagi_posts(limit: int = 5, min_score: int = 88) -> list:
    """Nagi の高得点ポスト。pipeline.py:125-138 の SQL と同じ条件。"""
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.text AS post_text, s.score, p.record_created_at
                FROM nagi.post_scores s
                JOIN nagi.posts p ON s.post_uri = p.uri
                WHERE s.score >= %s
                  AND p.record_created_at >= NOW() - INTERVAL '1 days'
                  AND p.deleted_at IS NULL
                  AND p.kossori = false
                ORDER BY s.score DESC
                LIMIT %s
            """, (min_score, limit))
            return [dict(r) for r in cur.fetchall()]


def get_latest_short() -> dict:
    """直近に投稿した Shorts。前日の振り返りネタに使う。"""
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT url, title, created_at
                FROM affirmative_bot.youtube_shorts
                ORDER BY id DESC LIMIT 1
            """)
            row = cur.fetchone()
            return dict(row) if row else {}


# ── 配信ログ ─────────────────────────────────────────

def save_comment(broadcast_id: str, author_name: str, author_channel_id: str,
                 comment: str, reply: str, energy_at: float) -> None:
    """コメントと返答を記録する。次回以降の配信で「前回こう言ってたね」に使う。"""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {LIVE_SCHEMA}.comments
                    (broadcast_id, author_name, author_channel_id, comment, reply, energy_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (broadcast_id, author_name, author_channel_id, comment, reply,
                  int(energy_at)))
        conn.commit()


def get_previous_live_highlights(limit: int = 5) -> list:
    """前回までの配信で交わしたやりとり。フリートークのネタになる。"""
    with connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT author_name, comment, reply, created_at
                FROM {LIVE_SCHEMA}.comments
                WHERE created_at < date_trunc('day', now())
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]


def start_broadcast(broadcast_id: str, url: str, title: str) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {LIVE_SCHEMA}.broadcasts (broadcast_id, url, title, started_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (broadcast_id) DO UPDATE
                    SET url = EXCLUDED.url, title = EXCLUDED.title, started_at = now()
            """, (broadcast_id, url, title))
        conn.commit()


def end_broadcast(broadcast_id: str, comment_count: int) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {LIVE_SCHEMA}.broadcasts
                SET ended_at = now(), comment_count = %s
                WHERE broadcast_id = %s
            """, (comment_count, broadcast_id))
        conn.commit()
