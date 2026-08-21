"""YouTube Live の配信枠を作って状態を遷移させる。

liveBroadcasts / liveStreams / bind / transition の一連を扱う。
OBS へ渡す RTMP の URL とキーもここから取る。

enableAutoStart / enableAutoStop は使わない。21:00 ちょうどに live へ遷移させ、
22:00 に complete させたいので、遷移はこちらから明示的に行う。
"""

from datetime import datetime, timedelta, timezone

from config import YOUTUBE_PRIVACY
from youtube_auth import get_client

JST = timezone(timedelta(hours=9))


# 配信枠のタイトルの決まり文句。後片付けで自分の作った枠だけを狙うために使う
TITLE_MARK = "【全肯定botたん】"


def cleanup_stale(title_mark: str = TITLE_MARK) -> int:
    """一度も live まで行かなかった自分の配信枠を消す。

    配信開始に失敗すると枠だけが `ready` のまま YouTube に残る。放っておくと
    毎晩ぶん溜まっていくので、新しい枠を作る前に掃除する。

    消すのは lifeCycleStatus が created / ready のものだけ。live や complete、
    つまり実際に配信した枠には触らない。タイトルも見て、このパイプラインが
    作った枠以外は対象外にする。
    """
    yt = get_client()
    removed = 0
    res = yt.liveBroadcasts().list(part="id,snippet,status",
                                   broadcastStatus="upcoming",
                                   broadcastType="all", maxResults=50).execute()
    for b in res.get("items", []):
        if b["status"]["lifeCycleStatus"] not in ("created", "ready"):
            continue
        if title_mark not in b["snippet"].get("title", ""):
            continue
        try:
            yt.liveBroadcasts().delete(id=b["id"]).execute()
            print(f"[YouTube] 配信されなかった枠を片付けました: {b['id']} "
                  f"{b['snippet']['title'][:40]}")
            removed += 1
        except Exception as e:
            print(f"[YouTube] {b['id']} を消せませんでした: {e}")
    return removed


class Broadcast:
    """1回ぶんの配信枠。"""

    def __init__(self):
        self.broadcast_id = None
        self.stream_id = None
        self.live_chat_id = None
        self.ingestion_address = None
        self.stream_name = None
        self.title = None
        # 一度でも live まで行ったか。行っていない枠に complete を投げると
        # YouTube が「Invalid transition」で 403 を返す
        self.went_live = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.broadcast_id}" if self.broadcast_id else ""

    def create(self, title: str, description: str, scheduled_start: datetime) -> "Broadcast":
        """配信枠とストリームを作って紐づける。"""
        yt = get_client()
        self.title = title

        res = yt.liveBroadcasts().insert(
            part="snippet,status,contentDetails",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                    "scheduledStartTime": scheduled_start.astimezone(timezone.utc)
                                                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "status": {
                    "privacyStatus": YOUTUBE_PRIVACY,
                    "selfDeclaredMadeForKids": False,
                },
                "contentDetails": {
                    # 遷移はこちらで制御する
                    "enableAutoStart": False,
                    "enableAutoStop": False,
                    "enableDvr": True,
                    "recordFromStart": True,
                    # ライブチャットが無いと配信の意味がない
                    "enableLiveChat": True,
                    "latencyPreference": "low",
                },
            },
        ).execute()
        self.broadcast_id = res["id"]
        self.live_chat_id = res.get("snippet", {}).get("liveChatId")
        print(f"[YouTube] 配信枠を作成: {self.url}")

        res = yt.liveStreams().insert(
            part="snippet,cdn,contentDetails",
            body={
                "snippet": {"title": f"{title} (stream)"},
                "cdn": {
                    "frameRate": "30fps",
                    "ingestionType": "rtmp",
                    "resolution": "1080p",
                },
                "contentDetails": {"isReusable": True},
            },
        ).execute()
        self.stream_id = res["id"]
        ingestion = res["cdn"]["ingestionInfo"]
        self.ingestion_address = ingestion["ingestionAddress"]
        self.stream_name = ingestion["streamName"]
        print(f"[YouTube] ストリームを作成: {self.stream_id}")

        yt.liveBroadcasts().bind(
            part="id,contentDetails",
            id=self.broadcast_id,
            streamId=self.stream_id,
        ).execute()
        print("[YouTube] 配信枠とストリームを紐づけました")

        # bind 後でないと liveChatId が入らないことがあるので取り直す
        if not self.live_chat_id:
            self.live_chat_id = self._fetch_live_chat_id()
        return self

    def _fetch_live_chat_id(self) -> str:
        yt = get_client()
        res = yt.liveBroadcasts().list(part="snippet", id=self.broadcast_id).execute()
        items = res.get("items", [])
        return items[0]["snippet"].get("liveChatId") if items else None

    def stream_status(self) -> str:
        """取り込み側の状態。OBS が送り始めると active になる。"""
        yt = get_client()
        res = yt.liveStreams().list(part="status", id=self.stream_id).execute()
        items = res.get("items", [])
        return items[0]["status"]["streamStatus"] if items else "unknown"

    def wait_for_ingestion(self, timeout: float = 120.0) -> bool:
        """OBS からの映像が届き始めるまで待つ。

        active になる前に testing へ遷移させると YouTube に拒否されるので、
        必ずここを通してから遷移すること。
        """
        import time
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status = self.stream_status()
            if status != last:
                print(f"[YouTube] 取り込み状態: {status}")
                last = status
            if status == "active":
                return True
            time.sleep(3)
        print(f"[YouTube] {timeout:.0f}秒待っても映像が届きません")
        return False

    def transition(self, status: str) -> bool:
        """testing / live / complete へ遷移する。"""
        yt = get_client()
        try:
            yt.liveBroadcasts().transition(
                part="id,status", id=self.broadcast_id, broadcastStatus=status,
            ).execute()
            print(f"[YouTube] {status} へ遷移しました")
            return True
        except Exception as e:
            print(f"[YouTube] {status} への遷移に失敗: {e}")
            return False

    def go_live(self) -> bool:
        """testing を経て live にする。"""
        if not self.transition("testing"):
            return False
        import time
        time.sleep(5)
        if not self.transition("live"):
            return False
        self.went_live = True
        return True

    def finish(self) -> bool:
        """配信を終了状態にする。

        一度も live まで行っていない枠に complete を投げると YouTube は
        「Invalid transition」で 403 を返す。配信開始に失敗したときに
        後片付けのログがエラーで埋まるので、行った場合だけ遷移させる。
        """
        if not self.went_live:
            print("[YouTube] live まで行っていないので complete への遷移は行いません"
                  f"（枠は残ります: {self.url}）")
            return False
        return self.transition("complete")
