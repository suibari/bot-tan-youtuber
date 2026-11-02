#!/usr/bin/env python3
"""
Instagram リール投稿（朝の作品紹介を YouTube と同じ動画で出す）

Facebook ログイン方式の Instagram Graph API を使う。Instagram ログイン方式は
ローカルの mp4 を送れず、公開 URL に置いた動画を取りに来させる仕組みしかないため。

**接続先は graph.facebook.com。** このトークンを graph.instagram.com に送ると
「Cannot parse access token」（190）で弾かれる（2026-11-02 に実際に起きた）。

流れ: コンテナ作成（resumable）→ rupload に mp4 を送る → 処理完了を待つ → 公開 → permalink 取得。
投稿（media_publish）は再試行しない。タイムアウトでも公開済みのことがあり、二重投稿になるため。

トークンは URL に載せず Authorization ヘッダーで送る。例外の文面に URL が出てもトークンが漏れない。
"""

import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import notify as _notify      # noqa: E402
from common.env import env_flag           # noqa: E402

GRAPH_VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}"

# 処理待ち。ドキュメントの目安は「1分ごと・5分まで」。30秒の Shorts なので早めに見て長めに待つ
POLL_INTERVAL = 30
POLL_TIMEOUT = 600
# mp4 の送信だけは、Meta が retriable と返したとき・通信が切れたときに送り直す
UPLOAD_RETRY_DELAYS = [10, 30]
# データアクセスの期限（90日）がこの日数を切ったら Discord に警告する
TOKEN_WARN_DAYS = 14

CAPTION_MAX = 2200


class InstagramError(RuntimeError):
    pass


def _token() -> str:
    return os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()


def _user_id() -> str:
    return os.getenv("INSTAGRAM_USER_ID", "").strip()


def enabled() -> bool:
    """INSTAGRAM_ENABLED=true かつトークンと IG の ID が入っているときだけ投稿する。"""
    return env_flag("INSTAGRAM_ENABLED") and bool(_token()) and bool(_user_id())


def _headers() -> dict:
    return {"Authorization": f"OAuth {_token()}"}


def _json(res: requests.Response, what: str) -> dict:
    """Graph API の応答を dict にする。エラーなら Meta のメッセージを載せた例外にする。"""
    try:
        body = res.json()
    except ValueError:
        body = {}
    if res.status_code >= 400 or "error" in body:
        err = body.get("error") or {}
        raise InstagramError(f"{what}: HTTP {res.status_code} "
                             f"{err.get('message') or res.text[:300]} (code={err.get('code')})")
    return body


def _get(path: str, fields: str) -> dict:
    res = requests.get(f"{GRAPH}/{path}", params={"fields": fields},
                       headers=_headers(), timeout=30)
    return _json(res, f"GET {path.split('/')[-1]}")


def _create_container(caption: str) -> str:
    data = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "share_to_feed": "true",
    }
    # AI 情報ラベル（2026-06 に追加された自己申告）
    if env_flag("INSTAGRAM_AI_GENERATED", True):
        data["is_ai_generated"] = "true"
    res = requests.post(f"{GRAPH}/{_user_id()}/media", data=data, headers=_headers(), timeout=30)
    return _json(res, "コンテナ作成")["id"]


def _upload_file(container_id: str, mp4_path: str) -> None:
    size = os.path.getsize(mp4_path)
    headers = {**_headers(), "offset": "0", "file_size": str(size)}
    for i, delay in enumerate([0] + UPLOAD_RETRY_DELAYS):
        if delay:
            print(f"[Instagram] 動画の送信を{delay}秒後にやり直します")
            time.sleep(delay)
        last = i == len(UPLOAD_RETRY_DELAYS)
        try:
            with open(mp4_path, "rb") as f:
                res = requests.post(f"{RUPLOAD}/{container_id}", headers=headers,
                                    data=f, timeout=600)
        except (requests.Timeout, requests.ConnectionError) as e:
            if last:
                raise InstagramError(f"動画の送信: {type(e).__name__}") from e
            print(f"[Instagram] 動画の送信で通信エラー: {type(e).__name__}")
            continue
        try:
            body = res.json()
        except ValueError:
            body = {}
        if res.ok and body.get("success"):
            print(f"[Instagram] 動画を送信しました（{size / 1e6:.1f}MB）")
            return
        info = body.get("debug_info") or {}
        if not info.get("retriable") or last:
            raise InstagramError(f"動画の送信: HTTP {res.status_code} "
                                 f"{info.get('message') or info.get('type') or res.text[:300]}")
        print(f"[Instagram] 動画の送信に失敗（再試行可）: {info.get('type')}")


def _wait_finished(container_id: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        body = _get(container_id, "status_code,status")
        code = body.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"動画の処理が {code}: {body.get('status', '')}")
        if time.monotonic() >= deadline:
            raise InstagramError(f"動画の処理が{POLL_TIMEOUT}秒で終わりませんでした（{code}）")
        print(f"[Instagram] 処理待ち（{code}）")
        time.sleep(POLL_INTERVAL)


def _publish(container_id: str) -> str:
    # 再試行しない。応答が返らなくても公開されていることがある
    res = requests.post(f"{GRAPH}/{_user_id()}/media_publish",
                        data={"creation_id": container_id}, headers=_headers(), timeout=60)
    return _json(res, "公開")["id"]


def _permalink(media_id: str) -> str:
    for delay in (0, 5, 15):
        time.sleep(delay)
        try:
            link = _get(media_id, "permalink").get("permalink")
            if link:
                return link
        except (InstagramError, requests.RequestException) as e:
            print(f"[Instagram] permalink の取得に失敗: {e}")
    # 公開はできている。URL が取れなくても成功として扱う
    return f"https://www.instagram.com/ (media_id={media_id})"


def upload_reel(mp4_path: str, caption: str) -> str | None:
    """リールを投稿して permalink を返す。失敗しても例外にせず None（YouTube 側を止めない）。"""
    if not enabled():
        print("[Instagram] 無効（INSTAGRAM_ENABLED かトークンが未設定）。スキップします。")
        return None
    stage = "コンテナ作成"
    try:
        container_id = _create_container(caption[:CAPTION_MAX])
        stage = "動画の送信"
        _upload_file(container_id, mp4_path)
        stage = "処理待ち"
        _wait_finished(container_id)
        stage = "公開"
        media_id = _publish(container_id)
        url = _permalink(media_id)
        print(f"[Instagram] 投稿完了: {url}")
        return url
    except Exception as e:
        print(f"[Instagram] {stage}で失敗: {e}")
        _notify.error("Instagram リール投稿", f"{stage}で失敗: {e}")
        return None


def check_token(now: float = None) -> None:
    """トークンの有効性とデータアクセスの期限を見て、近ければ Discord に警告する。例外は投げない。

    トークン自体は期限なしだが、データアクセスは 90 日で切れる（切れると投稿できない）。
    更新はグラフ API エクスプローラで構成「poster」から Generate し直し、.env を差し替える（README 5.5）。
    """
    if not _token():
        return
    now = now or time.time()
    try:
        res = requests.get(f"{GRAPH}/debug_token", params={"input_token": _token()},
                           headers=_headers(), timeout=30)
        data = _json(res, "debug_token").get("data", {})
    except Exception as e:
        print(f"[Instagram] トークンの確認に失敗（投稿は試みます）: {e}")
        return
    if not data.get("is_valid"):
        _notify.warn("Instagram のトークンが無効です。README 5.5 の手順で作り直してください")
        return
    expires = data.get("data_access_expires_at") or 0
    if expires:
        days = (expires - now) / 86400
        print(f"[Instagram] データアクセスの期限まで {days:.0f} 日")
        if days < TOKEN_WARN_DAYS:
            _notify.warn(f"Instagram のデータアクセス期限まで残り {max(days, 0):.0f} 日です。"
                         "README 5.5 の手順でトークンを作り直してください")
