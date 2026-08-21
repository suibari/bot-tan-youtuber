"""YouTube Live の配信枠を作って状態を遷移させる。

liveBroadcasts / liveStreams / bind / transition の一連を扱う。
OBS へ渡す RTMP の URL とキーもここから取る。

enableAutoStart は使わない。21:00 ちょうどに live へ遷移させたいので、開始は
こちらから明示的に行う。終わりは enableAutoStop に任せる（下記 create() 参照）。
"""

import time
from datetime import datetime, timedelta, timezone

from config import YOUTUBE_PRIVACY
from youtube_auth import get_client

JST = timezone(timedelta(hours=9))


# 配信枠のタイトルの決まり文句。後片付けで自分の作った枠だけを狙うために使う
TITLE_MARK = "【全肯定botたん】"

# 片付けてよい枠の状態。live / complete は実際に配信した枠なので触らない。
# testStarting / testing も入れてあるのは、live へ遷移できずに終わった枠が
# ready ではなくこの状態で残るため
STALE_STATUSES = ("created", "ready", "testStarting", "testing")


def cleanup_stale(title_mark: str = TITLE_MARK) -> int:
    """一度も live まで行かなかった自分の配信枠を消す。

    配信開始に失敗すると枠だけが `ready` のまま YouTube に残る。放っておくと
    毎晩ぶん溜まっていくので、新しい枠を作る前に掃除する。

    消すのは lifeCycleStatus が STALE_STATUSES のものだけ。live や complete、
    つまり実際に配信した枠には触らない。タイトルも見て、このパイプラインが
    作った枠以外は対象外にする。
    """
    yt = get_client()
    removed = 0
    res = yt.liveBroadcasts().list(part="id,snippet,status",
                                   broadcastStatus="upcoming",
                                   broadcastType="all", maxResults=50).execute()
    for b in res.get("items", []):
        if b["status"]["lifeCycleStatus"] not in STALE_STATUSES:
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
                    # 開始はこちらで制御する。OBS が送り始めた時点で live に
                    # なってしまうと、20:41 から配信が始まってしまう
                    "enableAutoStart": False,
                    # 終わりは YouTube にも任せる。complete を投げ損ねると
                    # （クォータ切れ、プロセスの異常終了、websocket の切断）
                    # 枠が live のまま残り続けて、真っ暗な配信が終わらない。
                    # OBS が送信を止めたら終わってよい
                    "enableAutoStop": True,
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

    def lifecycle_status(self) -> str:
        """配信枠そのものの状態。

        created → ready → testStarting → testing → liveStarting → live → complete
        と進む。transition() は要求を受け付けるだけで、実際に次の状態になるまでは
        数十秒かかることがある。
        """
        yt = get_client()
        res = yt.liveBroadcasts().list(part="status", id=self.broadcast_id).execute()
        items = res.get("items", [])
        return items[0]["status"]["lifeCycleStatus"] if items else "unknown"

    def wait_for_lifecycle(self, target: str, timeout: float = 180.0) -> bool:
        """lifeCycleStatus が target になるまで待つ。"""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            status = self.lifecycle_status()
            if status != last:
                print(f"[YouTube] 配信枠の状態: {status}")
                last = status
            if status == target:
                return True
            time.sleep(3)
        print(f"[YouTube] {timeout:.0f}秒待っても {target} になりません（今は {last}）")
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

    def start_testing(self, timeout: float = 180.0) -> bool:
        """モニターストリームを立ち上げて testing まで持っていく。

        testing の要求が通っても lifeCycleStatus はいったん testStarting になる。
        そこで live を投げると YouTube は「Invalid transition」で 403 を返すので、
        testing になりきるまでここで待つ。配信開始の前に済ませておくこと。
        """
        status = self.lifecycle_status()
        if status in ("testing", "liveStarting", "live"):
            print(f"[YouTube] すでに {status} なので testing は飛ばします")
            return True
        if not self.transition("testing"):
            return False
        return self.wait_for_lifecycle("testing", timeout)

    def go_live(self, retry_sec: float = 300.0) -> bool:
        """live にする。testing は start_testing() で済ませておく前提。

        まだ testStarting だったり YouTube 側の都合だったりで弾かれることがあるので、
        retry_sec のあいだは 5 秒おきに投げ直す。
        """
        deadline = time.time() + retry_sec
        while True:
            if self.transition("live"):
                break
            if time.time() >= deadline:
                print(f"[YouTube] {retry_sec:.0f}秒粘りましたが live になりません")
                return False
            time.sleep(5)
        if not self.wait_for_lifecycle("live", 60.0):
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
