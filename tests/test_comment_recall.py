"""聞かれたことを思い出す／あとで調べる（live/recall.py）。

**配信のホットパスから Gemini を外した**ことの回帰テストでもある。
外のことを聞かれたら、その場で調べずに調査キューへ積み、SearXNG が
非同期で調べて記憶へ入れる（live/recall.py の説明を参照）。
"""

import hashlib
import importlib
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"
sys.path.insert(0, str(ROOT))

from common import grounding  # noqa: E402


def load_with_stubs(name, path, stubs):
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


config_stub = types.ModuleType("config")
# live/config.py の既定と同じ値にしておくこと
config_stub.BOT_MEMORY_QUERY_MAX_CHARS = 500
config_stub.LIVE_RECALL_LIMIT = 4
config_stub.LIVE_RECALL_TIMEOUT_SEC = 5.0
config_stub.BIORHYTHM_MEMORY_API_URL = "http://192.168.1.200:3204"
config_stub.BIORHYTHM_INTERNAL_SECRET = "test-secret"
config_stub.BIORHYTHM_MEMORY_API_TIMEOUT_SEC = 15.0
# persona.py が読む（隣接リポジトリのペルソナ語彙の置き場所）
config_stub.AFFIRMATIVE_BOT_DIR = ROOT.parent / "bsky-affirmative-bot"

recall = load_with_stubs(
    "recall_test_module", LIVE / "recall.py", {"config": config_stub})
# **persona もスタブ越しに読む。** 素で読むと、先に走った別のテストが
# sys.modules へ置いた config を掴んで ImportError になる
persona = load_with_stubs(
    "persona_recall_test_module", LIVE / "persona.py", {"config": config_stub})


class FakeClient:
    """source_type ごとに返すものを分けられる偽クライアント。

    本物は sources で絞り込んだ結果を返すので、ここでもそう振る舞わせる。
    """

    def __init__(self, memories=None, knowledge=None, boom=False):
        self.memories = memories or []
        self.knowledge = knowledge or []
        self.boom = boom
        self.calls = []
        self.usage_calls = []

    def search(self, query, exclude_document_ids=None, limit=10, sources=None,
               purpose="live_filler"):
        sources = tuple(sources or ())
        self.calls.append({"query": query, "limit": limit, "sources": sources,
                           "purpose": purpose})
        # 本物は中で例外を握り潰して [] を返す（bot_memory_client.py:80-82）
        if self.boom:
            return []
        if sources == ("web_research",):
            return self.knowledge[:limit]
        return self.memories[:limit]

    def record_usage(self, document_ids, output_ref="", purpose="live_filler"):
        self.usage_calls.append({"document_ids": document_ids,
                                 "output_ref": output_ref,
                                 "purpose": purpose})
        return not self.boom


class SubjectHashTest(unittest.TestCase):
    """bsky-affirmative-bot の researchSubjectHash と同じ値を出すこと。

    **ずれると主キーが噛み合わず、同じ語が二重に積まれる。** 向こうは
    packages/database/src/researchJobs.ts:24-26。
    """

    def test_matches_the_typescript_hash(self):
        # node -e で実測した値。空白正規化 → trim → lower → sha256
        self.assertEqual(
            recall.research_subject_hash(" EU4 "),
            "e6bac767c5b87dbe09c3e4b9162453008634640a8c0175408a4bac154b9b99b9")

    def test_normalisation_folds_case_and_spaces(self):
        for text in ("EU4", "eu4", "  eu4  ", "eu4"):
            self.assertEqual(recall.research_subject_hash(text),
                             recall.research_subject_hash("EU4"))
        self.assertEqual(
            recall.research_subject_hash("Little  House\non the Prairie"),
            hashlib.sha256("little house on the prairie".encode()).hexdigest())


class EnqueueTest(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.recall = recall.CommentRecall(
            memory_client=FakeClient(), executor=self.jobs.append)

    def drain(self):
        self.recall._queue.join()

    def test_a_term_is_queued(self):
        self.assertEqual(self.recall.enqueue_research("ブルアカ"), "ブルアカ")
        self.drain()
        self.assertEqual(self.jobs, ["ブルアカ"])

    def test_too_short_or_too_long_or_a_url_is_dropped(self):
        # 上限・下限は researchJobs.ts と同じ値でなければならない
        self.assertEqual(self.recall.enqueue_research("あ"), "")
        self.assertEqual(self.recall.enqueue_research(""), "")
        self.assertEqual(self.recall.enqueue_research("   "), "")
        # URL は積まない。開きに行く導線にすると safety.py の対策が素通しになる
        self.assertEqual(self.recall.enqueue_research("https://example.com"), "")
        self.assertEqual(self.recall.enqueue_research("www.example.com"), "")
        self.drain()
        self.assertEqual(self.jobs, [])

    def test_a_long_term_is_clipped_not_dropped(self):
        queued = self.recall.enqueue_research("あ" * 100)
        self.assertEqual(len(queued), 60)   # MAX_TERM_LENGTH

    def test_a_broken_queue_does_not_stop_the_broadcast(self):
        boom = recall.CommentRecall(
            memory_client=FakeClient(),
            executor=lambda _s: (_ for _ in ()).throw(RuntimeError("db down")))
        boom.enqueue_research("ブルアカ")
        boom._queue.join()          # 例外がここまで漏れてこないこと
        boom.stop()


class LookupTest(unittest.TestCase):
    def test_the_question_itself_is_the_query(self):
        client = FakeClient([{"id": 1, "source": "biorhythm", "content": "EU4をやっていた"}])
        got = recall.CommentRecall(memory_client=client).lookup("最近やったゲームなに？")
        self.assertEqual(got, client.memories)
        for call in client.calls:
            self.assertEqual(call["query"], "最近やったゲームなに？")
            self.assertEqual(call["purpose"], "live_reply")

    def test_past_viewer_comments_are_not_searched(self):
        """**youtube_live_comment を引かないこと。**

        引くと、いま届いた質問とほぼ同じ文の「前に誰かが同じことを聞いた
        コメント」が上位を埋める。返り値にはそれに何と答えたかが入らないので、
        枠だけ食って答えにならない（実測は live/recall.py の説明を参照）。
        """
        client = FakeClient()
        recall.CommentRecall(memory_client=client).lookup("最近やったゲームなに？")
        for call in client.calls:
            self.assertNotIn("youtube_live_comment", call["sources"])
            self.assertNotIn("bsky_received_like", call["sources"])
            self.assertNotIn("nagi_received_reaction", call["sources"])

    def test_usage_is_recorded_as_live_reply(self):
        client = FakeClient()
        comment_recall = recall.CommentRecall(memory_client=client)
        self.assertTrue(comment_recall.record_usage(
            [{"id": 3}, {"id": 3}, {"id": "bad"}], "broadcast-1"))
        deadline = time.time() + 1
        while not client.usage_calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(client.usage_calls, [{
            "document_ids": [3],
            "output_ref": "broadcast-1",
            "purpose": "live_reply",
        }])

    def test_a_knowledge_card_gets_its_own_seat(self):
        """調べて覚えた知識は別枠で引き、いちばん前に置くこと。

        件数が桁違い（web_research 15件 対 bsky_affirmed_post 9020件）なので、
        まとめて引くと「知ってる？」への答えそのものが押し出される。
        """
        card = {"id": 9, "source": "web_research",
                "content": "eu4\n概要\nParadox のストラテジー",
                "metadata": {"term": "eu4"}}
        client = FakeClient(
            memories=[{"id": i, "source": "bsky_affirmed_post", "content": f"eu4 {i}"}
                      for i in range(4)],
            knowledge=[card])
        got = recall.CommentRecall(memory_client=client).lookup("eu4ってどんなゲーム？")
        self.assertEqual(got[0], card)
        self.assertEqual(len(got), 4)

    def test_an_unrelated_knowledge_card_is_dropped(self):
        """保管しているのは十数件なので、絞らないと必ず何かが返る。

        「eu4ってどんなゲーム？」の2位が「Chanquete's Boat」だった、というのが
        実際の並び。**質問がその語を含むものだけ**を通す。
        """
        client = FakeClient(knowledge=[
            {"id": 9, "source": "web_research", "content": "Chanquete's Boat\n概要",
             "metadata": {"term": "Chanquete's Boat"}}])
        got = recall.CommentRecall(memory_client=client).lookup("eu4ってどんなゲーム？")
        self.assertEqual(got, [])

    def test_an_old_card_without_metadata_still_matches(self):
        client = FakeClient(knowledge=[
            {"id": 9, "source": "web_research", "content": "eu4\n概要\nストラテジー"}])
        got = recall.CommentRecall(memory_client=client).lookup("eu4ってどんなゲーム？")
        self.assertEqual(len(got), 1)

    def test_a_long_comment_is_clipped(self):
        client = FakeClient()
        recall.CommentRecall(memory_client=client).lookup("あ" * 2000)
        self.assertEqual(len(client.calls[0]["query"]), 500)

    def test_the_two_searches_run_at_the_same_time(self):
        """順番に待つと、コメントへの反応が2倍遅れる。"""
        import threading
        import time
        started = threading.Barrier(2, timeout=2.0)

        class SlowClient(FakeClient):
            def search(self, query, exclude_document_ids=None, limit=10, sources=None,
                       purpose="live_filler"):
                started.wait()       # 相手が始まっていなければ TimeoutError
                time.sleep(0.05)
                return super().search(
                    query, exclude_document_ids, limit, sources, purpose)

        recall.CommentRecall(memory_client=SlowClient()).lookup("eu4って何？")

    def test_an_empty_comment_never_reaches_the_api(self):
        client = FakeClient()
        self.assertEqual(recall.CommentRecall(memory_client=client).lookup("   "), [])
        self.assertEqual(client.calls, [])

    def test_a_dead_api_returns_nothing(self):
        client = FakeClient(boom=True)
        self.assertEqual(recall.CommentRecall(memory_client=client).lookup("ゲーム"), [])


class SubjectOfTest(unittest.TestCase):
    """仕分け係が語を返さなかったときの保険。"""

    def test_a_quoted_or_latin_term_is_picked(self):
        self.assertEqual(recall.subject_of("「ブルアカ」やってる？"), "ブルアカ")
        self.assertEqual(recall.subject_of("EU4ってどう？"), "EU4")

    def test_nothing_is_picked_from_plain_chatter(self):
        self.assertEqual(recall.subject_of("こんばんはー！"), "")
        self.assertEqual(recall.subject_of(""), "")

    def test_the_longest_term_wins(self):
        # 検索クエリとしては長いほうが効く
        self.assertEqual(
            recall.subject_of("「Europa Universalis」と EU4 って同じ？"),
            "Europa Universalis")


class RecallBlockTest(unittest.TestCase):
    """**自分の体験と他人の話を混ぜないこと。**

    tests/test_memory_origin.py が守っているのと同じ事故（他人の投稿を
    自分の体験として喋る）を、思い出しブロックでも起こさない。
    """

    def setUp(self):
        self.block = persona.recall_block([
            {"source": "biorhythm", "content": "EU4をやっていた"},
            {"source": "web_research", "content": "eu4\nParadoxのストラテジー"},
            {"source": "bsky_affirmed_post", "content": "今日はカレーを作った"},
            {"source": "nagi_received_reply", "content": "おつかれさま！"},
        ])

    def test_her_own_records_are_hers(self):
        mine, _, theirs = self.block.partition("他の人の投稿・発言")
        self.assertIn("EU4をやっていた", mine)
        self.assertIn("Paradoxのストラテジー", mine)
        self.assertNotIn("カレー", mine)
        self.assertIn("カレー", theirs)
        self.assertIn("おつかれさま", theirs)

    def test_she_is_told_she_may_answer_from_them(self):
        # これが無いと、材料があるのに「知らない」と濁す
        self.assertIn("根拠に答えてよい", self.block)

    def test_research_is_not_called_a_memory(self):
        # 調べた事実を「思い出」にすると、体験として喋りだす
        self.assertIn("前に調べて知ったこと", self.block)

    def test_nothing_is_said_when_nothing_was_remembered(self):
        # 「思い出せなかった」と書くと、あいさつにまで「覚えてなくて」と返す
        self.assertEqual(persona.recall_block([]), "")
        self.assertEqual(persona.recall_block(None), "")
        self.assertEqual(persona.recall_block([{"source": "biorhythm", "content": " "}]), "")

    def test_it_sits_before_the_fixed_memory_block(self):
        prompt = persona.build_comment_prompt(
            "みかん", "最近やったゲームなに？", {"energy": 50},
            {"nagi_posts": [{"post_text": "きょうは晴れ"}]},
            recall=[{"source": "biorhythm", "content": "EU4をやっていた"}])
        self.assertLess(prompt.index("EU4をやっていた"), prompt.index("きょうは晴れ"))


class GeminiIsOutOfTheLiveLoopTest(unittest.TestCase):
    """既定で Gemini を叩かないこと。

    ここが緩むと、コメント1件につき課金リクエストが1回戻ってくる。
    """

    def test_the_default_is_off(self):
        """環境変数を置いていない状態では LIVE_GROUNDING が False であること。

        このリポジトリの `.env` は読み込まれるので、既定値そのものを
        読み直して確かめる。
        """
        import os
        # **共有の common.grounding を reload しない。** 環境変数を外したまま
        # 読み直すと、あとに走るテストがその状態を掴む（実際そうなった）。
        # 別名で読み込んで、こちらだけを見る
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LIVE_GROUNDING", None)
            spec = importlib.util.spec_from_file_location(
                "grounding_default_test_module", ROOT / "common" / "grounding.py")
            fresh = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh)
        self.assertFalse(fresh.LIVE_GROUNDING)

    def test_needs_lookup_is_false_while_the_switch_is_off(self):
        with patch.object(grounding, "LIVE_GROUNDING", False), \
             patch.object(grounding.requests, "post") as post:
            for text in ("いま話題のアニメ教えて", "最近やったゲームなに？", "こんばんは"):
                self.assertFalse(grounding.needs_lookup(text), text)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
