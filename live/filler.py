"""コメントが途切れているときのフリートーク。

番組進行は固定しない。コメントが最優先で、空いた時間をここで埋める。
同じ話を繰り返すのが一番つまらないので、ネタは必ずローテーションさせる。

生成は先読みする。コメントが途切れてから作り始めると、LLMと音声合成の
待ち時間ぶんだけ無音が伸びる。
"""

import random
import threading

import memory


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

_KINDS = ["mood", "nagi", "short", "previous_live", "hobby", "hobby", "ask"]


class FillerPlanner:
    """次に話すフリートークのネタを用意する。

    DB アクセスは配信ループを止めないよう別スレッドで行い、結果をキャッシュする。
    """

    def __init__(self):
        self.rotator = TopicRotator(_KINDS)
        self.hobby = TopicRotator(_HOBBY_TOPICS)
        self._cache = {}
        self._lock = threading.Lock()
        self._used_nagi = set()
        self._used_prev = set()

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

    def next_topic(self) -> str:
        """次のフリートークのお題を文字列で返す。"""
        mem = self.cache
        for _ in range(len(_KINDS)):
            kind = self.rotator.next()
            hint = self._build(kind, mem)
            if hint:
                return hint
        # DBが全滅していてもひとりごとは話せる
        return self.hobby.next()

    def _build(self, kind: str, mem: dict) -> str:
        if kind == "hobby":
            return self.hobby.next()

        if kind == "ask":
            return ("視聴者に話しかけて、コメントしやすい問いかけをして。"
                    "答えやすくて、重くない話題にすること。")

        if kind == "mood":
            acts = mem.get("activities") or []
            if not acts:
                return ""
            act = acts[0]
            return (f"さっきまで「{act.get('mood', '')}」をしていた話をして。"
                    f"その様子を視聴者に伝えるように話すこと。")

        if kind == "nagi":
            for post in (mem.get("nagi_posts") or []):
                text = (post.get("post_text") or "").replace("\n", " ").strip()
                if not text or text in self._used_nagi:
                    continue
                self._used_nagi.add(text)
                return (f"Nagiで見かけた投稿に反応して。投稿の内容：「{text[:100]}」\n"
                        f"投稿した人の名前は出さないこと。内容にだけ触れて全肯定して。")
            return ""

        if kind == "short":
            short = mem.get("latest_short") or {}
            if not short.get("title"):
                return ""
            return (f"昨日出したショート動画「{short['title']}」の話をして。"
                    f"見てくれた人にお礼を言うこと。")

        if kind == "previous_live":
            for prev in (mem.get("previous_live") or []):
                comment = (prev.get("comment") or "").strip()
                if not comment or comment in self._used_prev:
                    continue
                self._used_prev.add(comment)
                author = prev.get("author_name") or "誰か"
                return (f"前の配信で {author} さんが「{comment[:60]}」って言ってたのを"
                        f"思い出した、という話をして。")
            return ""

        return ""
