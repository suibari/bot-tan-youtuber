"""create() の再試行に待ちが入ること。

以前は待ちが一切無く、429（レート制限）を食うと3回を一瞬で使い切って次の
モデルへ移っていた。間を置かずに投げ直すのはレート制限を悪化させるだけで、
そのぶん配信のメインループが止まる（LLM を呼ぶのはメインループだけ）。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_llm(models):
    """common/llm.py を、OpenAI クライアントを差し替えて読み込む。"""
    openai_stub = types.ModuleType("openai")

    class BadRequestError(Exception):
        pass

    class OpenAI:
        def __init__(self, *_args, **_kwargs):
            self.chat = types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **kw: None))

    openai_stub.OpenAI = OpenAI
    openai_stub.BadRequestError = BadRequestError

    env_stub = types.ModuleType("common.env")
    env_stub.env_flag = lambda _name, default=False: default
    env_stub.env_float = lambda _name, default: default
    env_stub.env_int = lambda _name, default: default

    stubs = {"openai": openai_stub, "common.env": env_stub}
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        with patch.dict("os.environ", {"GEMINI_MODEL": ",".join(models)}):
            spec = importlib.util.spec_from_file_location(
                "llm_backoff_test_module", ROOT / "common" / "llm.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return module, BadRequestError


class RetryBackoffTest(unittest.TestCase):
    def test_failures_wait_before_retrying(self):
        llm, _ = load_llm(["model-a"])
        slept = []
        calls = []

        def always_fails(**_kwargs):
            calls.append(1)
            raise RuntimeError("429 rate limit")

        llm.client.chat.completions.create = always_fails
        with patch.object(llm.time, "sleep", slept.append):
            with self.assertRaises(RuntimeError):
                llm.create(attempts_per_model=3, messages=[])

        self.assertEqual(len(calls), 3)
        # 最後の失敗のあとは待たない。そのまま次のモデルへ移る
        self.assertEqual(len(slept), 2)
        self.assertEqual(slept, list(llm.LLM_RETRY_BACKOFF_SEC[:2]))

    def test_backoff_grows(self):
        llm, _ = load_llm(["model-a"])
        self.assertEqual(sorted(llm.LLM_RETRY_BACKOFF_SEC),
                         list(llm.LLM_RETRY_BACKOFF_SEC))
        self.assertGreater(llm.LLM_RETRY_BACKOFF_SEC[-1],
                           llm.LLM_RETRY_BACKOFF_SEC[0])

    def test_a_success_never_waits(self):
        llm, _ = load_llm(["model-a"])
        llm.client.chat.completions.create = lambda **_kw: "ok"
        with patch.object(llm.time, "sleep", lambda *_a: self.fail("待ってはいけない")):
            self.assertEqual(llm.create(messages=[]), "ok")

    def test_bad_request_raises_immediately_without_waiting(self):
        """400系はプロンプトかスキーマの問題。投げ直しても無駄。"""
        llm, bad_request = load_llm(["model-a", "model-b"])
        calls = []

        def rejects(**_kwargs):
            calls.append(1)
            raise bad_request("schema is wrong")

        llm.client.chat.completions.create = rejects
        with patch.object(llm.time, "sleep", lambda *_a: self.fail("待ってはいけない")):
            with self.assertRaises(bad_request):
                llm.create(messages=[])
        self.assertEqual(len(calls), 1)

    def test_worst_case_wait_stays_bounded(self):
        """待ちすぎてもメインループが止まる。頭を打たせてあること。"""
        llm, _ = load_llm(["model-a", "model-b"])
        slept = []

        def always_fails(**_kwargs):
            raise RuntimeError("boom")

        llm.client.chat.completions.create = always_fails
        with patch.object(llm.time, "sleep", slept.append):
            with self.assertRaises(RuntimeError):
                llm.create(attempts_per_model=3, messages=[])
        self.assertLessEqual(sum(slept), 10.0)


if __name__ == "__main__":
    unittest.main()
