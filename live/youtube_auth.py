"""YouTube API クライアント（ライブ配信からの入り口）。実装は common/youtube_auth.py。

配信は完全自動で動かすので、ここで対話的な認可フローに入ってはいけない。
トークンが無い・失効している場合は例外にして、事前に shorts/youtube_reauth.py を
人が走らせる運用にする。
"""

from common.youtube_auth import (  # noqa: F401
    SCOPES,
    TOKEN_PATH,
    YouTubeAuthError,
    get_client,
)
