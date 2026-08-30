"""調べもの（common/grounding.py）と、その結果がプロンプトに載るところ。

**なぜ返答生成に検索を相乗りさせないのか**は common/grounding.py の説明を参照。
ここで守りたいのは次の4つ:

  - 明らかな雑談で API を叩かない（リクエストを1件につき1回増やさない）
  - 検索が使えなくても配信が止まらない（例外を投げない）
  - 「調べる必要が無かった」と「調べたが分からなかった」を混同しない
  - 調べた事実が、記憶ブロックより前にプロンプトへ載る
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import grounding  # noqa: E402

# persona は config から AFFIRMATIVE_BOT_DIR だけを読む。他のテストが
# sys.modules["config"] を差し替えていることがあるので、
# tests/test_prompt_repetition.py と同じやり方で自前のスタブ越しに読み込む
_config_stub = types.ModuleType("config")
_config_stub.AFFIRMATIVE_BOT_DIR = str(ROOT / "does-not-exist")


def _load_persona():
    previous = sys.modules.get("config")
    sys.modules["config"] = _config_stub
    try:
        spec = importlib.util.spec_from_file_location(
            "persona_grounding_test_module", LIVE / "persona.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous


persona = _load_persona()


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        # ゲート側は raise_for_status を使う（調べもの側は status_code を直接見る）
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


def reply(text, queries=None, thought=None):
    parts = []
    if thought:
        parts.append({"text": thought, "thought": True})
    parts.append({"text": text})
    candidate = {"content": {"parts": parts}}
    if queries is not None:
        candidate["groundingMetadata"] = {"webSearchQueries": queries}
    return {"candidates": [candidate]}


def gate_reply(text):
    return FakeResponse({"message": {"content": text}})


class RegexGateTest(unittest.TestCase):
    """ollama を使えないときの保険。実測 18/20 で、疑問語の無い質問を取りこぼす。"""

    def setUp(self):
        patcher = patch.object(grounding, "LIVE_GROUNDING_GATE", "regex")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_questions_are_looked_up(self):
        for text in ("今日の東京の天気どうだった？", "いま話題のアニメ教えて",
                     "ノーベル賞誰がとったの", "ラーメンって何が好き"):
            self.assertTrue(grounding.needs_lookup(text), text)

    def test_chatter_is_not_looked_up(self):
        # ここが落ちるとコメント1件につき Gemini のリクエストが無駄に増える
        for text in ("こんばんはー！", "かわいい", "おつかれさま", "8888", "", "   "):
            self.assertFalse(grounding.needs_lookup(text), text)

    def test_no_ollama_request_is_made(self):
        with patch.object(grounding.requests, "post") as post:
            grounding.needs_lookup("いま話題のアニメ教えて")
        post.assert_not_called()


class LlmGateTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(grounding, "LIVE_GROUNDING_GATE", "llm")
        patcher.start()
        self.addCleanup(patcher.stop)
        # 「一度だけ警告する」フラグはモジュール変数なので、テスト間で持ち越さない
        grounding._gate_warned = False
        self.addCleanup(setattr, grounding, "_gate_warned", False)

    def test_yes_and_no_are_honoured(self):
        with patch.object(grounding.requests, "post",
                          return_value=gate_reply("YES")) as post:
            self.assertTrue(grounding.needs_lookup("最近のおすすめ映画知りたい"))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], grounding.LIVE_GROUNDING_GATE_MODEL)
        # 理由を喋り始めると判定が1秒を超える
        self.assertEqual(body["options"]["num_predict"], 4)
        # keep_alive は既定では送らない。判定と返答生成が同じ runner になったので、
        # ここで送ると ollama.service の OLLAMA_KEEP_ALIVE=-1 を上書きしてしまう
        self.assertNotIn("keep_alive", body)

        with patch.object(grounding.requests, "post", return_value=gate_reply("NO")):
            self.assertFalse(grounding.needs_lookup("こんばんはー！"))

    def test_gate_shares_the_runner_with_reply_generation(self):
        """判定だけ別モデル・別 num_ctx にすると、26B の runner がもう1つ立つ。

        VRAM は 16GB しかないので 13.1GB のモデルは1つしか載らない。
        2026-08-30 の配信では判定用の gemma3:4b が 503 で載らず、
        調べもの判定がまるごと語句へ降格していた。
        """
        from common import llm
        self.assertEqual(grounding.LIVE_GROUNDING_GATE_MODEL, llm.LOCAL_LLM_MODEL)
        with patch.object(grounding.requests, "post",
                          return_value=gate_reply("YES")) as post:
            grounding.needs_lookup("東京タワーの高さしってる")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["options"]["num_ctx"], llm.OLLAMA_NUM_CTX)

    def test_ollama_failure_falls_back_to_the_regex(self):
        # ollama が落ちていても配信は続ける
        with patch.object(grounding.requests, "post", side_effect=RuntimeError("down")):
            self.assertTrue(grounding.needs_lookup("いま話題のアニメ教えて"))
            self.assertFalse(grounding.needs_lookup("こんばんはー！"))

    def test_unparsable_answer_falls_back_to_the_regex(self):
        with patch.object(grounding.requests, "post",
                          return_value=gate_reply("たぶん調べたほうがいいと思うよ")):
            self.assertTrue(grounding.needs_lookup("いま話題のアニメ教えて"))
            self.assertFalse(grounding.needs_lookup("こんばんはー！"))

    def test_gate_off_looks_everything_up(self):
        with patch.object(grounding, "LIVE_GROUNDING_GATE", "off"), \
             patch.object(grounding.requests, "post") as post:
            self.assertTrue(grounding.needs_lookup("こんばんはー！"))
            # 空だけは、切っていても叩かない
            self.assertFalse(grounding.needs_lookup(""))
        post.assert_not_called()

    def test_empty_comment_never_reaches_ollama(self):
        with patch.object(grounding.requests, "post") as post:
            self.assertFalse(grounding.needs_lookup("   "))
        post.assert_not_called()

    def test_kill_switch_stops_the_gate_too(self):
        # LIVE_GROUNDING=false は「調べもの機能ごと止める」スイッチ。
        # 判定だけ走らせても lookup が SKIP を返すので、ollama を叩くだけ無駄
        with patch.object(grounding, "LIVE_GROUNDING", False), \
             patch.object(grounding.requests, "post") as post:
            self.assertFalse(grounding.needs_lookup("いま話題のアニメ教えて"))
        post.assert_not_called()


class WarmupTest(unittest.TestCase):
    def test_warmup_loads_the_model_twice(self):
        # 読み込みだけでなく、そのあとの過渡（2.8〜3.0秒）も配信前に済ませる
        with patch.object(grounding, "LIVE_GROUNDING_GATE", "llm"), \
             patch.object(grounding.requests, "post",
                          return_value=gate_reply("NO")) as post:
            grounding.warmup()
        self.assertEqual(post.call_count, 2)
        # 読み込みには1件ぶんの上限では足りない
        self.assertEqual(post.call_args.kwargs["timeout"],
                         grounding.LIVE_GROUNDING_GATE_LOAD_SEC)

    def test_warmup_never_raises(self):
        with patch.object(grounding, "LIVE_GROUNDING_GATE", "llm"), \
             patch.object(grounding.requests, "post", side_effect=RuntimeError("down")):
            grounding.warmup()

    def test_warmup_is_skipped_when_not_using_the_llm_gate(self):
        for gate in ("regex", "off"):
            with patch.object(grounding, "LIVE_GROUNDING_GATE", gate), \
                 patch.object(grounding.requests, "post") as post:
                grounding.warmup()
            post.assert_not_called()
        with patch.object(grounding, "LIVE_GROUNDING", False), \
             patch.object(grounding.requests, "post") as post:
            grounding.warmup()
        post.assert_not_called()


class LookupTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_facts_are_returned_with_queries(self):
        payload = reply("- 最高気温は35.0℃\n- 猛暑日だった", queries=["東京 天気"],
                        thought="まず検索する")
        with patch.object(grounding.requests, "post",
                          return_value=FakeResponse(payload)) as post:
            result = grounding.lookup("視聴者のコメント：今日の東京暑かった？")
        self.assertEqual(result["status"], grounding.FACTS)
        self.assertIn("35.0℃", result["facts"])
        self.assertEqual(result["queries"], ["東京 天気"])
        # 思考の要約を事実として持ち出さないこと
        self.assertNotIn("まず検索する", result["facts"])

        body = post.call_args.kwargs["json"]
        self.assertEqual(body["tools"], [{"googleSearch": {}}])
        # urlContext を足すと、コメントに貼られた URL を開きに行く導線ができる
        self.assertNotIn("urlContext", str(body["tools"]))
        self.assertEqual(
            body["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            grounding.LIVE_GROUNDING_THINKING)

    def test_skip_and_unknown_are_distinguished(self):
        # SKIP は「調べる話ではなかった」、UNKNOWN は「調べたが分からなかった」。
        # 混ぜると、あいさつにまで「分からなかった」と返し始める
        with patch.object(grounding.requests, "post",
                          return_value=FakeResponse(reply("SKIP"))):
            self.assertEqual(grounding.lookup("こんばんは")["status"], grounding.SKIP)
        with patch.object(grounding.requests, "post",
                          return_value=FakeResponse(reply("UNKNOWN"))):
            self.assertEqual(grounding.lookup("誕生日いつ？")["status"], grounding.UNKNOWN)

    def test_empty_body_is_treated_as_skip(self):
        # 検索は走ったのに本文が空、という応答が実際にある
        with patch.object(grounding.requests, "post",
                          return_value=FakeResponse(reply("", queries=["x"]))):
            self.assertEqual(grounding.lookup("なに？")["status"], grounding.SKIP)

    def test_failures_never_raise(self):
        # 調べられなくても返事は作る。ここで例外が出るとコメントを1件落とす
        with patch.object(grounding.requests, "post",
                          side_effect=RuntimeError("timeout")):
            self.assertEqual(grounding.lookup("なに？")["status"], grounding.SKIP)
        with patch.object(grounding.requests, "post",
                          return_value=FakeResponse({}, status_code=429, text="rate")):
            self.assertEqual(grounding.lookup("なに？")["status"], grounding.SKIP)

    def test_all_models_are_tried_in_order(self):
        calls = []

        def post(url, **_kwargs):
            calls.append(url)
            raise RuntimeError("boom")

        with patch.object(grounding, "LIVE_GROUNDING_MODELS", ["model-a", "model-b"]), \
             patch.object(grounding.requests, "post", side_effect=post):
            grounding.lookup("なに？")
        self.assertEqual(len(calls), 2)
        self.assertIn("model-a", calls[0])
        self.assertIn("model-b", calls[1])

    def test_disabled_switch_makes_no_request(self):
        with patch.object(grounding, "LIVE_GROUNDING", False), \
             patch.object(grounding.requests, "post") as post:
            self.assertEqual(grounding.lookup("なに？")["status"], grounding.SKIP)
        post.assert_not_called()

    def test_missing_api_key_makes_no_request(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}), \
             patch.object(grounding.requests, "post") as post:
            self.assertEqual(grounding.lookup("なに？")["status"], grounding.SKIP)
        post.assert_not_called()


class PromptBlockTest(unittest.TestCase):
    def setUp(self):
        self.persona = persona

    def test_facts_block_is_shown(self):
        block = self.persona.search_block(
            {"status": grounding.FACTS, "facts": "- 最高気温は35.0℃"})
        self.assertIn("## 調べたこと", block)
        self.assertIn("35.0℃", block)

    def test_unknown_shows_a_line_but_skip_shows_nothing(self):
        self.assertIn("分からなかった",
                      self.persona.search_block({"status": grounding.UNKNOWN}))
        self.assertEqual("", self.persona.search_block({"status": grounding.SKIP}))
        self.assertEqual("", self.persona.search_block(None))
        self.assertEqual("",
                         self.persona.search_block({"status": grounding.FACTS, "facts": "  "}))

    def test_comment_prompt_puts_facts_before_the_memory_block(self):
        prompt = self.persona.build_comment_prompt(
            "suibari", "今日の東京暑かった？", {"now": "21:30", "energy": 70.0},
            mem={"activities": [{"mood": "散歩していた"}]},
            search={"status": grounding.FACTS, "facts": "- 最高気温は35.0℃"})
        self.assertIn("35.0℃", prompt)
        # 聞かれたことへの答えが先。後ろに回すと自分の近況の話に流れる
        self.assertLess(prompt.index("## 調べたこと"), prompt.index("散歩していた"))

    def test_followup_prompt_can_carry_facts(self):
        prompt = self.persona.build_followup_prompt(
            "夏アニメの話", search={"status": grounding.FACTS, "facts": "- 7月開始"})
        self.assertIn("7月開始", prompt)


if __name__ == "__main__":
    unittest.main()
