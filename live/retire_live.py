"""ライブ配信を休止するときの後片付け。

    ./venv/bin/python live/retire_live.py

先に配信と枠準備の timer を止めてから実行すること（README「ライブ配信の休止と再開」）。
timer が残っていると、翌朝4:00の枠準備がまた次の2枠を作り直す。

- 共有DBの未開始の配信枠を ended にする。bsky-affirmative-bot の気まぐれポストは
  この表を見て告知していたので、ここを閉じれば古い版が動いていても告知は止まる。
- YouTube 上の、一度も live まで行かなかった自分の枠を消す。
  実際に配信した枠（live / complete）には触らない（broadcast.cleanup_stale）。
"""

import sys
import traceback

import config  # noqa: F401  リポジトリのルートを sys.path へ足す
import broadcast
import memory


def main() -> int:
    try:
        memory.ensure_schema()
        memory.retire_other_prepared_broadcasts([])
        print("[retire] DBの未開始の配信枠を閉じました")
        removed = broadcast.cleanup_stale()
        print(f"[retire] YouTube の未配信の枠を {removed} 件消しました")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
