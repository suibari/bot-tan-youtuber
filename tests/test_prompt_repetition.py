"""同じ話題がプロンプトに何度も載らないこと。

2026-08-24 の配信で「FLASHBULB」の話が61発話中14回、しかも逐語でほぼ同じ
フリートークが3回出た。原因はお題ではなくプロンプトの共通ブロックで、

  - `_bot_state_block` が mood（さっきまでしてたこと）を全プロンプトに載せ、
    しかも「返事をこれに寄せてください」と指示していた
  - `_memory_block` の「今日やってたこと」が同じ biorhythm_history を引いており、
    先頭行が mood と同じ文になっていた（1プロンプトに同じ文が2回）

ここはその2つの回帰テスト。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"


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
config_stub.AFFIRMATIVE_BOT_DIR = str(ROOT / "does-not-exist")

persona = load_with_stubs(
    "persona_repetition_test_module", LIVE / "persona.py", {"config": config_stub})

MOOD = "お風呂で「FLASHBULB」を聴きながら、今日の出来事を思い出してたんだ。"
MOOD_OTHER = "お風呂上がりに「FLASHBULB」を聴きながらストレッチしてたよ。"
BOT = {"now": "21:30", "status": "Relax", "mood": MOOD, "energy": 70.0}
MEMORY = {
    "activities": [{"mood": MOOD}, {"mood": MOOD_OTHER}, {"mood": "モルフォと散歩した"}],
}


class BotStateBlockTest(unittest.TestCase):
    def test_it_does_not_tell_the_model_to_stick_to_the_mood(self):
        """「寄せてください」と書くと、mood が変わるまで全発話が同じ話になる。"""
        block = persona._bot_state_block(BOT)
        self.assertNotIn("寄せてください", block)

    def test_dropping_the_mood_keeps_status_and_energy(self):
        """mood を落としても口調を決める材料は残す。無いと無味乾燥になる。"""
        block = persona._bot_state_block({**BOT, "mood": ""})
        self.assertNotIn("さっきまでしてたこと", block)
        self.assertIn("ステータス", block)
        self.assertIn("元気度", block)

    def test_the_mood_is_shown_while_it_is_still_served(self):
        self.assertIn("さっきまでしてたこと", persona._bot_state_block(BOT))


class MemoryBlockTest(unittest.TestCase):
    def test_the_current_mood_is_not_repeated_in_the_memory_block(self):
        """同じ biorhythm_history を引いているので、素だと同じ文が2回載る。"""
        block = persona._memory_block(MEMORY, MOOD)
        self.assertNotIn(MOOD, block)

    def test_the_same_topic_in_other_wording_is_dropped_too(self):
        """FLASHBULB の行は文面違いで2つあった。文字列一致では落としきれない。

        渡すのは FillerPlanner.used_terms()（お題としてもう出したネタ）。
        """
        block = persona._memory_block(MEMORY, MOOD, ["flashbulb"])
        self.assertNotIn("FLASHBULB", block)
        self.assertIn("モルフォと散歩した", block)

    def test_unrelated_activities_survive(self):
        block = persona._memory_block(MEMORY, MOOD)
        self.assertIn("モルフォと散歩した", block)

    def test_a_whole_prompt_never_contains_the_mood_twice(self):
        prompt = persona.build_filler_prompt("なにか話して", BOT, MEMORY)
        self.assertEqual(prompt.count(MOOD), 1)


class BackwardCompatibleTest(unittest.TestCase):
    def test_prompts_work_without_the_optional_arguments(self):
        """既定で従来どおり組み上がること（他のテストが無改造で通る前提）。"""
        self.assertIn("届いたコメント",
                      persona.build_comment_prompt("suibari", "やっほー", BOT))
        self.assertIn("今回の話題", persona.build_filler_prompt("お題", BOT))
        self.assertIn("さっき話していたこと",
                      persona.build_followup_prompt("テーマ", None, BOT))
        self.assertIn("クロージング",
                      persona.build_scripted_prompt("クロージングです", BOT))


if __name__ == "__main__":
    unittest.main()
