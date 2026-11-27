"""ライブ配信を休止するときの後片付け。

    ./venv/bin/python live/retire_live.py

先に配信と枠準備の timer を止め、起動したジョブが終了してから実行すること
（README「ライブ配信の休止と再開」）。
"""

import sys

import config  # noqa: F401  リポジトリのルートを sys.path へ足す
import memory
from broadcast import TITLE_MARK
from youtube_auth import get_client


def retire_youtube_broadcasts(candidates: list[tuple[str, str | None]]) -> int:
    """DB にある候補だけを個別に照合し、失敗件数を返す。"""
    if not candidates:
        return 0

    broadcasts = get_client().liveBroadcasts()
    removed = preserved = failed = 0
    for broadcast_id, title in candidates:
        try:
            items = broadcasts.list(part="id,snippet,status,contentDetails",
                                    id=broadcast_id).execute().get("items", [])
            if not items:
                # 既に片付けた枠。同じ ID で再実行しても削除は要求しない。
                continue

            item = items[0]
            snippet = item.get("snippet", {})
            content_details = item.get("contentDetails")
            if (len(items) != 1 or item.get("id") != broadcast_id
                    or not title or not title.startswith(TITLE_MARK)
                    or snippet.get("title") != title
                    or item.get("status", {}).get("lifeCycleStatus") not in ("created", "ready")
                    or "actualStartTime" in snippet
                    or not isinstance(content_details, dict)
                    or content_details.get("boundStreamId")):
                preserved += 1
                continue

            broadcasts.delete(id=broadcast_id).execute()
            removed += 1
        except Exception as error:
            print(f"[retire] YouTube の枠を確認または削除できませんでした ({type(error).__name__})",
                  file=sys.stderr)
            failed += 1

    print(f"[retire] YouTube の枠: 削除 {removed} 件、保全 {preserved} 件、失敗 {failed} 件")
    return failed


def main() -> int:
    try:
        memory.ensure_schema()
        candidates = memory.retire_unstarted_broadcasts()
    except Exception as error:
        print(f"[retire] DB の未開始枠を閉じられませんでした ({type(error).__name__})",
              file=sys.stderr)
        return 1

    print("[retire] DB の未開始枠を告知対象から外しました")
    try:
        return int(retire_youtube_broadcasts(candidates) > 0)
    except Exception as error:
        print(f"[retire] YouTube の片付けを開始できませんでした ({type(error).__name__})",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
