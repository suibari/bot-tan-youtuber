import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"
JST = timezone(timedelta(hours=9))


def load_module(name, path, stubs):
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class BroadcastPrepareTest(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.saved = []
        self.cleaned = []
        self.db_row = None
        self.loaded = None
        self.found = None

        outer = self

        class FakeBroadcast:
            def __init__(self):
                self.broadcast_id = None
                self.url = ""
                self.title = None

            @classmethod
            def load(cls, _broadcast_id):
                return outer.loaded

            @classmethod
            def find_scheduled(cls, _scheduled_start):
                return outer.found

            def create_event(self, title, _description, _scheduled_start):
                self.broadcast_id = "created-id"
                self.url = "https://www.youtube.com/watch?v=created-id"
                self.title = title
                outer.created.append(self.broadcast_id)
                return self

        broadcast_stub = types.ModuleType("broadcast")
        broadcast_stub.Broadcast = FakeBroadcast
        broadcast_stub.cleanup_stale = lambda preserve_ids=(), **_kwargs: self.cleaned.append(list(preserve_ids))

        memory_stub = types.ModuleType("memory")
        memory_stub.ensure_schema = lambda: None
        memory_stub.get_prepared_broadcast = lambda _start: self.db_row
        memory_stub.save_prepared_broadcast = lambda *args: self.saved.append(args)

        schedule_stub = types.ModuleType("schedule")
        schedule_stub.today_schedule = lambda _now=None: (
            datetime(2026, 8, 22, 21, tzinfo=JST),
            datetime(2026, 8, 22, 22, tzinfo=JST),
        )
        schedule_stub.broadcast_text = lambda _start: ("today title", "description")

        self.module = load_module(
            "prepare_broadcast_test_module",
            LIVE / "prepare_broadcast.py",
            {"broadcast": broadcast_stub, "memory": memory_stub, "schedule": schedule_stub},
        )
        self.FakeBroadcast = FakeBroadcast

    def event(self, broadcast_id):
        value = self.FakeBroadcast()
        value.broadcast_id = broadcast_id
        value.url = f"https://www.youtube.com/watch?v={broadcast_id}"
        value.title = "today title"
        return value

    def test_first_run_creates_and_saves_event(self):
        event = self.module.prepare_today()
        self.assertEqual(event.broadcast_id, "created-id")
        self.assertEqual(self.created, ["created-id"])
        self.assertEqual(self.saved[0][0], "created-id")
        self.assertEqual(self.cleaned, [["created-id"]])

    def test_second_run_reuses_database_event(self):
        self.db_row = {"broadcast_id": "db-id"}
        self.loaded = self.event("db-id")
        event = self.module.prepare_today()
        self.assertEqual(event.broadcast_id, "db-id")
        self.assertEqual(self.created, [])

    def test_recovers_youtube_event_when_database_row_is_missing(self):
        self.found = self.event("youtube-id")
        event = self.module.prepare_today()
        self.assertEqual(event.broadcast_id, "youtube-id")
        self.assertEqual(self.saved[0][0], "youtube-id")

    def test_database_save_failure_is_reported(self):
        self.module.memory.save_prepared_broadcast = lambda *_args: (_ for _ in ()).throw(
            RuntimeError("database down")
        )
        with self.assertRaisesRegex(RuntimeError, "database down"):
            self.module.prepare_today()


class CleanupStaleTest(unittest.TestCase):
    def test_cleanup_preserves_current_and_removes_only_owned_stale_event(self):
        deleted = []
        items = [
            {"id": "current", "status": {"lifeCycleStatus": "created"},
             "snippet": {"title": "【全肯定botたん】今日",
                         "scheduledStartTime": "2026-08-22T12:00:00Z"}},
            {"id": "old", "status": {"lifeCycleStatus": "ready"},
             "snippet": {"title": "【全肯定botたん】昨日",
                         "scheduledStartTime": "2026-08-21T12:00:00Z"}},
            {"id": "future", "status": {"lifeCycleStatus": "ready"},
             "snippet": {"title": "【全肯定botたん】明日",
                         "scheduledStartTime": "2026-08-23T12:00:00Z"}},
            {"id": "foreign", "status": {"lifeCycleStatus": "ready"},
             "snippet": {"title": "別の配信",
                         "scheduledStartTime": "2026-08-21T12:00:00Z"}},
        ]

        broadcasts = types.SimpleNamespace(
            list=lambda **_kwargs: Request({"items": items}),
            delete=lambda **kwargs: Request(deleted.append(kwargs["id"]) or {}),
        )
        client = types.SimpleNamespace(liveBroadcasts=lambda: broadcasts)
        config_stub = types.ModuleType("config")
        config_stub.YOUTUBE_PRIVACY = "public"
        auth_stub = types.ModuleType("youtube_auth")
        auth_stub.get_client = lambda: client
        module = load_module(
            "broadcast_cleanup_test_module",
            LIVE / "broadcast.py",
            {"config": config_stub, "youtube_auth": auth_stub},
        )

        removed = module.cleanup_stale(
            preserve_ids=["current"],
            scheduled_before=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(removed, 1)
        self.assertEqual(deleted, ["old"])


class BroadcastSplitTest(unittest.TestCase):
    def load_broadcast(self, client, name):
        config_stub = types.ModuleType("config")
        config_stub.YOUTUBE_PRIVACY = "public"
        auth_stub = types.ModuleType("youtube_auth")
        auth_stub.get_client = lambda: client
        return load_module(
            name,
            LIVE / "broadcast.py",
            {"config": config_stub, "youtube_auth": auth_stub},
        )

    def test_event_creation_does_not_create_stream_until_live_preparation(self):
        calls = {"event": 0, "stream": 0, "bind": 0}

        class Broadcasts:
            def insert(self, **_kwargs):
                calls["event"] += 1
                return Request({"id": "event-id", "snippet": {}})

            def bind(self, **_kwargs):
                calls["bind"] += 1
                return Request({})

            def list(self, **_kwargs):
                return Request({"items": [{"snippet": {"liveChatId": "chat-id"}}]})

        class Streams:
            def insert(self, **_kwargs):
                calls["stream"] += 1
                return Request({
                    "id": "stream-id",
                    "cdn": {"ingestionInfo": {
                        "ingestionAddress": "rtmp://example",
                        "streamName": "secret-key",
                    }},
                })

        client = types.SimpleNamespace(
            liveBroadcasts=lambda: Broadcasts(),
            liveStreams=lambda: Streams(),
        )
        module = self.load_broadcast(client, "broadcast_split_test_module")
        event = module.Broadcast().create_event(
            "title", "description", datetime(2026, 8, 22, 21, tzinfo=JST),
        )
        self.assertEqual(calls, {"event": 1, "stream": 0, "bind": 0})

        event.create_stream_and_bind()
        self.assertEqual(calls, {"event": 1, "stream": 1, "bind": 1})
        self.assertEqual(event.ingestion_address, "rtmp://example")
        self.assertEqual(event.stream_name, "secret-key")

    def test_youtube_api_failure_is_not_hidden(self):
        broadcasts = types.SimpleNamespace(
            insert=lambda **_kwargs: Request(RuntimeError("youtube unavailable")),
        )
        client = types.SimpleNamespace(liveBroadcasts=lambda: broadcasts)
        module = self.load_broadcast(client, "broadcast_failure_test_module")
        with self.assertRaisesRegex(RuntimeError, "youtube unavailable"):
            module.Broadcast().create_event(
                "title", "description", datetime(2026, 8, 22, 21, tzinfo=JST),
            )


if __name__ == "__main__":
    unittest.main()
