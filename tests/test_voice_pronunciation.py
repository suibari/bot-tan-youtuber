import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


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


env_stub = types.ModuleType("common.env")
env_stub.env_flag = lambda _name, default=False: default
env_stub.env_float = lambda _name, default: default
env_stub.env_int = lambda _name, default: default
pronunciation_module = load_with_stubs(
    "pronunciation_test_module",
    ROOT / "common" / "pronunciation.py",
    {"common.env": env_stub},
)
PronunciationCache = pronunciation_module.PronunciationCache

pronunciation_stub = types.ModuleType("common.pronunciation")
pronunciation_stub.apply_pronunciations = lambda text: text
pronunciation_stub.preload_pronunciations = lambda: True
voice = load_with_stubs(
    "voice_pronunciation_test_module",
    ROOT / "common" / "voice.py",
    {"common.env": env_stub, "common.pronunciation": pronunciation_stub},
)


class PronunciationCacheTest(unittest.TestCase):
    def test_longest_surface_wins_in_a_single_pass(self):
        cache = PronunciationCache(loader=lambda: [
            ("攻殻", "コウカク"),
            ("攻殻機動隊", "コウカク、キドウタイ"),
        ], enabled=True)
        self.assertEqual(
            cache.apply("『攻殻機動隊』の人形遣い"),
            "『コウカク、キドウタイ』の人形遣い",
        )

    def test_ascii_surface_uses_word_boundaries(self):
        cache = PronunciationCache(
            loader=lambda: [("Nagi", "ナギ")], enabled=True)
        self.assertEqual(cache.apply("NagiとNagisa"), "ナギとNagisa")

    def test_separators_inside_the_surface_are_ignored(self):
        """登録は連結表記でも、喋るときは区切りが入る。

        2026-08-23 の配信で `ファイアーエムブレム万紫千紅` を登録してあったのに
        「ファイアーエムブレム 万紫千紅」と喋って素読みした。
        """
        cache = PronunciationCache(loader=lambda: [
            ("ファイアーエムブレム万紫千紅", "ファイアーエムブレム、バンシセンコウ"),
        ], enabled=True)
        for written in ("ファイアーエムブレム万紫千紅",
                        "ファイアーエムブレム 万紫千紅",
                        "ファイアーエムブレム\u3000万紫千紅",
                        "ファイアーエムブレム、万紫千紅",
                        "ファイアーエムブレム・万紫千紅"):
            with self.subTest(written=written):
                self.assertEqual(cache.apply(written),
                                 "ファイアーエムブレム、バンシセンコウ")

    def test_separator_is_only_allowed_where_the_script_changes(self):
        """どこにでも区切りを許すと、関係ない語に食い込む。"""
        cache = PronunciationCache(
            loader=lambda: [("アニメ", "アニメ映像")], enabled=True)
        self.assertEqual(cache.apply("アニ、メートル"), "アニ、メートル")

    def test_long_vowel_mark_is_not_a_boundary(self):
        """`ー` は読みの一部。ここで切れると別語に当たる。"""
        cache = PronunciationCache(
            loader=lambda: [("ラーメン", "ラーメン料理")], enabled=True)
        self.assertEqual(cache.apply("ラー、メン屋"), "ラー、メン屋")
        self.assertEqual(cache.apply("ラーメン屋"), "ラーメン料理屋")

    def test_ascii_case_variants_fall_back_to_the_registered_reading(self):
        cache = PronunciationCache(
            loader=lambda: [("YouTube", "ユーチューブ")], enabled=True)
        for written in ("YouTube", "youtube", "Youtube", "YOUTUBE"):
            with self.subTest(written=written):
                self.assertEqual(cache.apply(written), "ユーチューブ")

    def test_case_distinguishes_words_registered_as_different_readings(self):
        """`Halo`(ヘイロー) と `halo`(ハロー) は別語として登録されている。"""
        cache = PronunciationCache(loader=lambda: [
            ("Halo", "ヘイロー"),
            ("halo", "ハロー"),
        ], enabled=True)
        self.assertEqual(cache.apply("Halo"), "ヘイロー")
        self.assertEqual(cache.apply("halo"), "ハロー")

    def test_colliding_registrations_do_not_raise(self):
        """区切りと大小を潰すと衝突する登録があっても落ちない。"""
        cache = PronunciationCache(loader=lambda: [
            ("Umamusume Cinderella Grey", "ウマムスメ、シンデレラ、グレイ"),
            ("umamusume cinderella grey", "ウマムスメ、シンデレラ、グレイ"),
        ], enabled=True)
        self.assertEqual(cache.apply("Umamusume Cinderella Grey"),
                         "ウマムスメ、シンデレラ、グレイ")
        self.assertEqual(cache.apply("umamusume cinderella grey"),
                         "ウマムスメ、シンデレラ、グレイ")

    def test_both_spellings_are_found_when_both_are_registered(self):
        """区切りの有無だけが違う登録が両方あっても、どちらの書かれ方でも拾う。"""
        cache = PronunciationCache(loader=lambda: [
            ("Blue Sky", "ブルー、スカイ"),
            ("Bluesky", "ブルースカイ"),
        ], enabled=True)
        self.assertEqual(cache.apply("Bluesky"), "ブルースカイ")
        self.assertEqual(cache.apply("Blue Sky"), "ブルー、スカイ")

    def test_failed_refresh_keeps_last_good_cache(self):
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            if calls == 1:
                return [("作品名", "サクヒンメイ")]
            raise RuntimeError("database unavailable")

        cache = PronunciationCache(loader=loader, enabled=True)
        self.assertTrue(cache.preload())
        self.assertFalse(cache.preload())
        self.assertEqual(cache.apply("作品名だよ"), "サクヒンメイだよ")

    def test_disabled_cache_never_loads_or_rewrites(self):
        cache = PronunciationCache(
            loader=lambda: self.fail("loader must not run"), enabled=False)
        self.assertEqual(cache.apply("攻殻機動隊"), "攻殻機動隊")

    def test_regression_sentence_keeps_display_text_and_rewrites_speech_only(self):
        original = "わたしの一番好きなキャラは、『攻殻機動隊』の人形遣いだよ"
        cache = PronunciationCache(loader=lambda: [
            ("攻殻機動隊", "コウカク、キドウタイ"),
        ], enabled=True)
        spoken = cache.apply(original)
        self.assertEqual(
            spoken,
            "わたしの一番好きなキャラは、『コウカク、キドウタイ』の人形遣いだよ",
        )
        self.assertIn("攻殻機動隊", original)


class VoiceAudioQueryTest(unittest.TestCase):
    def test_audio_query_sends_rewritten_text_without_changing_caller_value(self):
        original = "攻殻機動隊の人形遣い"
        response = Mock()
        # _post_with_retry が 5xx を再試行の判定に使うので、Mock でも必ず立てること。
        # 立て忘れると `Mock < int` の TypeError になり、このテストが検証したい
        # 「送信テキスト」まで到達しない（2026-08-31 の _post_with_retry 追加以降そうなっていた）
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {"accent_phrases": []}
        with patch.object(voice, "apply_pronunciations",
                          return_value="コウカク、キドウタイの人形遣い"), \
                patch.object(voice.requests, "post", return_value=response) as post:
            voice.audio_query(original)
        self.assertEqual(
            post.call_args.kwargs["params"]["text"],
            "コウカク、キドウタイの人形遣い",
        )
        self.assertEqual(original, "攻殻機動隊の人形遣い")


if __name__ == "__main__":
    unittest.main()
