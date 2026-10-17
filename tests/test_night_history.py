"""夜の動画の台帳と、次の回の型・切り口の選び方（shorts/night_history.py）。"""

import json
import tempfile
import unittest
from pathlib import Path

import night_history as nh


def ideas(post=1, facts=(("歴史・由来", "a"), ("語源・言葉", "b"), ("心と体", "c")),
          question="q", reframe="r", metaphor="m"):
    return {"chosen_post": post, "ideas": {
        "fact": [{"about": "ポケモン", "angle": a, "idea": i} for a, i in facts],
        "question": question, "reframe": reframe, "metaphor": metaphor}}


class ChoosePlanTest(unittest.TestCase):
    def test_an_unused_type_comes_first(self):
        history = [{"type": "fact", "angle": "心と体"}, {"type": "question"}]
        self.assertEqual(nh.choose_plan(ideas(), history)["type"], "reframe")

    def test_the_least_recently_used_type_when_all_were_used(self):
        history = [{"type": "reframe"}, {"type": "fact", "angle": "心と体"},
                   {"type": "metaphor"}, {"type": "question"}]
        self.assertEqual(nh.choose_plan(ideas(), history)["type"], "reframe")

    def test_a_type_without_an_idea_is_skipped(self):
        history = [{"type": "fact", "angle": "心と体"}]
        plan = nh.choose_plan(ideas(question="  "), history)
        self.assertEqual(plan["type"], "reframe")
        self.assertEqual(plan["idea"], "r")

    def test_fact_avoids_recently_used_angles(self):
        others = [{"type": "question"}, {"type": "reframe"}, {"type": "metaphor"}]
        history = [{"type": "fact", "angle": "心と体"}] + others
        plan = nh.choose_plan(ideas(), history)
        self.assertEqual((plan["type"], plan["angle"], plan["idea"], plan["picked_post"]),
                         ("fact", "語源・言葉", "b", 1))
        # 「心と体」と「語源・言葉」が続いていたら、残りの切り口を選ぶ
        history = [{"type": "fact", "angle": "心と体"}, {"type": "fact", "angle": "語源・言葉"}] + others
        self.assertEqual(nh.choose_plan(ideas(), history)["angle"], "歴史・由来")

    def test_ties_follow_the_angle_list_not_the_llm_order(self):
        plan = nh.choose_plan(ideas(facts=(("心と体", "c"), ("動物・植物", "d"))), [])
        self.assertEqual(plan["angle"], "動物・植物")

    def test_fact_falls_back_to_the_oldest_angle(self):
        history = [{"type": "fact", "angle": "語源・言葉"}, {"type": "fact", "angle": "心と体"},
                   {"type": "fact", "angle": "歴史・由来"}, {"type": "question"},
                   {"type": "reframe"}, {"type": "metaphor"}]
        self.assertEqual(nh.choose_plan(ideas(), history)["angle"], "語源・言葉")

    def test_unknown_angles_are_dropped(self):
        plan = nh.choose_plan(ideas(facts=(("脳科学", "x"),), question="", reframe="",
                                    metaphor=""), [])
        # fact の候補が無く、他も空 → 全部の型から選び、切り口は一覧から決める
        self.assertEqual(plan["type"], "fact")
        self.assertIn(plan["angle"], nh.FACT_ANGLES)
        self.assertEqual(plan["idea"], "")

    def test_a_fact_without_its_subject_is_dropped(self):
        # 何についての事実かが無い候補は、気持ちの一般論（脳の報酬系など）になりやすい
        raw = ideas(question="", reframe="", metaphor="")
        raw["ideas"]["fact"][1]["about"] = ""
        plan = nh.choose_plan(raw, [])
        self.assertEqual(plan["angle"], "歴史・由来")

    def test_closing_forms_rotate(self):
        history = [{"type": "fact", "closing_form": "announce"}, {"type": "question", "closing_form": "call"}]
        self.assertEqual(nh.choose_plan(ideas(), history)["closing_form"], "because")
        self.assertEqual(nh.choose_plan(ideas(), [])["closing_form"], "announce")

    def test_fact_carries_what_it_is_about(self):
        plan = nh.choose_plan(ideas(), [])
        self.assertEqual((plan["type"], plan["about"]), ("fact", "ポケモン"))

    def test_excluded_types_are_not_chosen(self):
        # 豆知識を調べられなかった日は fact を外して次の型にする
        plan = nh.choose_plan(ideas(), [], exclude=("fact",))
        self.assertEqual((plan["type"], plan["idea"], plan["angle"]), ("question", "q", None))

    def test_a_failed_plan_still_rotates(self):
        history = [{"type": "fact", "angle": "語源・言葉"}]
        plan = nh.choose_plan(None, history)
        self.assertEqual(plan["type"], "question")
        self.assertIsNone(plan["picked_post"])


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "night_used.jsonl"

    def job(self, date, kind="fact"):
        return {"date": date, "hook_ja": "掴み" + date, "closing_ja": "締め",
                "plan": {"type": kind, "angle": "語源・言葉", "idea": "i", "closing_form": "call"}}

    def test_missing_ledger_is_empty(self):
        self.assertEqual(nh.load_recent(path=self.path), [])

    def test_written_rows_come_back_oldest_first(self):
        nh.append(self.job("2026-10-17"), path=self.path)
        nh.append(self.job("2026-10-16"), path=self.path)
        rows = nh.load_recent(path=self.path, today="2026-10-18")
        self.assertEqual([r["date"] for r in rows], ["2026-10-16", "2026-10-17"])
        self.assertEqual(rows[0]["hook"], "掴み2026-10-16")
        self.assertEqual(rows[0]["type"], "fact")
        self.assertEqual(rows[0]["closing_form"], "call")

    def test_old_rows_are_left_out(self):
        nh.append(self.job("2026-09-01"), path=self.path)
        nh.append(self.job("2026-10-17"), path=self.path)
        rows = nh.load_recent(days=14, path=self.path, today="2026-10-18")
        self.assertEqual([r["date"] for r in rows], ["2026-10-17"])

    def test_a_reshoot_on_the_same_day_keeps_the_last(self):
        nh.append(self.job("2026-10-17", "fact"), path=self.path)
        nh.append(self.job("2026-10-17", "metaphor"), path=self.path)
        rows = nh.load_recent(path=self.path, today="2026-10-18")
        self.assertEqual([r["type"] for r in rows], ["metaphor"])

    def test_broken_lines_are_skipped(self):
        self.path.write_text("{broken\n" + json.dumps({"date": "2026-10-17", "type": "fact"}) + "\n")
        self.assertEqual(len(nh.load_recent(path=self.path, today="2026-10-18")), 1)


if __name__ == "__main__":
    unittest.main()
