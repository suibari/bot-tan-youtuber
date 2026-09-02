"""配信ループの分岐（掘り下げ・クロージング直前の沈黙）。

コメント > クロージング直前は黙る > 掘り下げ > フリートーク の順で、
「何も喋らない時間」が伸びないこと。run_loop は LIVE_CLOSING_HHMM で抜けて
しまい、それ以降に届いたコメントには一切反応できないので、終了間際に長い
独り言を始めさせない。
"""

import importlib.util
import sys
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "live"


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def load_live():
    """live.py を、外の世界に触るものを全部差し替えて読み込む。"""
    config_stub = _stub(
        "config",
        DRY_RUN=True, ENERGY_REFRESH_SEC=30,
        FILLER_IDLE_SEC=25.0, FILLER_STOP_LEAD_SEC=120.0,
        FOLLOWUP_IDLE_SEC=8.0, FOLLOWUP_MAX_DEPTH=2, FOLLOWUP_TTL_SEC=120.0,
        IDLE_ENABLED=True, LIVE_CLOSING_HHMM="21:55", LIVE_END_HHMM="22:00",
        LIVE_GO_LIVE_RETRY_SEC=300, LIVE_START_HHMM="21:00",
        LIVE_TESTING_LEAD_SEC=120, BOT_CONTEXT_TTL_SEC=20.0,
        BOT_MOOD_SERVE_LIMIT=1, FPS_LOG_SEC=60.0,
        LIVE_HISTORY_TURNS=6, LIVE_HISTORY_USER_TURNS=3, SKIP_ARDY=True,
        SUBTITLE_LEAD_SEC=0.0, UNITY_PROJECT="/tmp/unity",
        UNITY_RESTART_MAX=2, UNITY_RESTART_TIMEOUT_SEC=120.0,
        UNITY_RESTART_COOLDOWN_SEC=30.0,
        WORK_DIR=Path("/tmp/bottan-live-test"), ensure_dirs=lambda: None,
    )
    stubs = {
        "config": config_stub,
        "chat": _stub("chat", CommentQueue=object, make_poller=lambda *a, **k: None),
        "conversation": _stub("conversation", ConversationLog=object),
        "energy": _stub("energy", get_energy=lambda: 50.0),
        "filler": _stub("filler", FillerPlanner=object),
        "idle": _stub("idle", IdleAnimator=object),
        "llm": _stub("llm", generate_reply=lambda *a, **k: {}),
        "memory": _stub("memory", BotMemoryWriter=object),
        "gauge": _stub("gauge", write=lambda *a: None),
        "motion": _stub("motion", MotionPool=object, ArdyWorker=object),
        "notify": _stub("notify", warn=lambda *a: None, error=lambda *a: None),
        "persona": _stub("persona", build_system_prompt=lambda: ""),
        # recall は BotMemoryClient と DB を掴むので差し替える
        "recall": _stub("recall", CommentRecall=object,
                        subject_of=lambda _text: ""),
        "safety": _stub("safety"),
        "schedule": _stub("schedule"),
        "subtitle": _stub("subtitle", SubtitleScheduler=object),
        # topics は依存ゼロなので実物を読ませる（mood の消費判定を通したい）
        "unity_client": _stub("unity_client", UnityError=RuntimeError),
        "unity_live": _stub("unity_live", UnityLive=object),
        "voice": _stub("voice"),
        # live.py は `from common import grounding` で読む（live/ に同名モジュールは
        # 無い）。実物は import しただけでは外に触らないが、needs_lookup / lookup は
        # ollama と Gemini を叩くので、ここでも差し替えておく
        "common.grounding": _stub(
            "common.grounding", SKIP="skip", UNKNOWN="unknown", FACTS="facts",
            WEB="web", SELF="self", NONE="none",
            classify=lambda _text: ("none", ""),
            needs_lookup=lambda _text: False,
            lookup=lambda _q: {"status": "skip", "facts": "", "queries": []},
            warmup=lambda: None),
    }
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(
            "live_loop_test_module", LIVE / "live.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


live = load_live()


class FakePlanner:
    def __init__(self):
        self.themes = []

    def set_followup_theme(self, theme):
        self.themes.append(theme)


def bare_session():
    """__init__ を通さずに、分岐に要るものだけ持った LiveSession を作る。"""
    session = object.__new__(live.LiveSession)
    session._thread = None
    session.last_speech_at = time.monotonic()
    session.planner = FakePlanner()
    session._mood_state = {"text": "", "served": 0}
    return session


MOOD = ("全肯定botたんは、お風呂で「FLASHBULB」を聴きながら、"
        "今日の出来事を思い出しているよ。")


class MoodServingTest(unittest.TestCase):
    """mood を全プロンプトに載せ続けない。

    mood は biorhythm 由来で 20〜90分ごとにしか変わらないので、素で入れると
    1時間の配信の全発話に同じ文が乗る。2026-08-24 の配信で「FLASHBULB」の話が
    61発話中14回出たのがこれ。
    """

    def test_the_mood_is_served_once(self):
        session = bare_session()
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], MOOD)
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], "")

    def test_it_stays_dropped(self):
        session = bare_session()
        session._bot_for_prompt({"mood": MOOD})
        for _ in range(5):
            self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], "")

    def test_a_new_mood_starts_over(self):
        """20〜90分ごとの更新で復活する。配信中に2〜3回は近況を話せる。"""
        session = bare_session()
        session._bot_for_prompt({"mood": MOOD})
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], "")
        other = "モルフォと公園を散歩していた"
        self.assertEqual(session._bot_for_prompt({"mood": other})["mood"], other)

    def test_the_original_mood_is_kept_for_the_memory_block(self):
        """載せないと決めたあとも、記憶ブロックからは同じ話を落としたい。"""
        session = bare_session()
        session._bot_for_prompt({"mood": MOOD})
        bot = session._bot_for_prompt({"mood": MOOD})
        self.assertEqual(bot["mood"], "")
        self.assertEqual(bot["mood_raw"], MOOD)

    def test_peeking_does_not_spend_the_budget(self):
        """雑務スレッド（RAG 先読み）は消費せずに覗くだけ。"""
        session = bare_session()
        for _ in range(10):
            session._bot_for_prompt({"mood": MOOD}, consume=False)
        self.assertEqual(session._bot_for_prompt({"mood": MOOD})["mood"], MOOD)

    def test_an_empty_mood_is_harmless(self):
        session = bare_session()
        self.assertEqual(session._bot_for_prompt({})["mood_raw"], "")
        self.assertEqual(session._bot_for_prompt({"mood": ""})["mood_raw"], "")


class ClosingSoonTest(unittest.TestCase):
    def test_it_is_quiet_inside_the_lead(self):
        session = bare_session()
        closing_at = datetime.now() + timedelta(seconds=30)
        self.assertTrue(session._closing_soon(closing_at))

    def test_it_still_talks_well_before_closing(self):
        session = bare_session()
        closing_at = datetime.now() + timedelta(seconds=600)
        self.assertFalse(session._closing_soon(closing_at))


class ThreadTest(unittest.TestCase):
    def test_beginning_a_thread_hands_the_theme_to_the_planner(self):
        session = bare_session()
        session._begin_thread("モルフォに起こされた話")
        self.assertEqual(session.planner.themes, ["モルフォに起こされた話"])
        self.assertEqual(session._thread["depth"], 0)

    def test_an_empty_theme_clears_the_thread(self):
        session = bare_session()
        session._begin_thread("なにか")
        session._begin_thread("   ")
        self.assertIsNone(session._thread)

    def test_a_planner_failure_does_not_break_the_stream(self):
        session = bare_session()

        def explode(_theme):
            raise RuntimeError("bot memory is down")

        session.planner.set_followup_theme = explode
        session._begin_thread("なにかの話")           # 例外が漏れないこと
        self.assertIsNotNone(session._thread)


class FollowupDueTest(unittest.TestCase):
    def test_nothing_to_dig_into_without_a_thread(self):
        self.assertFalse(bare_session()._followup_due())

    def test_it_waits_for_the_silence_to_open_up(self):
        session = bare_session()
        session._begin_thread("なにかの話")
        session.last_speech_at = time.monotonic()          # いま喋り終わった
        self.assertFalse(session._followup_due())
        session.last_speech_at = time.monotonic() - 9      # 9秒空いた
        self.assertTrue(session._followup_due())

    def test_it_stops_at_the_depth_limit(self):
        session = bare_session()
        session._begin_thread("なにかの話")
        session.last_speech_at = time.monotonic() - 30
        session._thread["depth"] = live.FOLLOWUP_MAX_DEPTH
        self.assertFalse(session._followup_due())

    def test_a_stale_theme_is_dropped(self):
        """古い話題を蒸し返さない。"""
        session = bare_session()
        session._begin_thread("ずっと前の話")
        session.last_speech_at = time.monotonic() - 30
        session._thread["at"] = time.monotonic() - live.FOLLOWUP_TTL_SEC - 1
        self.assertFalse(session._followup_due())
        self.assertIsNone(session._thread)

    def test_the_silence_never_gets_longer_than_the_filler_threshold(self):
        """掘り下げはフリートークより先に出ること。

        逆だと、掘り下げられるテーマがあるのに FILLER_IDLE_SEC だけ黙る。
        """
        self.assertLess(live.FOLLOWUP_IDLE_SEC, live.FILLER_IDLE_SEC)


class UnityRecoveryTest(unittest.TestCase):
    class FakeUnity:
        def __init__(self, start_error=None):
            self.alive = False
            self.started = []
            self.start_error = start_error

        def is_alive(self):
            return self.alive

        def start(self, ready_timeout):
            self.started.append(ready_timeout)
            if self.start_error:
                raise self.start_error
            self.alive = True

    class FakeObs:
        def __init__(self):
            self.bound = []

        def bind_window_capture(self, project):
            self.bound.append(project)

    def make_session(self, unity=None):
        session = bare_session()
        session.unity = unity or self.FakeUnity()
        session.obs = self.FakeObs()
        session._unity_restart_count = 0
        session._unity_restart_after = 0.0
        return session

    def test_restart_rebinds_obs_to_the_new_window(self):
        session = self.make_session()
        self.assertTrue(session._recover_unity())
        self.assertEqual(session.unity.started, [live.UNITY_RESTART_TIMEOUT_SEC])
        self.assertEqual(session.obs.bound, [live.UNITY_PROJECT])
        self.assertEqual(session._unity_restart_count, 1)

    def test_a_live_process_is_not_restarted(self):
        session = self.make_session()
        session.unity.alive = True
        self.assertTrue(session._recover_unity(force=True))
        self.assertEqual(session.unity.started, [])

    def test_restart_budget_prevents_an_endless_crash_loop(self):
        session = self.make_session(self.FakeUnity(RuntimeError("crashed")))
        for _ in range(live.UNITY_RESTART_MAX):
            session._unity_restart_after = 0.0
            self.assertFalse(session._recover_unity(force=True))
        self.assertFalse(session._recover_unity(force=True))
        self.assertEqual(len(session.unity.started), live.UNITY_RESTART_MAX)


if __name__ == "__main__":
    unittest.main()
