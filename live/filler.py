"""コメントが途切れているときのフリートーク。

番組進行は固定しない。コメントが最優先で、空いた時間をここで埋める。
同じ話を繰り返すのが一番つまらないので、ネタは必ずローテーションさせる。

生成は先読みする。コメントが途切れてから作り始めると、LLMと音声合成の
待ち時間ぶんだけ無音が伸びる。
"""

import random
import threading

import memory
from bot_memory_client import BotMemoryClient
from config import BOT_MEMORY_QUERY_MAX_CHARS


class TopicRotator:
    """ネタを一巡させてから次の周回に入る。直近で使ったものは選ばない。"""

    def __init__(self, kinds: list):
        self.kinds = list(kinds)
        self._bag = []

    def next(self) -> str:
        if not self._bag:
            self._bag = list(self.kinds)
            random.shuffle(self._bag)
        return self._bag.pop()


# ペルソナ由来のひとりごとネタ。DB を引かずに always 使える
_HOBBY_TOPICS = [
    "最近見たアニメの話。能力バトルものの敵キャラの魅力について語ってみて。",
    "いまハマっているゲームの話。緻密に計画を立てるのが好きという話につなげて。",
    "飼っている白い大型犬のモルフォ（サモエド）の話。朝に弱くて起こされる話でもいい。",
    "友達のことみちゃんの話。学校でおしゃべりしている様子を話して。",
    "友達のラテちゃんの話。ネコに変身する魔法使い見習いの子。",
    "GIANTのクロスバイクでポタリングした話。",
    "SONYのカメラで青空を撮るのが好きな話。",
    "ホラー映画が好きなこと。明るい性格とのギャップの話にしてもいい。",
    "夜の時間が好きな理由。静かな時間に考えごとをする話。",
    "視聴者に質問を投げかけて。今日どんな一日だったか聞いてみて。",
    "いま書いてみたい本の話。誰かを励ます本を書きたいという夢について。",
]

_KINDS = ["rag", "rag", "mood", "nagi", "short", "previous_live", "hobby", "hobby", "ask"]


class FillerPlanner:
    """次に話すフリートークのネタを用意する。

    DB アクセスは配信ループを止めないよう別スレッドで行い、結果をキャッシュする。
    """

    def __init__(self, memory_client=None):
        self.rotator = TopicRotator(_KINDS)
        self.hobby = TopicRotator(_HOBBY_TOPICS)
        self._cache = {}
        self._lock = threading.Lock()
        self._used_nagi = set()
        self._used_prev = set()
        self._used_rag = set()
        self._rag_candidates = []
        self._rag_refreshing = False
        self._rag_client = memory_client or BotMemoryClient()

    def refresh_memory(self) -> dict:
        """DBから記憶を引き直す。数分に1回でよい。"""
        data = {}
        for key, fn in (
            ("activities",    lambda: memory.get_today_activities(5)),
            ("nagi_posts",    lambda: memory.get_nagi_posts(5)),
            ("latest_short",  memory.get_latest_short),
            ("previous_live", lambda: memory.get_previous_live_highlights(5)),
        ):
            try:
                data[key] = fn()
            except Exception as e:
                print(f"[filler] {key} を引けません（無視します）: {e}")
                data[key] = [] if key != "latest_short" else {}
        with self._lock:
            self._cache = data
        return data

    @property
    def cache(self) -> dict:
        with self._lock:
            return dict(self._cache)

    def next_topic(self) -> dict:
        """次のお題と、そのお題が実際に参照する記憶IDを返す。"""
        mem = self.cache
        for _ in range(len(_KINDS)):
            kind = self.rotator.next()
            hint = self._build(kind, mem)
            if hint:
                return hint
        # DBが全滅していてもひとりごとは話せる
        return {"hint": self.hobby.next(), "memory_ids": []}

    def prefetch_rag(self, bot: dict, recent_comments=None,
                     recent_replies=None) -> bool:
        """フリートークのホットパスを止めず、次のRAG候補を先読みする。"""
        with self._lock:
            if self._rag_refreshing or not self._rag_client.enabled:
                return False
            self._rag_refreshing = True
        # 1件ずつ切り詰めてから連結する。全体を後ろから切ると、配信が進んで
        # mood と返答が長くなったぶんだけ直近のコメントが落ちてしまう
        def clip(text, limit):
            return (text or "").strip()[:limit]

        comments = [
            clip(item.get("text"), 80)
            for item in (recent_comments or [])[-4:]
            if isinstance(item, dict)
        ]
        query = "\n".join(filter(None, [
            clip(bot.get("mood"), 100),
            clip(bot.get("status"), 40),
            *(clip(reply, 80) for reply in (recent_replies or [])[-2:]),
            *comments,
        ]))[:BOT_MEMORY_QUERY_MAX_CHARS]

        def run():
            try:
                candidates = self._rag_client.search(
                    query,
                    exclude_document_ids=list(self._used_rag),
                    limit=10,
                )
                with self._lock:
                    self._rag_candidates = candidates
            except Exception as error:
                print(f"[filler] RAG先読みでエラー（従来話題へ戻します）: {error}")
                with self._lock:
                    self._rag_candidates = []
            finally:
                with self._lock:
                    self._rag_refreshing = False

        threading.Thread(target=run, daemon=True, name="bot-memory-prefetch").start()
        return True

    def record_usage(self, document_ids: list, output_ref: str = "") -> bool:
        threading.Thread(
            target=self._rag_client.record_usage,
            args=(document_ids, output_ref),
            daemon=True,
            name="bot-memory-usage",
        ).start()
        return True

    def _build(self, kind: str, mem: dict):
        if kind == "rag":
            with self._lock:
                candidates = list(self._rag_candidates)
            for candidate in candidates:
                document_id = candidate.get("id")
                if document_id in self._used_rag:
                    continue
                self._used_rag.add(document_id)
                source = candidate.get("source") or "memory"
                content = candidate.get("content", "").replace("\n", " ").strip()
                if not content:
                    continue
                return {
                    "hint": {
                        "rag_source": source,
                        "rag_content": content[:300],
                    },
                    "memory_ids": [document_id],
                }
            return None

        if kind == "hobby":
            return {"hint": self.hobby.next(), "memory_ids": []}

        if kind == "ask":
            return {"hint": ("視聴者に話しかけて、コメントしやすい問いかけをして。"
                             "答えやすくて、重くない話題にすること。"),
                    "memory_ids": []}

        if kind == "mood":
            acts = mem.get("activities") or []
            if not acts:
                return ""
            act = acts[0]
            return {"hint": (f"さっきまで「{act.get('mood', '')}」をしていた話をして。"
                             f"その様子を視聴者に伝えるように話すこと。"),
                    "memory_ids": []}

        if kind == "nagi":
            for post in (mem.get("nagi_posts") or []):
                text = (post.get("post_text") or "").replace("\n", " ").strip()
                if not text or text in self._used_nagi:
                    continue
                self._used_nagi.add(text)
                return {"hint": (f"Nagiで見かけた投稿に反応して。投稿の内容：「{text[:100]}」\n"
                                 f"投稿した人の名前は出さないこと。内容にだけ触れて全肯定して。"),
                        "memory_ids": []}
            return ""

        if kind == "short":
            short = mem.get("latest_short") or {}
            if not short.get("title"):
                return ""
            return {"hint": (f"昨日出したショート動画「{short['title']}」の話をして。"
                             f"見てくれた人にお礼を言うこと。"),
                    "memory_ids": []}

        if kind == "previous_live":
            for prev in (mem.get("previous_live") or []):
                comment = (prev.get("comment") or "").strip()
                if not comment or comment in self._used_prev:
                    continue
                self._used_prev.add(comment)
                return {"hint": (f"前の配信で「{comment[:60]}」っていう話が出たのを"
                                 f"思い出した、という話をして。名前は出さないこと。"),
                        "memory_ids": []}
            return ""

        return ""
