"""YouTube API の認証。

トークンは Shorts 投稿とライブ配信で共用する（~/.bottan_youtube_token.pickle）。
スコープに youtube.force-ssl が含まれているので、追加の認可なしで
Live API とライブチャット取得に使える。

配信は完全自動なので `interactive=False`（既定）では対話フローに入らず例外にする。
トークンが無い・失効しているときは、人が事前に shorts/youtube_reauth.py を走らせる運用。
"""

import os
import pickle
from pathlib import Path

TOKEN_PATH = Path(os.getenv("YOUTUBE_TOKEN_PATH",
                            str(Path.home() / ".bottan_youtube_token.pickle")))
CLIENT_SECRETS = Path(os.getenv("YOUTUBE_CLIENT_SECRETS",
                                str(Path.home() / ".bottan_youtube_client_secrets.json")))
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

_client = None


class YouTubeAuthError(RuntimeError):
    pass


def _load_creds():
    if not TOKEN_PATH.exists():
        return None
    with open(TOKEN_PATH, "rb") as f:
        return pickle.load(f)


def _save_creds(creds) -> None:
    with open(TOKEN_PATH, "wb") as f:
        pickle.dump(creds, f)


def get_client(interactive: bool = False, force_new: bool = False):
    """認証済みクライアントを返す。使い回すのでプロセス内で1つ。

    interactive=True のときだけ、トークンが無い／無効な場合に
    コンソールの認可フローへ入る（人が居る前提。systemd からは絶対に通らない）。
    """
    global _client
    if _client is not None and not force_new:
        return _client

    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    creds = _load_creds()

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)

    if not creds or not creds.valid:
        if not interactive:
            where = "がありません" if creds is None else "が無効です"
            raise YouTubeAuthError(
                f"{TOKEN_PATH} {where}。shorts/youtube_reauth.py で認可してください")
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
        auth_url, _ = flow.authorization_url(prompt="consent")
        print(f"\n以下のURLをブラウザで開いてください:\n{auth_url}\n")
        code = input("認証後に表示されたコードを入力してください: ")
        flow.fetch_token(code=code)
        creds = flow.credentials
        _save_creds(creds)

    missing = [s for s in SCOPES if s not in (creds.scopes or [])]
    if missing:
        raise YouTubeAuthError(
            f"トークンに必要なスコープがありません: {missing}。再認可してください")

    _client = build("youtube", "v3", credentials=creds, cache_discovery=False)
    return _client
