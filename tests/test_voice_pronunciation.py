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
