#!/usr/bin/env python3
"""Instagram リール投稿を手で 1 本試す（実機確認用）。

**Instagram の API には非公開投稿が無い。実行すると公開される。** 確認が済んだら
アプリから手で削除する。INSTAGRAM_ENABLED の値によらず投稿する（トークンと ID は .env から読む）。

    dotenv -f /home/suibari/work/bot-tan-youtuber/.env run -- \\
        ./venv/bin/python tools/instagram_post_test.py /tmp/pickup_xxx.mp4 [キャプション]
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shorts"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    os.environ["INSTAGRAM_ENABLED"] = "true"
    import instagram

    caption = sys.argv[2] if len(sys.argv) > 2 else "テスト投稿です（すぐ消します） #botたん"
    instagram.check_token()
    url = instagram.upload_reel(sys.argv[1], caption)
    print(url or "失敗しました")
    return 0 if url else 1


if __name__ == "__main__":
    sys.exit(main())
