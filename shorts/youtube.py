#!/usr/bin/env python3
"""
YouTube API 関連処理モジュール

- OAuth2認証・トークン管理
- 動画アップロード
- DB記録・Discord通知
"""

import os
import sys
import time
import json
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import notify as _notify                  # noqa: E402
from common import youtube_auth as _youtube_auth      # noqa: E402
from common.db import DB_CONFIG, connect_raw          # noqa: E402,F401



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


def _with_retry(label: str, fn) -> bool:
    """アップロード直後の 404 に備えて、THUMBNAIL_RETRY_DELAYS で伸ばしながら試す。

    失敗しても例外は投げない（_set_thumbnail と同じく、動画は既に公開されている）。
    """
    for i, delay in enumerate([0] + THUMBNAIL_RETRY_DELAYS):
        if delay:
            print(f"[YouTube] {label}を{delay}秒後に再試行します")
            time.sleep(delay)
        try:
            fn()
            print(f"[YouTube] {label}完了" + (f"（{i}回再試行）" if i else ""))
            return True
        except Exception as e:
            print(f"[YouTube] {label}に失敗: {e}")
    return False


def _add_english_caption(youtube, video_id: str, srt: str) -> None:
    """英語字幕トラックを付ける。quota は captions.insert の 400。"""
    import io
    from googleapiclient.http import MediaIoBaseUpload

    def caption():
        media = MediaIoBaseUpload(io.BytesIO(srt.encode("utf-8")),
                                  mimetype="application/octet-stream", resumable=False)
        youtube.captions().insert(part="snippet", body={"snippet": {
            "videoId": video_id, "language": "en", "name": "English", "isDraft": False,
        }}, media_body=media).execute()

    _with_retry("英語字幕の追加", caption)


def _localize(body: dict, english: dict) -> dict:
    """insert の body に英語版のタイトル・説明文（localizations）を載せたものを返す。

    **投稿のあとから videos.update で付けてはいけない。** 2026-10-08 の非公開テストで、
    投稿直後（処理中）に update するとタグが消えた。update の応答にはタグが入っていたのに、
    処理が終わると消えていた。処理を待ってから update すれば残るが、Shorts の処理は
    数分かかるので待てない。
    """
    return {
        **body,
        "snippet": {**body["snippet"], "defaultLanguage": "ja"},
        "localizations": {"en": {"title": english["title"],
                                 "description": english["description"]}},
    }


def _insert(youtube, body: dict, mp4_path: str) -> dict:
    from googleapiclient.http import MediaFileUpload

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
    return response


def upload_to_youtube(mp4_path: str, title: str, description: str, thumbnail_path: str = "",
                      english: dict = None) -> None:
    """YouTube Data API v3で動画をアップロードする

    english（{"title", "description", "srt"}）を渡すと、英語版のタイトル・説明文を
    投稿と同時に付け、投稿のあとに英語字幕トラックを足す。
    """
    print(f"[YouTube] アップロード中: {title}")
    youtube = _get_youtube_client()
    if youtube is None:
        print("[YouTube] クライアント取得失敗。スキップします。")
        return None

    try:
        from googleapiclient.errors import HttpError

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

        english = english or {}
        if english.get("title"):
            try:
                response = _insert(youtube, _localize(body, english), mp4_path)
            except HttpError as e:
                # 英訳のタイトルが YouTube の制約に引っかかると 400 で弾かれる。
                # 英語のために投稿を止めないので、英語版なしで出し直す
                if e.resp.status != 400:
                    raise
                print(f"[YouTube] 英語版タイトル付きの投稿が弾かれました。英語版なしで出し直します: {e}")
                response = _insert(youtube, body, mp4_path)
        else:
            response = _insert(youtube, body, mp4_path)

        video_id = response['id']
        url = f"https://youtube.com/watch?v={video_id}"

        if thumbnail_path and Path(thumbnail_path).exists():
            _set_thumbnail(youtube, video_id, thumbnail_path)

        if english.get("srt"):
            _add_english_caption(youtube, video_id, english["srt"])

        print(f"[YouTube] アップロード完了 ({privacy}): {url}")
        return url

    except ImportError:
        print("[YouTube] google-api-python-client未インストール。スキップします。")
        print("pip install google-api-python-client google-auth-oauthlib")


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
