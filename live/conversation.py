"""配信中の会話。

統合前は botたん自身の発話テキストを8件持つだけで（live.py の recent_replies）、
**視聴者が何を言ったかはどこにも残っていなかった**。しかもそれを
「同じ言い回しを繰り返さないこと」という逆向きの制約でプロンプトに渡していたので、
話を続けるどころか話題を逸らす方向に効いていた。「一つ前の会話を忘れている」の正体。

ここでは1本の時系列に、コメントへの返答もフリートークも同じ形で積む。
相手ごとの索引を別に持つのは、場の流れから押し出されたあとも
「さっきその人と何を話したか」だけは引けるようにするため。

**当日の配信内だけ**をメモリに持つ。DB へは書かない（memory.save_comment() が
既に bottan_live.comments へ書いている）。ここが壊れても配信は止めないこと。
"""

import threading
from collections import deque


class ConversationLog:
    """場の流れ（時系列）と、相手ごとのやりとりの両方を引ける。

    書くのは配信のメインループだけだが、recent_replies() は RAG 先読みの
    雑務スレッドから呼ばれる（live.py の _start_chores）ので、ロックは必須。
    """

    def __init__(self, max_turns: int = 6, per_user_turns: int = 3):
        self._max_turns = max(1, int(max_turns))
        self._per_user_turns = max(1, int(per_user_turns))
        self._turns = deque(maxlen=self._max_turns)
        self._by_user = {}
        self._lock = threading.Lock()

    def add_comment_turn(self, channel_id: str, author: str,
                         comment: str, reply: str) -> None:
        """視聴者コメントとその返答。相手ごとの索引にも入れる。"""
        if not (comment or "").strip() or not (reply or "").strip():
            return
        turn = {
            "kind": "comment",
            "channel_id": channel_id or "",
            "author": author or "",
            "comment": comment.strip(),
            "reply": reply.strip(),
        }
        with self._lock:
            self._turns.append(turn)
            if turn["channel_id"]:
                per_user = self._by_user.get(turn["channel_id"])
                if per_user is None:
                    per_user = deque(maxlen=self._per_user_turns)
                    self._by_user[turn["channel_id"]] = per_user
                per_user.append(turn)

    def add_solo_turn(self, kind: str, reply: str, origin: str = "") -> None:
        """フリートーク・オープニング・クロージング。

        積まないと「さっき自分が何を話していたか」が場の流れから飛ぶ。

        origin は「その発話の出どころ」（persona.topic_origin）。他人の投稿に
        反応した発話は、履歴では role=assistant として残るので、札が無いと
        次のターンから自分の体験として扱われる。自分の話なら空でよい。
        """
        if not (reply or "").strip():
            return
        with self._lock:
            self._turns.append({
                "kind": kind or "solo",
                "channel_id": "",
                "author": "",
                "comment": "",
                "reply": reply.strip(),
                "origin": (origin or "").strip(),
            })

    def recent_turns(self, limit: int = None) -> list:
        """場の流れ。古い順に返す。"""
        with self._lock:
            turns = list(self._turns)
        return turns[-limit:] if limit and limit > 0 else turns

    def user_turns(self, channel_id: str, limit: int = None) -> list:
        """その相手とのやりとりだけ。古い順に返す。"""
        if not channel_id:
            return []
        with self._lock:
            turns = list(self._by_user.get(channel_id) or ())
        return turns[-limit:] if limit and limit > 0 else turns

    def recent_replies(self, limit: int = None) -> list:
        """botたん自身の発話だけを古い順に。

        フリートークの「同じ話を繰り返さない」ブロックと、RAG 先読みの
        クエリ材料に使う。統合前の live.py の recent_replies と同じ中身。
        """
        return [turn["reply"] for turn in self.recent_turns(limit)]
