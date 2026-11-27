import sqlite3
import types
import unittest
from contextlib import ExitStack, closing, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock

from tests.test_broadcast_prepare import LIVE, Request, load_module


TITLE = "【全肯定botたん】夜のおしゃべり配信"


def make_broadcast(broadcast_id, status="created", title=TITLE, *, bound=False, started=False):
    snippet = {"title": title}
    if started:
        snippet["actualStartTime"] = "2026-11-27T12:00:00Z"
    return {
        "id": broadcast_id,
        "snippet": snippet,
        "status": {"lifeCycleStatus": status},
        "contentDetails": {"boundStreamId": "stream"} if bound else {},
    }


class LiveRetirementTest(unittest.TestCase):
    def setUp(self):
        self.broadcasts = Mock()
        self.client = Mock(return_value=types.SimpleNamespace(liveBroadcasts=lambda: self.broadcasts))
        self.memory = types.SimpleNamespace(
            ensure_schema=Mock(), retire_unstarted_broadcasts=Mock(),
        )
        self.retire = load_module(
            "live_retirement_test_module",
            LIVE / "retire_live.py",
            {
                "config": types.ModuleType("config"),
                "broadcast": types.SimpleNamespace(TITLE_MARK="【全肯定botたん】"),
                "memory": self.memory,
                "youtube_auth": types.SimpleNamespace(get_client=self.client),
            },
        )
        self.stdout = StringIO()
        self.stderr = StringIO()
        output = ExitStack()
        self.addCleanup(output.close)
        output.enter_context(redirect_stdout(self.stdout))
        output.enter_context(redirect_stderr(self.stderr))

    def test_only_matching_unbound_unstarted_db_broadcasts_are_deleted(self):
        items = {
            "unrelated": make_broadcast("unrelated"),
            "created": make_broadcast("created"),
            "ready": make_broadcast("ready", "ready"),
            "renamed": make_broadcast("renamed", title=f"{TITLE} 変更"),
            "wrong-id": make_broadcast("another-id"),
            "testing": make_broadcast("testing", "testing"),
            "test-starting": make_broadcast("test-starting", "testStarting"),
            "live-starting": make_broadcast("live-starting", "liveStarting"),
            "live": make_broadcast("live", "live"),
            "complete": make_broadcast("complete", "complete"),
            "bound": make_broadcast("bound", "ready", bound=True),
            "actual-start": make_broadcast("actual-start", "ready", started=True),
            "missing-details": {"id": "missing-details", "snippet": {"title": TITLE},
                                "status": {"lifeCycleStatus": "created"}},
        }
        self.memory.retire_unstarted_broadcasts.return_value = [
            (broadcast_id, TITLE) for broadcast_id in items if broadcast_id != "unrelated"
        ]
        self.broadcasts.list.side_effect = lambda *, id, part: Request({"items": [items[id]]})
        self.broadcasts.delete.return_value = Request({})

        self.assertEqual(self.retire.main(), 0)
        looked_up_ids = [call.kwargs["id"] for call in self.broadcasts.list.call_args_list]
        self.assertNotIn("unrelated", looked_up_ids)
        self.assertEqual(
            [call.kwargs["id"] for call in self.broadcasts.delete.call_args_list],
            ["created", "ready"],
        )

    def test_partial_failure_reports_error_and_retry_skips_deleted_ids(self):
        items = {key: make_broadcast(key) for key in ("lookup-failure", "delete-failure", "success")}
        self.memory.retire_unstarted_broadcasts.return_value = [(key, TITLE) for key in items]
        attempted = []

        def lookup(*, id, part):
            self.memory.retire_unstarted_broadcasts.assert_called()
            if id == "lookup-failure" and self.memory.retire_unstarted_broadcasts.call_count == 1:
                return Request(RuntimeError("raw API details"))
            return Request({"items": [items[id]] if id in items else []})

        def delete(*, id):
            attempted.append(id)
            if id == "delete-failure" and attempted.count(id) == 1:
                return Request(RuntimeError("raw API details"))
            del items[id]
            return Request({})

        self.broadcasts.list.side_effect = lookup
        self.broadcasts.delete.side_effect = delete
        self.assertEqual(self.retire.main(), 1)
        self.assertEqual(attempted, ["delete-failure", "success"])
        self.assertIn("失敗 2 件", self.stdout.getvalue())
        self.assertNotIn("raw API details", self.stderr.getvalue())
        self.assertEqual(self.retire.main(), 0)
        self.assertEqual(attempted, ["delete-failure", "success", "lookup-failure", "delete-failure"])

    def test_database_failure_or_empty_candidates_skips_youtube(self):
        self.memory.retire_unstarted_broadcasts.side_effect = [RuntimeError("db failure"), []]
        self.assertEqual(self.retire.main(), 1)
        self.assertEqual(self.retire.main(), 0)
        self.client.assert_not_called()


class RetirementDatabaseTest(unittest.TestCase):
    def test_prepared_unstarted_rows_are_retired_and_stay_candidates_for_retry(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("ATTACH DATABASE ':memory:' AS bottan_live")
            conn.execute("""
                CREATE TABLE bottan_live.broadcasts (
                    broadcast_id text, title text, prepared_at text, started_at text,
                    ended_at text, scheduled_start_at text
                )
            """)
            conn.executemany("INSERT INTO bottan_live.broadcasts VALUES (?, ?, ?, ?, ?, ?)", [
                ("past", TITLE, "prepared", None, None, "past"),
                ("future", TITLE, "prepared", None, None, "future"),
                ("retry", TITLE, "prepared", None, "previous-end", "past"),
                ("in-progress", TITLE, "prepared", "started", None, "past"),
                ("completed", TITLE, "prepared", "started", "previous-end", "past"),
                ("unprepared", TITLE, None, None, None, "future"),
                (None, TITLE, "prepared", None, None, "future"),
            ])
            conn.create_function("now", 0, lambda: "closed")
            connection = types.SimpleNamespace(
                cursor=lambda: closing(conn.cursor()), commit=conn.commit,
            )
            db = types.SimpleNamespace(
                LIVE_SCHEMA="bottan_live", connect=lambda: nullcontext(connection),
            )
            memory = load_module(
                "live_retirement_memory_test_module",
                LIVE / "memory.py",
                {
                    "common.db": db,
                    "common.env": types.SimpleNamespace(env_int=lambda name, default: default),
                    "psycopg2.extras": types.SimpleNamespace(Json=object, RealDictCursor=object),
                },
            )

            self.assertCountEqual(memory.retire_unstarted_broadcasts(),
                                  [(key, TITLE) for key in ("past", "future", "retry")])
            conn.create_function("now", 0, lambda: "retried")
            self.assertCountEqual(memory.retire_unstarted_broadcasts(),
                                  [(key, TITLE) for key in ("past", "future", "retry")])
            self.assertEqual(dict(conn.execute("SELECT broadcast_id, ended_at FROM bottan_live.broadcasts")),
                             {"past": "closed", "future": "closed", "retry": "previous-end",
                              "in-progress": None, "completed": "previous-end",
                              "unprepared": None, None: "closed"})
