#!/usr/bin/env python3
"""YouTube OAuth2 再認証スクリプト（一度だけ手動実行）。"""

import os
import pickle
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN_PATH = Path.home() / ".bottan_youtube_token.pickle"
CLIENT_SECRETS = Path(os.getenv("YOUTUBE_CLIENT_SECRETS", str(Path.home() / ".bottan_youtube_client_secrets.json")))

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

if TOKEN_PATH.exists():
    TOKEN_PATH.unlink()
    print(f"古いトークンを削除しました: {TOKEN_PATH}")

from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_PATH, "wb") as f:
    pickle.dump(creds, f)

print(f"\n認証成功。トークンを保存しました: {TOKEN_PATH}")
print("以降はサービスが自動でトークンを更新します。")
