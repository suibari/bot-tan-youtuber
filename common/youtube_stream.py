"""YouTube公式gRPC streamList。RESTポーリングへの自動切り戻しはしない。"""

import threading

import grpc
from google.protobuf.json_format import MessageToDict

from common.youtube_auth import get_credentials
from common.youtube_stream_proto import stream_list_pb2 as pb


def response_dict(response) -> dict:
    """protoのenumをRESTと同じ名前に揃え、既存のコメント処理へ渡す。"""
    result = MessageToDict(response)
    for item in result.get("items", []):
        snippet = item.get("snippet", {})
        kind = snippet.get("type", "")
        if isinstance(kind, str):
            words = kind.lower().split("_")
            snippet["type"] = words[0] + "".join(w.title() for w in words[1:])
    return result


class LiveChatStream:
    """接続ごとにOAuthを確認し、stop時は受信待ちもキャンセルする。"""

    # 認証の再確認とハングした接続の回収。無通信でも毎秒再接続しない。
    CONNECTION_SEC = 1800

    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False
        self._call = None
        self._channel = None
        self.force_refresh = False

    def close(self):
        with self._lock:
            self._closed = True
            call, channel = self._call, self._channel
        if call is not None:
            call.cancel()
        if channel is not None:
            channel.close()

    def responses(self, live_chat_id: str, page_token: str | None):
        # RESTクライアントのhttplib2接続を別スレッドで共有しない。
        credentials = get_credentials(force_refresh=self.force_refresh)
        self.force_refresh = False
        request = pb.LiveChatMessageListRequest(
            live_chat_id=live_chat_id, part=["id", "snippet", "authorDetails"])
        if page_token:
            request.page_token = page_token
        channel = grpc.secure_channel("youtube.googleapis.com:443",
                                      grpc.ssl_channel_credentials())
        call = None
        try:
            with self._lock:
                if self._closed:
                    return
                self._channel = channel
                rpc = channel.unary_stream(
                    "/youtube.api.v3.V3DataLiveChatMessageService/StreamList",
                    request_serializer=pb.LiveChatMessageListRequest.SerializeToString,
                    response_deserializer=pb.LiveChatMessageListResponse.FromString)
                call = rpc(request, metadata=(("authorization", f"Bearer {credentials.token}"),),
                           timeout=self.CONNECTION_SEC)
                self._call = call
            for response in call:
                yield response_dict(response)
        finally:
            if call is not None:
                call.cancel()
            channel.close()
            with self._lock:
                self._call = None
                self._channel = None
