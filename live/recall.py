"""視聴者に聞かれたことを、記憶から思い出す／あとで調べる。

配信のコメント応答には**記憶検索が一本も繋がっていなかった**。載っていたのは
`FillerPlanner` が10分ごとに引き直す決め打ちのスナップショット（その日の高得点
ポスト・直近の行動）だけで、質問の内容では何も引いていない。だから
「最近やったゲームなに？」を聞かれると、biorhythm に記録があるのに答えられず、
`common/grounding.py` が Gemini に聞きに行って「分からなかった」と返していた。

ここが2つの入口になる。

  SELF（自分の過去のこと）… `CommentRecall.lookup()` が Bot Memory API を
      **質問文で**引く。公開SNSの投稿・返信とbiorhythmを検索し、
      `web_research`（前に調べて覚えた知識）は別枠で引く。いいね・絵文字
      リアクション由来の文書と、返答本文を持たない過去の配信コメントは引かない。

  WEB（外の世界のこと）… `CommentRecall.enqueue_research()` が
      `affirmative_bot.bot_memory_research_jobs` へ語を積む。その場では調べない。
      biorhythm_server の botMemoryResearchWorker が60秒ごとに拾い、SearXNG で
      調べて `source_type='web_research'` として記憶へ入れる。**次に同じ話題が
      来たときには SELF 側で引ける**（同じ配信の数分後に間に合う）。

その場では「知らない」と正直に答える。これは Bluesky / Nagi のリプライ経路が
前からやっていることで、配信だけが輪から外れて同期で Gemini を叩いていた。

**ここは FillerPlanner に混ぜない。** あちらは「フリートークのお題在庫」の管理で、
`_used` / `_used_terms` にコメント応答の検索結果まで入れると、話に出していない
資料が「もう出したお題」として在庫から消える。共有するのは BotMemoryClient だけ。
"""

import hashlib
import queue
import re
import threading

import topics
from bot_memory_client import BotMemoryClient
from config import (
    BOT_MEMORY_QUERY_MAX_CHARS,
    LIVE_RECALL_LIMIT,
    LIVE_RECALL_TIMEOUT_SEC,
)

from common.db import connect

# ── 調査キューの決まり ────────────────────────────
#
# **bsky-affirmative-bot と同じ値でなければならない。** 片側だけ変えると、
# 同じ語が二重に積まれたり、向こうが弾く語をこちらだけ積んだりする。
# 原典: packages/database/src/researchJobs.ts:23-70
_MAX_TERM_LENGTH = 60     # MAX_TERM_LENGTH
_MIN_SUBJECT_LENGTH = 2   # MIN_SUBJECT_LENGTH
_MAX_PENDING_JOBS = 200   # MAX_PENDING_JOBS

# URL は積まない。貼られたリンクを開きに行く導線にすると、live/safety.py で
# 弾いている入力側の対策が素通しになる（researchJobs.ts:30-33 と同じ方針）
_URL = re.compile(r"https?://|www\.", re.I)

# 思い出すときに見る source_type。**youtube_live_comment を外してある。**
#
# 外さないと、いま届いた質問とほぼ同じ文の「過去に誰かが同じことを聞いたコメント」が
# 上位を埋める。実測（2026-09-02）:
#
#   「最近やったゲームなに？」 → 1位 youtube_live_comment「最近やったゲームは？」
#                                2位 youtube_live_comment「今日は何してた？」
#   「eu4ってどんなゲーム？」   → 1位 youtube_live_comment「eu4って知ってる？」で、
#                                前日に調べた web_research が4件の枠から押し出された
#
# 記憶に入っているのは視聴者の発言だけで、それに何と答えたかは検索の返り値に
# 入らない（botMemoryRouter.ts の serializeBotMemorySearchResult）。つまり
# 質問の答えにはならないのに、質問文と字面がいちばん近いので必ず上位に来る。
# 前回の配信で交わしたやりとりは _memory_block の previous_live が別に載せている。
_RECALL_SOURCES = (
    "biorhythm",              # 自分の行動ログ
    "bsky_affirmed_post", "nagi_affirmed_post",
    "bsky_received_reply", "nagi_received_reply",
)

# 調べて覚えた知識は**別枠で引く**。
#
# 一緒に引くと埋もれる。件数がまるで違う（web_research 15件に対して
# bsky_affirmed_post は9020件）うえ、サーバの並びはランクの合成なので、
# その話題について喋っている投稿が何件もあると知識カードを押し出す。
# 実測（2026-09-02）:
#
#   「eu4ってどんなゲーム？」           → 知識カードは5位（4件枠から漏れる）
#   「ワルプルギスの廻天って知ってる？」 → 上位13件が全部その映画の感想投稿で、
#                                          知識カードは20位までに出てこない
#
# 「知ってる？」に答えるための材料はまさにこの知識カードなので、席を1つ空ける。
_KNOWLEDGE_SOURCES = ("web_research",)
# 別枠に取る件数と、候補として見る件数。**候補は多めに見て、あとで絞る。**
_KNOWLEDGE_SLOTS = 1
_KNOWLEDGE_CANDIDATES = 3


def research_subject_hash(subject: str) -> str:
    """researchJobs.ts の `researchSubjectHash` と同じ値を出す。

    向こうは `botMemoryContentHash(subject.replace(/\\s+/g," ").trim().toLowerCase())`
    ＝ sha256 の16進。**ここがずれると主キーが噛み合わず、重複防止が効かない。**
    """
    normalized = re.sub(r"\s+", " ", subject or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def subject_of(comment_text: str) -> str:
    """調べる語を、コメント本文から拾う保険。

    ふだんは仕分け係（`grounding.classify`）が語まで返す。ollama が落ちて
    正規表現へ落ちたときだけここが要る。`topics.terms()` はカギ括弧の中身と
    ラテン文字語しか拾わないので取りこぼすが、**拾えなければ積まないだけ**で
    配信は続く。
    """
    found = topics.terms(comment_text)
    if not found:
        return ""
    # いちばん長い語を選ぶ。「EU4」より「Europa Universalis」のほうが検索で効く
    return max(found, key=len)


class CommentRecall:
    """コメントに応じて記憶を引き、知らない語を調査キューへ積む。

    **例外は投げない。** 記憶が引けなくても、キューに積めなくても、返事は作れる。
    """

    def __init__(self, memory_client=None, executor=None):
        self._client = memory_client or BotMemoryClient(
            timeout=LIVE_RECALL_TIMEOUT_SEC)
        self._executor = executor or self._insert_job
        self._queue = queue.Queue()
        self._thread = None
        self._warned = False

    # ── SELF: 思い出す ────────────────────────────
    #
    # ここだけはメインループの中で待つ。コメントの内容に依存する検索は先読み
    # できないので、これまで同期で Gemini を叩いていた枠をそのまま使う。
    # LIVE_RECALL_TIMEOUT_SEC で頭を打たせ、超えたら記憶なしで返事を作る。

    def lookup(self, comment_text: str) -> list:
        """質問文で記憶を引く。引けなければ空リスト。

        **2本を同時に投げる。** 知識カードは別枠で引かないと埋もれるが、
        順番に待つとコメントへの反応が2倍遅れる。LAN 内なので往復が1本増えても
        課金は増えず、待ち時間は遅いほうに合わせるだけで済む。
        """
        query = (comment_text or "").strip()[:BOT_MEMORY_QUERY_MAX_CHARS]
        if not query:
            return []

        knowledge = []

        def fetch_knowledge():
            knowledge.extend(self.knowledge(query))

        worker = threading.Thread(target=fetch_knowledge, daemon=True,
                                  name="recall-knowledge")
        worker.start()
        # search() は中で例外を握り潰して [] を返す（bot_memory_client.py:80-82）
        memories = self._client.search(
            query, limit=max(1, LIVE_RECALL_LIMIT - _KNOWLEDGE_SLOTS),
            sources=_RECALL_SOURCES, purpose="live_reply")
        worker.join(timeout=LIVE_RECALL_TIMEOUT_SEC)
        # 知識カードが先。**「知ってる？」への答えそのもの**なので、
        # 感想の投稿より前に置く
        return (knowledge + memories)[:LIVE_RECALL_LIMIT]

    def knowledge(self, query: str, asked: str = None) -> list:
        """前に調べて覚えた知識のうち、**その語が実際に聞かれているものだけ**。

        絞らないと必ず何かが返る（保管しているのは十数件で、順位は相対的にしか
        付かない）。「eu4ってどんなゲーム？」に対して2位が
        「Chanquete's Boat」だった、というのが実際の並び。
        調べた語は metadata.term に入っている（botMemoryResearchWorker.ts:79）。

        **外のことを聞かれたとき（WEB）にも呼ぶ。** それが「次に同じ話題が来た
        ときには答えられる」の出口で、ここを通さないと、せっかく調べた知識を
        読まずにモデルの当て推量で答えることになる（実測: 調べ終わった直後の
        「ペルセウス座流星群って知ってる？」に、カードを見ずに「知ってるよ！」）。
        asked には照合したい文（＝コメント本文）を渡す。省略すると query を使う。
        """
        query = (query or "").strip()[:BOT_MEMORY_QUERY_MAX_CHARS]
        if not query:
            return []
        asked = topics.term_key(asked if asked is not None else query)
        picked = []
        for item in self._client.search(query, limit=_KNOWLEDGE_CANDIDATES,
                                        sources=_KNOWLEDGE_SOURCES,
                                        purpose="live_reply"):
            metadata = item.get("metadata")
            term = (metadata or {}).get("term") if isinstance(metadata, dict) else ""
            # metadata が無い古い行のために、本文の1行目（＝語）も見る
            term = term or str(item.get("content") or "").split("\n")[0]
            key = topics.term_key(term)
            if key and key in asked:
                picked.append(item)
                if len(picked) >= _KNOWLEDGE_SLOTS:
                    break
        return picked

    def record_usage(self, items: list, output_ref: str = "") -> bool:
        """実際に読み上げた返答へ渡した記憶を、配信ループを止めず記録する。"""
        ids = list(dict.fromkeys(
            item.get("id") for item in (items or [])
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ))
        if not ids:
            return False
        threading.Thread(
            target=self._client.record_usage,
            args=(ids, output_ref, "live_reply"),
            daemon=True,
            name="live-reply-memory-usage",
        ).start()
        return True

    # ── WEB: あとで調べさせる ─────────────────────
    #
    # **ホットパスで DB を待たない。** 積めたかどうかは返事に影響しないので、
    # memory.BotMemoryWriter と同じくキュー＋デーモンスレッドへ渡す。

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="research-enqueue")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.join()
        self._queue.put(None)
        self._thread.join(timeout=5)

    def enqueue_research(self, subject: str) -> str:
        """調べる語を積む。積める形に整えた語を返す（積まないなら空文字）。

        返り値は**ログ用**。実際に INSERT できたかはここでは分からない
        （そのために待つと、そのぶんコメントへの反応が遅れる）。
        """
        subject = (subject or "").strip()[:_MAX_TERM_LENGTH].strip()
        if len(subject) < _MIN_SUBJECT_LENGTH or _URL.search(subject):
            return ""
        self.start()
        self._queue.put(subject)
        return subject

    def _run(self) -> None:
        while True:
            subject = self._queue.get()
            try:
                if subject is None:
                    return
                self._executor(subject)
                self._warned = False
            except Exception as e:
                if not self._warned:
                    print(f"[recall] 調査キューへ積めません（配信は継続します）: {e}")
                    self._warned = True
            finally:
                self._queue.task_done()

    @staticmethod
    def _insert_job(subject: str) -> None:
        """researchJobs.ts の `enqueueResearchJob` と同じことを SQL で行う。

        `affirmative_bot` は drizzle の管理下だが、消えるのはテーブル定義であって
        行ではない（memory.BotMemoryWriter が bot_memory_documents へ書くのと同じ）。
        """
        with connect() as conn:
            with conn.cursor() as cur:
                # 未処理の上限。ワーカーは同時実行1なので、無制限に積むと
                # 「もう誰も話題にしていない語」を延々調べ続けることになる
                cur.execute("""
                    SELECT count(*) FROM affirmative_bot.bot_memory_research_jobs
                    WHERE state = 'pending'
                """)
                if (cur.fetchone() or [0])[0] >= _MAX_PENDING_JOBS:
                    return
                cur.execute("""
                    INSERT INTO affirmative_bot.bot_memory_research_jobs
                        (subject_hash, subject)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (research_subject_hash(subject), subject))
            conn.commit()
