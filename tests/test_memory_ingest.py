import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "live"))

# このテストは標準ライブラリだけで動かす。配信環境固有のYouTube/DB依存は
# コールバック境界の外なので、import時だけ最小スタブへ差し替える。
#
# **差し替えたら必ず戻すこと。** sys.modules に置きっぱなしにすると、あとから
# 読み込まれるテストまで巻き込む（common.db を残していたせいで、shorts 側の
# core.py / pipeline.py を読む tests/test_subtitle_chunks.py と
# tests/test_night_prompt.py が ImportError になっていた）。
# chat / memory は import 時にスタブを束縛済みなので、戻しても動き続ける。
_STUBS = {}

config_stub = types.ModuleType("config")
config_stub.COMMENT_USER_COOLDOWN_SEC = 60
config_stub.COMMENT_MAX_AGE_SEC = 180
config_stub.DRY_RUN = False
config_stub.FAKE_COMMENTS = ""
_STUBS["config"] = config_stub

safety_stub = types.ModuleType("safety")
safety_stub.sanitize_comment = lambda text: (bool(text), text, "")
_STUBS["safety"] = safety_stub

subtitle_stub = types.ModuleType("subtitle")
subtitle_stub.write_comments = lambda comments: None
_STUBS["subtitle"] = subtitle_stub

youtube_auth_stub = types.ModuleType("youtube_auth")
youtube_auth_stub.get_client = lambda: None
_STUBS["youtube_auth"] = youtube_auth_stub

extras_stub = types.ModuleType("psycopg2.extras")
extras_stub.Json = lambda value: value
extras_stub.RealDictCursor = object
psycopg_stub = types.ModuleType("psycopg2")
psycopg_stub.extras = extras_stub
_STUBS["psycopg2"] = psycopg_stub
_STUBS["psycopg2.extras"] = extras_stub

db_stub = types.ModuleType("common.db")
db_stub.LIVE_SCHEMA = "bottan_live"
db_stub.connect = lambda: None
_STUBS["common.db"] = db_stub

_previous = {key: sys.modules.get(key) for key in _STUBS}
sys.modules.update(_STUBS)
try:
    import chat  # noqa: E402
    import memory  # noqa: E402
finally:
    for _key, _value in _previous.items():
        if _value is None:
            sys.modules.pop(_key, None)
        else:
            sys.modules[_key] = _value


class ChatMemoryIngestTest(unittest.TestCase):
    def test_sanitized_comment_keeps_message_id_and_is_forwarded(self):
        received = []
        poller = chat.ChatPoller("live", chat.CommentQueue(), on_comment=received.append)
        item = {
            "id": "message-1",
            "snippet": {"type": "textMessageEvent", "displayMessage": "こんばんは"},
            "authorDetails": {"displayName": "視聴者", "channelId": "channel-1"},
        }
        with patch.object(chat.subtitle, "write_comments"):
            poller._accept(item)
        self.assertEqual(received[0].message_id, "message-1")
        self.assertEqual(received[0].text, "こんばんは")

    def test_deleted_event_is_forwarded_without_becoming_comment(self):
        deleted = []
        queue = chat.CommentQueue()
        poller = chat.ChatPoller("live", queue, on_delete=deleted.append)
        poller._accept({"snippet": {
            "type": "messageDeletedEvent",
            "messageDeletedDetails": {"deletedMessageId": "message-2"},
        }})
        self.assertEqual(deleted, ["message-2"])
        self.assertEqual(len(queue), 0)

    def test_writer_processes_upsert_response_and_delete_in_order(self):
        actions = []
        writer = memory.BotMemoryWriter(executor=lambda kind, payload: actions.append((kind, payload)))
        writer.start()
        writer.ingest_comment("message-3", "broadcast", "channel", "name", "本文")
        writer.update_response("message-3", "返答")
        writer.tombstone("message-3")
        writer.stop()
        self.assertEqual([kind for kind, _ in actions], ["upsert", "response", "delete"])
        self.assertEqual(actions[0][1]["metadata"]["authorName"], "name")

    def test_writer_failure_does_not_stop_later_actions(self):
        attempts = []

        def execute(kind, payload):
            attempts.append(kind)
            if kind == "upsert":
                raise RuntimeError("schema missing")

        writer = memory.BotMemoryWriter(executor=execute)
        writer.start()
        writer.ingest_comment("message-4", "broadcast", "channel", "name", "本文")
        writer.tombstone("message-4")
        writer.stop()
        self.assertEqual(attempts, ["upsert", "delete"])


class BroadcastMemoryTest(unittest.TestCase):
    class Cursor:
        def __init__(self, row=None):
            self.queries = []
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            self.queries.append((query, params))

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self, cursor):
            self.value = cursor
            self.commits = 0

        def cursor(self, **_kwargs):
            return self.value

        def commit(self):
            self.commits += 1

    @staticmethod
    @contextmanager
    def connection(value):
        yield value

    def test_ensure_schema_adds_schedule_columns_compatibly(self):
        cursor = self.Cursor()
        conn = self.Connection(cursor)
        with patch.object(memory, "connect", lambda: self.connection(conn)):
            memory.ensure_schema()
        sql = "\n".join(query for query, _ in cursor.queries)
        self.assertIn("ADD COLUMN IF NOT EXISTS scheduled_start_at", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS scheduled_end_at", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS prepared_at", sql)
        self.assertEqual(conn.commits, 1)

    def test_prepared_broadcast_upsert_and_lookup(self):
        expected = {
            "broadcast_id": "broadcast-1",
            "url": "https://www.youtube.com/watch?v=broadcast-1",
        }
        cursor = self.Cursor(row=expected)
        conn = self.Connection(cursor)
        with patch.object(memory, "connect", lambda: self.connection(conn)):
            memory.save_prepared_broadcast(
                "broadcast-1", expected["url"], "title", "start", "end",
            )
            row = memory.get_prepared_broadcast("start")
        self.assertEqual(row, expected)
        self.assertTrue(any("ON CONFLICT (broadcast_id)" in query
                            for query, _ in cursor.queries))
        self.assertTrue(any("scheduled_start_at = %s" in query
                            for query, _ in cursor.queries))


if __name__ == "__main__":
    unittest.main()
