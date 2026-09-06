"""streamListの再接続・重複排除・キャンセルとwire形式の回帰テスト。"""

import importlib.util
import itertools
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import grpc

from common import youtube_stream as wire
from common.youtube_stream_proto import stream_list_pb2 as pb

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("chat_stream_test_subject", ROOT / "live/chat.py")
chat = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = chat
with patch.dict(sys.modules, {
    "config": types.SimpleNamespace(COMMENT_MAX_AGE_SEC=180, COMMENT_USER_COOLDOWN_SEC=60,
                                    DRY_RUN=False, FAKE_COMMENTS=""),
    "safety": types.SimpleNamespace(sanitize_comment=lambda text: (True, text, "")),
    "subtitle": types.SimpleNamespace(write_comments=lambda _: None),
}):
    spec.loader.exec_module(chat)


def message(mid="m1", kind="textMessageEvent"):
    return {"id": mid, "snippet": {"type": kind, "displayMessage": "こんにちは",
            "publishedAt": (datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat()},
            "authorDetails": {"displayName": "視聴者", "channelId": "c1"}}


class RpcFailure(grpc.RpcError):
    def __init__(self, code): self._code = code
    def code(self): return self._code


class StopAfterWait:
    def __init__(self, limit=1): self.waits = []; self.limit = limit
    def is_set(self): return len(self.waits) >= self.limit
    def wait(self, seconds): self.waits.append(seconds)


class ChatStreamTest(unittest.TestCase):
    def make(self, **kwargs):
        return chat.ChatPoller("chat-id", chat.CommentQueue(), **kwargs)

    def test_resume_replay_does_not_duplicate_reply(self):
        received = []
        poller = self.make(on_comment=received.append)
        calls = []

        def responses(cid, token):
            calls.append((cid, token))
            if len(calls) == 1:
                yield {"items": [message()], "nextPageToken": "next-1"}
                raise RpcFailure(grpc.StatusCode.UNAVAILABLE)
            yield {"items": [message(), message("m2")], "nextPageToken": "next-2"}
            yield {"items": [], "offlineAt": "2026-09-06T13:00:00Z"}

        poller._stop = StopAfterWait(limit=10)
        poller._receive(types.SimpleNamespace(responses=responses))
        self.assertEqual(calls, [("chat-id", None), ("chat-id", "next-1")])
        self.assertEqual([c.message_id for c in received], ["m1", "m2"])
        self.assertEqual(poller.queue.total_received, 2)
        self.assertGreaterEqual(received[0].delivery_delay_sec, 2)
        self.assertEqual(poller._page_token, "next-2")

    def test_quota_backoff_survives_brief_success(self):
        poller = self.make()
        poller._stop = StopAfterWait(limit=3)
        def responses(*_):
            yield {"items": [], "nextPageToken": "next"}
            raise RpcFailure(grpc.StatusCode.RESOURCE_EXHAUSTED)
        poller._receive(types.SimpleNamespace(responses=responses))
        self.assertEqual(poller._stop.waits, [30, 60, 120])

    def test_auth_failure_forces_refresh_next_connection(self):
        poller = self.make()
        poller._stop = StopAfterWait()
        transport = Mock()
        transport.responses.side_effect = RpcFailure(grpc.StatusCode.UNAUTHENTICATED)
        poller._receive(transport)
        self.assertIs(transport.force_refresh, True)

    def test_terminal_errors_do_not_retry(self):
        for code in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.NOT_FOUND,
                     grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.INVALID_ARGUMENT):
            with self.subTest(code=code):
                poller = self.make()
                poller._stop = StopAfterWait()
                transport = Mock()
                transport.responses.side_effect = RpcFailure(code)
                poller._receive(transport)
                self.assertEqual(poller._stop.waits, [])

    def test_idle_disconnect_backs_off_instead_of_spinning(self):
        poller = self.make()
        poller._stop = StopAfterWait(3)
        poller._receive(types.SimpleNamespace(responses=lambda *_: iter(())))
        self.assertEqual(poller._stop.waits, [1, 2, 4])

    def test_long_silent_connection_resets_backoff(self):
        # コメントが来ないまま接続上限で切れても、次の接続を待たせない。
        poller = self.make()
        poller._stop = StopAfterWait(2)
        clock = itertools.count(step=poller.STABLE_CONNECTION_SEC)
        def responses(*_):
            raise RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)
            yield  # ジェネレータにするためだけ。到達しない
        with patch.object(chat.time, "monotonic", lambda: next(clock)):
            poller._receive(types.SimpleNamespace(responses=responses))
        self.assertEqual(poller._stop.waits, [1, 1])

    def test_end_event_stops_without_accepting_later_items(self):
        poller = self.make()
        self.assertFalse(poller._handle_response({"items": [message(kind="chatEndedEvent"), message()]}))
        self.assertEqual(len(poller.queue), 0)

    def test_tombstone_after_seen_message_reaches_delete_callback(self):
        deleted = []
        poller = self.make(on_delete=deleted.append)
        poller._accept(message())
        poller._accept(message(kind="tombstone"))
        self.assertEqual(deleted, ["m1"])
        self.assertEqual(poller.queue.total_received, 1)

    def test_seen_ids_are_bounded(self):
        poller = self.make()
        poller.SEEN_LIMIT = 2
        for mid in ("1", "2", "3"): poller._accept(message(mid))
        self.assertEqual(poller._seen_ids, {"2", "3"})

    def test_stop_closes_transport_before_join(self):
        poller = self.make()
        order = []
        poller._transport = Mock(close=lambda: order.append("close"))
        poller._thread = Mock(join=lambda **_: order.append("join"))
        poller.stop()
        self.assertEqual(order, ["close", "join"])


class TransportTest(unittest.TestCase):
    def test_proto_conversion_keeps_author_and_superchat(self):
        response = pb.LiveChatMessageListResponse(next_page_token="next")
        item = response.items.add(id="paid")
        item.snippet.type = pb.LiveChatMessageSnippet.TypeWrapper.SUPER_CHAT_EVENT
        item.snippet.super_chat_details.user_comment = "応援しています"
        item.author_details.is_chat_sponsor = True
        item.author_details.channel_id = "viewer"
        res = wire.response_dict(pb.LiveChatMessageListResponse.FromString(response.SerializeToString()))
        self.assertEqual(res["nextPageToken"], "next")
        self.assertEqual(res["items"][0]["snippet"]["type"], "superChatEvent")
        accepted = []
        poller = chat.ChatPoller("chat", chat.CommentQueue(), on_comment=accepted.append)
        poller._handle_response(res)
        self.assertTrue(accepted[0].is_super_chat)
        self.assertTrue(accepted[0].is_member)
        self.assertEqual(accepted[0].text, "応援しています")

    def test_wire_path_request_and_cleanup(self):
        response = pb.LiveChatMessageListResponse(next_page_token="after")
        call = Mock()
        call.__iter__ = Mock(return_value=iter([response]))
        rpc = Mock(return_value=call)
        channel = Mock(unary_stream=Mock(return_value=rpc))
        stream = wire.LiveChatStream()
        stream.force_refresh = True
        with patch.object(wire, "get_credentials", return_value=types.SimpleNamespace(token="test-token")) as auth, \
             patch.object(wire.grpc, "secure_channel", return_value=channel):
            self.assertEqual(list(stream.responses("chat", "before"))[0]["nextPageToken"], "after")
        auth.assert_called_once_with(force_refresh=True)
        request = rpc.call_args.args[0]
        self.assertEqual(request.page_token, "before")
        self.assertEqual(list(request.part), ["id", "snippet", "authorDetails"])
        self.assertEqual(channel.unary_stream.call_args.args[0],
                         "/youtube.api.v3.V3DataLiveChatMessageService/StreamList")
        call.cancel.assert_called_once()
        channel.close.assert_called_once()

    def test_close_unblocks_idle_stream(self):
        cancelled = threading.Event()
        entered = threading.Event()
        class Call:
            def __iter__(self):
                entered.set()
                cancelled.wait(3)
                return iter(())
            def cancel(self): cancelled.set()
        channel = Mock(unary_stream=Mock(return_value=Mock(return_value=Call())))
        stream = wire.LiveChatStream()
        with patch.object(wire, "get_credentials", return_value=types.SimpleNamespace(token="test")), \
             patch.object(wire.grpc, "secure_channel", return_value=channel):
            thread = threading.Thread(target=lambda: list(stream.responses("chat", None)))
            thread.start()
            self.assertTrue(entered.wait(2))
            stream.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
