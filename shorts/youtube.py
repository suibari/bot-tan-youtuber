#!/usr/bin/env python3
"""
YouTube API 関連処理モジュール

- OAuth2認証・トークン管理
- 動画アップロード
- コメント・視聴統計取得
- DB記録・Discord通知
"""

import os
import re
import sys
import time
import json
import random
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from psycopg2.extras import RealDictCursor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import notify as _notify                  # noqa: E402
from common import youtube_auth as _youtube_auth      # noqa: E402
from common.db import DB_CONFIG, connect_raw          # noqa: E402,F401

# SNSコーナーのcorner_name。"BlueskyCorner"はNagi移行前に保存された過去データとの互換用
SNS_CORNER_NAMES = ("NagiCorner", "BlueskyCorner")


def _get_youtube_client():
    """YouTube API クライアントを返す（OAuth2認証）。失敗時は None。

    人が居る前提の対話フローに入りうる（トークンが失効していたとき）。
    従来どおり失敗しても例外にせず None を返し、呼び出し側が投稿をスキップする。
    """
    try:
        return _youtube_auth.get_client(interactive=True)
    except ImportError:
        print("[YouTube] google-api-python-client未インストール。")
        return None
    except Exception as e:
        print(f"[YouTube] 認証失敗: {e}")
        return None


def fetch_youtube_comments() -> list[dict]:
    """前日のYouTube動画へのコメントをランダム3件取得する。取得失敗時は []。"""
    try:
        conn = connect_raw()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT url FROM affirmative_bot.youtube_shorts ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            print("[コメント] 前日動画なし → CommentCornerスキップ")
            return []

        video_id = parse_qs(urlparse(row["url"]).query).get("v", [None])[0]
        if not video_id:
            print("[コメント] video_id抽出失敗")
            return []

        youtube = _get_youtube_client()
        if youtube is None:
            return []

        result = youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=50,
            order="relevance",
            textFormat="plainText",
        ).execute()

        comments = [
            {
                "text":   item["snippet"]["topLevelComment"]["snippet"]["textDisplay"].strip(),
                "author": item["snippet"]["topLevelComment"]["snippet"]["authorDisplayName"],
            }
            for item in result.get("items", [])
            if item["snippet"]["topLevelComment"]["snippet"]["textDisplay"].strip()
        ]

        if not comments:
            print("[コメント] コメントなし → CommentCornerスキップ")
            return []

        selected = random.sample(comments, min(3, len(comments)))
        print(f"[コメント] {len(selected)}件取得 (候補{len(comments)}件中)")
        return selected

    except Exception as e:
        print(f"[コメント] 取得失敗: {e} → CommentCornerスキップ")
        return []


# アップロード直後は、動画がまだ処理中でサムネイルAPIから見つからない
# （videoNotFound の404が返る）。待ち時間は毎回違うので、伸ばしながら数回試す。
# 実測: 2026-08-15 の朝版で1回目が即座に404になり、例外が upload_to_youtube を
# 突き抜けて URL が返らなかった。結果、動画は公開されたのに DB にも残らず、
# クイズも消費済みにならないという最悪の不整合になった
THUMBNAIL_RETRY_DELAYS = [5, 10, 20, 30, 60]


def _set_thumbnail(youtube, video_id: str, thumbnail_path: str) -> bool:
    """サムネイルを設定する。失敗しても例外は投げない。

    **サムネイルの失敗でアップロードを失敗扱いにしてはいけない。**
    動画そのものは既に公開されているので、URL を返さないと DB への記録も
    Discord 通知も台帳の消費も飛んでしまう。サムネイルは後から手で設定できる。
    """
    from googleapiclient.http import MediaFileUpload

    for i, delay in enumerate([0] + THUMBNAIL_RETRY_DELAYS):
        if delay:
            print(f"[YouTube] サムネイル設定を{delay}秒後に再試行します")
            time.sleep(delay)
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/png")
            ).execute()
            print("[YouTube] サムネイル設定完了"
                  + (f"（{i}回再試行）" if i else ""))
            return True
        except Exception as e:
            print(f"[YouTube] サムネイル設定に失敗: {e}")

    print(f"[YouTube] サムネイルを設定できませんでした（動画は公開済み）。"
          f"手動で設定してください: {thumbnail_path}")
    return False


def upload_to_youtube(mp4_path: str, title: str, description: str, thumbnail_path: str = "") -> None:
    """YouTube Data API v3で動画をアップロードする"""
    print(f"[YouTube] アップロード中: {title}")
    youtube = _get_youtube_client()
    if youtube is None:
        print("[YouTube] クライアント取得失敗。スキップします。")
        return None

    try:
        from googleapiclient.http import MediaFileUpload

        privacy = os.getenv("YOUTUBE_PRIVACY", "public")
        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": ["botたん", "全肯定", "Nagi", "VTuber"],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            }
        }

        media = MediaFileUpload(mp4_path, mimetype="video/mp4", resumable=True)
        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"[YouTube] アップロード進捗: {int(status.progress() * 100)}%")

        video_id = response['id']
        url = f"https://youtube.com/watch?v={video_id}"

        if thumbnail_path and Path(thumbnail_path).exists():
            _set_thumbnail(youtube, video_id, thumbnail_path)

        print(f"[YouTube] アップロード完了 ({privacy}): {url}")
        return url

    except ImportError:
        print("[YouTube] google-api-python-client未インストール。スキップします。")
        print("pip install google-api-python-client google-auth-oauthlib")


def fetch_recent_corners(limit: int = 2) -> dict:
    """直近N件のcornersからClosingの除外statusを取得する"""
    conn = connect_raw()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT corners FROM affirmative_bot.youtube_shorts
                WHERE corners IS NOT NULL
                ORDER BY id DESC LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()

    excluded_fg = set()
    for row in rows:
        corners_data = row.get("corners") or []
        if isinstance(corners_data, str):
            corners_data = json.loads(corners_data)
        for corner in corners_data:
            status = corner.get("status")
            if not status:
                continue
            if corner.get("corner_name") == "Closing":
                excluded_fg.add(status)
    result = {
        "excluded_first_greeting_statuses": list(excluded_fg),
    }
    print(f"[corners] 除外status: FG={result['excluded_first_greeting_statuses']}")
    return result


def get_recent_video_stats(n: int = 3) -> list[dict]:
    """DBの直近n本の動画について YouTube API で viewCount/commentCount を取得する"""
    conn = connect_raw()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT url FROM affirmative_bot.youtube_shorts ORDER BY id DESC LIMIT %s",
                (n,)
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    yt = _get_youtube_client()
    result = []
    for row in rows:
        vid_match = re.search(r'[?&]v=([^&]+)', row["url"])
        if not vid_match:
            continue
        try:
            resp = yt.videos().list(part="statistics", id=vid_match.group(1)).execute()
            items = resp.get("items", [])
            if items:
                stats = items[0]["statistics"]
                result.append({
                    "view_count":    int(stats.get("viewCount",    0)),
                    "comment_count": int(stats.get("commentCount", 0)),
                })
        except Exception as e:
            print(f"[YouTube API] 動画統計取得失敗: {e}")
    return result


def should_enable_comment_corner(recent_videos: list[dict]) -> bool:
    """直近3本の再生数・コメント数条件を満たす場合のみ True を返す"""
    if len(recent_videos) < 3:
        return False
    avg_views = sum(v["view_count"] for v in recent_videos) / len(recent_videos)
    if avg_views < 200:
        return False
    videos_with_comments = sum(1 for v in recent_videos if v["comment_count"] >= 1)
    if videos_with_comments < 2:
        return False
    return True


def fetch_nagi_corner_context() -> dict:
    """NagiCornerの参考リスト（いいね上位3件）と除外リスト（直近3日間）を取得する"""
    conn = connect_raw()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT url, corners FROM affirmative_bot.youtube_shorts
                WHERE corners IS NOT NULL AND created_at >= NOW() - INTERVAL '14 days'
                ORDER BY created_at DESC
            """)
            videos_14d = cur.fetchall()
            cur.execute("""
                SELECT corners FROM affirmative_bot.youtube_shorts
                WHERE corners IS NOT NULL AND created_at >= NOW() - INTERVAL '3 days'
                ORDER BY created_at DESC
            """)
            videos_3d = cur.fetchall()
    finally:
        conn.close()

    # 除外リスト（直近3日間のNagiCornerテーマ）
    excluded_themes = []
    for row in videos_3d:
        for corner in (row.get("corners") or []):
            if corner.get("corner_name") in SNS_CORNER_NAMES:
                themes = corner.get("theme", [])
                if isinstance(themes, list):
                    excluded_themes.extend(themes)

    # 参考リスト: YouTube APIでいいね数取得 → 上位3件のNagiCornerテーマ
    reference_themes = []
    if videos_14d:
        try:
            yt = _get_youtube_client()
            video_likes = []
            for video in videos_14d:
                vid_match = re.search(r'[?&]v=([^&]+)', video["url"])
                if not vid_match:
                    continue
                try:
                    resp = yt.videos().list(part="statistics", id=vid_match.group(1)).execute()
                    items = resp.get("items", [])
                    if items:
                        like_count = int(items[0]["statistics"].get("likeCount", 0))
                        video_likes.append({"like_count": like_count, "corners": video["corners"]})
                except Exception as e:
                    print(f"[YouTube API] いいね数取得失敗: {e}")
            video_likes.sort(key=lambda x: x["like_count"], reverse=True)
            for v in video_likes[:3]:
                for corner in (v["corners"] or []):
                    if corner.get("corner_name") in SNS_CORNER_NAMES:
                        themes = corner.get("theme", [])
                        if isinstance(themes, list):
                            reference_themes.extend(themes)
        except Exception as e:
            print(f"[YouTube API] 参考リスト取得失敗（スキップ）: {e}")

    result = {
        "reference_nagi_themes": list(dict.fromkeys(reference_themes)),
        "excluded_nagi_themes": list(dict.fromkeys(excluded_themes)),
    }
    print(f"[corners] NagiCorner参考={result['reference_nagi_themes']}, 除外={result['excluded_nagi_themes']}")
    return result


def save_youtube_upload_to_db(url: str, title: str, corners_metadata: list[dict] = None) -> None:
    """YouTube投稿情報をDBのyoutube_shortsテーブルに記録する"""
    print(f"[DB] YouTube投稿情報を記録中: {url}")
    conn = connect_raw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO affirmative_bot.youtube_shorts (url, title, status, corners)
                VALUES (%s, %s, 'new', %s)
                ON CONFLICT (url) DO NOTHING
                """,
                (url, title, json.dumps(corners_metadata or [], ensure_ascii=False)),
            )
        conn.commit()
        print("[DB] youtube_shorts に記録完了")
    finally:
        conn.close()


def notify_discord(yt_url: str, title: str) -> None:
    _notify.youtube_uploaded(yt_url, title)
