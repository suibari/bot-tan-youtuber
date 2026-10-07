#!/usr/bin/env python3
"""朝版・夜版の再生数を週ごとにまとめる（手動で使う）。

2026-09-14 から Shorts フィードに乗らなくなり、朝夜とも1本あたり0〜3回に落ちた。
隔日化（shorts/turn.py）と配色・台詞の抽選で戻るかを、ここで週ごとに追う。

    ./venv/bin/python tools/shorts_stats.py            # 直近8週
    ./venv/bin/python tools/shorts_stats.py --weeks 20

API 呼び出しはチャンネル1回 + アップロード一覧 (本数/50) 回 + 動画情報 (本数/50) 回。
いまの OAuth スコープ（Data API）で取れるのは再生数・高評価まで。
流入元（ショートフィードが戻ったか）は YouTube Studio で見ること。
"""

import argparse
import datetime
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import youtube_auth  # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))


def kind_of(title: str) -> str | None:
    """タイトルの末尾で見分ける（shorts/description.py の build_*title）。"""
    if title.endswith("/ 朝の勘違いクイズ"):
        return "朝"
    if title.endswith("/ 全肯定botたん"):
        return "夜"
    return None


def fetch_videos(yt, since: datetime.datetime) -> list[dict]:
    uploads = yt.channels().list(part="contentDetails", mine=True).execute()[
        "items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while True:
        page = yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                       maxResults=50, pageToken=token).execute()
        items = page["items"]
        ids += [i["contentDetails"]["videoId"] for i in items]
        token = page.get("nextPageToken")
        # 新しい順に並んでいるので、期間より古いものが出たら打ち切る
        oldest = items[-1]["contentDetails"].get("videoPublishedAt", "") if items else ""
        if not token or (oldest and oldest < since.isoformat()):
            break

    videos = []
    for i in range(0, len(ids), 50):
        videos += yt.videos().list(part="snippet,statistics",
                                   id=",".join(ids[i:i + 50])).execute()["items"]
    return videos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weeks", type=int, default=8)
    args = parser.parse_args()

    today = datetime.datetime.now(JST).date()
    first_monday = today - datetime.timedelta(days=today.weekday(), weeks=args.weeks - 1)
    since = datetime.datetime.combine(first_monday, datetime.time(), JST)

    weeks = defaultdict(lambda: defaultdict(list))
    for v in fetch_videos(youtube_auth.get_client(interactive=False), since):
        kind = kind_of(v["snippet"]["title"])
        published = datetime.datetime.fromisoformat(
            v["snippet"]["publishedAt"].replace("Z", "+00:00")).astimezone(JST).date()
        if kind is None or published < first_monday:
            continue
        monday = published - datetime.timedelta(days=published.weekday())
        weeks[monday][kind].append(int(v["statistics"].get("viewCount", 0)))

    print(f"{'週（月曜）':<12}" + "".join(f"{k + '版（本数 中央値 最大 合計）':<28}" for k in ("朝", "夜")))
    for monday in sorted(weeks):
        row = f"{monday.isoformat():<14}"
        for kind in ("朝", "夜"):
            views = sorted(weeks[monday][kind])
            if views:
                cell = f"{len(views)}本  {views[len(views) // 2]}  {views[-1]}  {sum(views)}"
            else:
                cell = "-"
            row += f"{cell:<32}"
        print(row)


if __name__ == "__main__":
    main()
