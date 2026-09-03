"""コメントが途切れているときのフリートークと、その話題の掘り下げ。

番組進行は固定しない。コメントが最優先で、空いた時間をここで埋める。
同じ話を繰り返すのが一番つまらないので、**配信中に一度出した話題は二度出さない**。
ネタが一巡したら使用済みを畳んで2周目に入る（無言になるよりはよい）。

生成は先読みする。コメントが途切れてから作り始めると、LLMと音声合成の
待ち時間ぶんだけ無音が伸びる。

botたんのホームは Nagi、Bluesky は毎日通う出張先。**どちらにも固定枠を置く**。
片方しか無いと、そちらだけが居場所であるかのように喋ってしまう。
"""

import random
import threading

import memory
import topics
from bot_memory_client import BotMemoryClient
from config import BOT_MEMORY_QUERY_MAX_CHARS


class TopicRotator:
    """ネタを一巡させてから次の周回に入る。直近で使ったものは選ばない。"""

    def __init__(self, kinds: list):
        self.kinds = list(kinds)
        self._bag = []
        self._last = None

    def next(self) -> str:
        if not self._bag:
            self._bag = list(self.kinds)
            random.shuffle(self._bag)
            # 袋の切れ目で同じものが続かないようにする。pop() は末尾から
            # 取るので、末尾が直前と同じなら別の位置と入れ替える
            if len(self._bag) > 1 and self._bag[-1] == self._last:
                swap = random.randrange(len(self._bag) - 1)
                self._bag[-1], self._bag[swap] = self._bag[swap], self._bag[-1]
        self._last = self._bag.pop()
        return self._last


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

# コメントを促す問いかけ。1種類だけだと配信中に何度も同じことを聞くことになる
_ASK_TOPICS = [
    "視聴者に話しかけて、コメントしやすい問いかけをして。答えやすくて、重くない話題にすること。",
    "今日あったちょっといいことを聞いてみて。小さなことでいいと伝えること。",
    "いま何を飲んでいるか、何を食べたかを聞いてみて。",
    "最近ハマっているものを聞いてみて。作品でも食べ物でもいいと伝えること。",
    "今日はどんな天気だったか聞いてみて。空の話につなげてもいい。",
    "明日やろうと思っていることを聞いてみて。小さな予定でいいと伝えること。",
]

_KINDS = ["rag", "rag", "mood", "nagi", "bsky", "short",
          "previous_live", "hobby", "hobby", "ask"]


def _topic_key(kind: str, text: str) -> tuple:
    """配信内の重複判定に使うキー。

    固有名詞が拾えるならそれを並べたものをキーにする。頭の60文字で見ていると、
    同じネタでも言い回しが違うだけで別物として通ってしまう（2026-08-24 の配信では
    「お風呂で『FLASHBULB』を聴きながら…」が文面違いで履歴に2行あった）。

    語が拾えないときだけ、従来どおり空白を潰した頭60文字に落ちる。
    """
    found = topics.terms(text)
    if found:
        return (kind, "|".join(sorted(topics.term_key(term) for term in found)))
    return (kind, "".join(str(text or "").split())[:60])


class FillerPlanner:
    """次に話すフリートークのネタと、いま話しているテーマの掘り下げを用意する。

    DB アクセスは配信ループを止めないよう別スレッドで行い、結果をキャッシュする。
    """

    def __init__(self, memory_client=None):
        self.rotator = TopicRotator(_KINDS)
        self.hobby = TopicRotator(_HOBBY_TOPICS)
        self.ask = TopicRotator(_ASK_TOPICS)
        self._cache = {}
        self._lock = threading.Lock()
        # 配信中に使った話題。_lock とは別にしておく（_build は _lock を
        # 握った状態から呼ばれることがあり、同じ錠を二度取ると固まる）
        self._used_lock = threading.Lock()
        self._used = set()
        # 出したお題に含まれていた固有名詞。キーが違っても中身が同じネタを
        # 弾くために使う（RAG は同じ話が別 document_id で何件も入っている）
        self._used_terms = set()
        self._rag_candidates = []
        self._rag_refreshing = False
        self._rag_client = memory_client or BotMemoryClient()
        # 掘り下げ用。フリートークの候補とは別に持つ（クエリの作り方が違う）
        self._followup_theme = ""
        self._followup_candidates = []
        self._followup_pending = False
        self._followup_refreshing = False

    # ── 使用済みの管理 ────────────────────────────────

    def _is_used(self, key) -> bool:
        with self._used_lock:
            return key in self._used

    def _mark_used(self, key, text: str = "") -> None:
        with self._used_lock:
            self._used.add(key)
            if text:
                self._used_terms |= topics.keys(text)

    def used_terms(self) -> set:
        """お題として出したネタの固有名詞。記憶ブロックの絞り込みにも使う。"""
        with self._used_lock:
            return set(self._used_terms)

    def _overlaps(self, text: str) -> bool:
        """そのお題が、もう出したネタと固有名詞で重なるか。

        **固定文のお題（hobby / ask）には掛けないこと。** 人手で書き分けた
        レパートリーなので、重なりで潰すと在庫が痩せる。
        """
        return topics.overlaps(text, self.used_terms())

    def _used_rag_ids(self) -> list:
        with self._used_lock:
            return [key[1] for key in self._used
                    if key[0] == "rag" and isinstance(key[1], int)]

    def reset_used(self) -> None:
        with self._used_lock:
            self._used.clear()
            self._used_terms.clear()

    # ── 記憶 ──────────────────────────────────────────

    def refresh_memory(self) -> dict:
        """DBから記憶を引き直す。数分に1回でよい。"""
        data = {}
        for key, fn in (
            ("activities",    lambda: memory.get_today_activities(5)),
            ("nagi_posts",    lambda: memory.get_nagi_posts(5)),
            ("bsky_posts",    lambda: memory.get_bsky_posts(5)),
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

    # ── フリートークのお題 ────────────────────────────

    def next_topic(self) -> dict:
        """次のお題と、そのお題が実際に参照する記憶IDを返す。

        一巡して何も残らなければ使用済みを畳んで2周目に入る。同じ話が二度出るのは
        よくないが、黙るよりはよい。
        """
        topic = self._pick_topic()
        if topic is not None:
            return topic

        print("[filler] 話題を一巡したので使用済みをリセットします")
        self.reset_used()
        topic = self._pick_topic()
        if topic is not None:
            return topic

        # DBが全滅していてもひとりごとは話せる。
        # ここも key を返す（呼ぶ側が話題の種別をログに出すのと、
        # tests/test_filler_topics.py が key を見ているため）
        hint = self.hobby.next()
        return {"hint": hint, "memory_ids": [], "key": _topic_key("hobby", hint)}

    def _pick_topic(self):
        mem = self.cache
        for _ in range(len(_KINDS)):
            topic = self._build(self.rotator.next(), mem)
            if topic:
                return topic
        # 抽選で拾えなかっただけで、まだ残っているネタがあるかもしれない。
        # _KINDS には重複があるうえ、袋のどこから引き始めるかで一巡しても
        # 触らない種別が出る。使用済みを畳む前に、種別を順に当たって確かめる
        for kind in dict.fromkeys(_KINDS):
            topic = self._build(kind, mem)
            if topic:
                return topic
        return None

    # ── RAG の先読み ──────────────────────────────────

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
                    exclude_document_ids=self._used_rag_ids(),
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

    # ── 掘り下げ ──────────────────────────────────────

    def set_followup_theme(self, theme: str) -> None:
        """いま話しているテーマを覚える。**ここではネットワークを触らない。**

        呼ぶのは配信のメインループなので、ここで RAG を引くとそのぶん
        次のコメントへの反応が遅れる。実際に引くのは雑務スレッドの
        prefetch_followup()。
        """
        theme = (theme or "").strip()
        with self._lock:
            self._followup_theme = theme
            self._followup_pending = bool(theme)
            # 前のテーマの候補は使わない。別の話の掘り下げになってしまう
            self._followup_candidates = []

    def prefetch_followup(self) -> bool:
        """テーマが変わっていたら候補を引き直す。雑務スレッドから毎秒呼んでよい。"""
        with self._lock:
            if self._followup_refreshing or not self._followup_pending:
                return False
            if not self._rag_client.enabled:
                self._followup_pending = False
                return False
            theme = self._followup_theme[:BOT_MEMORY_QUERY_MAX_CHARS]
            self._followup_pending = False
            self._followup_refreshing = True

        def run():
            try:
                candidates = self._rag_client.search(
                    theme, exclude_document_ids=self._used_rag_ids(), limit=5)
                with self._lock:
                    # 引いている間にテーマが変わっていたら捨てる
                    if self._followup_theme[:BOT_MEMORY_QUERY_MAX_CHARS] == theme:
                        self._followup_candidates = candidates
            except Exception as error:
                print(f"[filler] 掘り下げの先読みでエラー（無視します）: {error}")
            finally:
                with self._lock:
                    self._followup_refreshing = False

        threading.Thread(target=run, daemon=True, name="bot-memory-followup").start()
        return True

    def next_followup(self):
        """掘り下げに使う資料を1件。無ければ None。

        None でも掘り下げ自体はできる（資料なしで別の角度を出させる）ので、
        呼び出し側はここが空でも黙らないこと。
        """
        with self._lock:
            candidates = list(self._followup_candidates)
        for candidate in candidates:
            document_id = candidate.get("id")
            key = ("rag", document_id)
            if self._is_used(key):
                continue
            content = (candidate.get("content") or "").replace("\n", " ").strip()
            if not content:
                continue
            if self._overlaps(content):
                continue
            self._mark_used(key, content)
            return {
                "hint": {
                    "rag_source": candidate.get("source") or "memory",
                    "rag_content": content[:300],
                },
                "memory_ids": [document_id],
            }
        return None

    def record_usage(self, document_ids: list, output_ref: str = "") -> bool:
        threading.Thread(
            target=self._rag_client.record_usage,
            args=(document_ids, output_ref),
            daemon=True,
            name="bot-memory-usage",
        ).start()
        return True

    # ── お題の組み立て ────────────────────────────────

    def _build(self, kind: str, mem: dict):
        """そのお題をいま出せるなら dict を、出せないなら None を返す。

        戻り値の "key" が配信内の重複判定に使われる。**返すときにその場で
        使用済みの印を付ける**（_pick_topic は最初に返ってきたものをそのまま
        使うので、ここで付けても取りこぼさない）。
        """
        if kind == "rag":
            with self._lock:
                candidates = list(self._rag_candidates)
            for candidate in candidates:
                document_id = candidate.get("id")
                key = ("rag", document_id)
                if self._is_used(key):
                    continue
                content = (candidate.get("content") or "").replace("\n", " ").strip()
                if not content:
                    continue
                # 同じ話が別 document_id で何件も入っていることがある。
                # excludeDocumentIds は ID 単位なので、受け取ってから中身で捨てる
                if self._overlaps(content):
                    print(f"[filler] もう出したネタと重なるので飛ばします: {content[:30]}")
                    continue
                self._mark_used(key, content)
                return {
                    "hint": {
                        "rag_source": candidate.get("source") or "memory",
                        "rag_content": content[:300],
                    },
                    "memory_ids": [document_id],
                    "key": key,
                }
            return None

        if kind == "hobby":
            return self._from_rotator("hobby", self.hobby, len(_HOBBY_TOPICS))

        if kind == "ask":
            return self._from_rotator("ask", self.ask, len(_ASK_TOPICS))

        if kind == "mood":
            # 以前は activities[0] 固定だったので、同じ行動の話が何度も出ていた
            for act in (mem.get("activities") or []):
                mood = (act.get("mood") or "").strip()
                if not mood:
                    continue
                key = _topic_key("mood", mood)
                if self._is_used(key) or self._overlaps(mood):
                    continue
                self._mark_used(key, mood)
                return {"hint": (f"さっきまで「{mood}」をしていた話をして。"
                                 f"その様子を視聴者に伝えるように話すこと。"),
                        "memory_ids": [], "key": key}
            return None

        if kind == "nagi":
            return self._from_posts(
                "nagi", mem.get("nagi_posts"),
                "SNSのNagiで見かけた投稿に反応して。投稿の内容：「{text}」\n"
                "投稿した人の名前は出さないこと。内容にだけ触れて全肯定して。")

        if kind == "bsky":
            return self._from_posts(
                "bsky", mem.get("bsky_posts"),
                "Blueskyで見かけた投稿に反応して。投稿の内容：「{text}」\n"
                "投稿した人の名前は出さないこと。内容にだけ触れて全肯定して。")

        if kind == "short":
            short = mem.get("latest_short") or {}
            title = (short.get("title") or "").strip()
            if not title:
                return None
            key = _topic_key("short", title)
            if self._is_used(key):
                return None
            self._mark_used(key, title)
            return {"hint": (f"昨日出したショート動画「{title}」の話をして。"
                             f"見てくれた人にお礼を言うこと。"),
                    "memory_ids": [], "key": key}

        if kind == "previous_live":
            for prev in (mem.get("previous_live") or []):
                comment = (prev.get("comment") or "").strip()
                if not comment:
                    continue
                key = _topic_key("prev", comment)
                if self._is_used(key) or self._overlaps(comment):
                    continue
                self._mark_used(key, comment)
                return {"hint": (f"前の配信で「{comment[:60]}」っていう話が出たのを"
                                 f"思い出した、という話をして。名前は出さないこと。"),
                        "memory_ids": [], "key": key}
            return None

        return None

    def _from_rotator(self, kind: str, rotator: TopicRotator, size: int):
        """固定文のお題。一巡ぶん引いてみて、未使用のものがあれば返す。"""
        for _ in range(size):
            hint = rotator.next()
            key = _topic_key(kind, hint)
            if self._is_used(key):
                continue
            # 重なり判定は掛けない（固定文は人手で書き分けたレパートリーなので、
            # 潰すと在庫が痩せる）。ただし語は台帳に入れる。ここで GIANT の話を
            # したら、同じ話題の RAG 候補は弾いてよい
            self._mark_used(key, hint)
            return {"hint": hint, "memory_ids": [], "key": key}
        return None

    def _from_posts(self, kind: str, posts, template: str):
        """SNS で見かけた投稿のお題。Nagi と Bluesky で同じ形。"""
        for post in (posts or []):
            text = (post.get("post_text") or "").replace("\n", " ").strip()
            if not text:
                continue
            key = _topic_key(kind, text)
            if self._is_used(key) or self._overlaps(text):
                continue
            self._mark_used(key, text)
            return {"hint": template.format(text=text[:100]),
                    "memory_ids": [], "key": key}
        return None
