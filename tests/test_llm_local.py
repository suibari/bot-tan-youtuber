"""ローカル（Ollama）経路のリクエストの形。

**ここで守りたいのは1点だけ: 送っているものが本当に届いていること。**
OpenAI 互換の `/v1/chat/completions` に `extra_body` で `options` を渡していた頃、
Ollama はそれを**エラーにせず黙って捨てて**いた。結果 num_ctx は既定の 4096 に落ち、
9,000字超のペルソナを載せた配信の返答が空文字で返る。エラーが出ないので、
壊れていることに気付く手立てが無かった。

だからテストは「送信ボディの中身」を見る。
"""

import importlib.util
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_local_llm():
    """common/llm.py を USE_LOCAL_LLM=true で読み込む。"""
    openai_stub = types.ModuleType("openai")

    class BadRequestError(Exception):
        pass

    class OpenAI:
        def __init__(self, *_a, **_kw):
            raise AssertionError("ローカル経路で Gemini クライアントを作ってはいけない")

    openai_stub.OpenAI = OpenAI
    openai_stub.BadRequestError = BadRequestError

    env_stub = types.ModuleType("common.env")
    env_stub.env_flag = lambda name, default=False: (
        True if name == "USE_LOCAL_LLM" else default)
    env_stub.env_float = lambda _name, default: default
    env_stub.env_int = lambda _name, default: default

    stubs = {"openai": openai_stub, "common.env": env_stub}
    previous = {key: sys.modules.get(key) for key in stubs}
    try:
        sys.modules.update(stubs)
        spec = importlib.util.spec_from_file_location(
            "llm_local_test_module", ROOT / "common" / "llm.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    return module


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def ok(content="{}"):
    return FakeResponse({"message": {"content": content}, "done_reason": "stop"})


SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


class OllamaRequestTest(unittest.TestCase):
    def setUp(self):
        self.llm = load_local_llm()

    def _send(self, **kwargs):
        """create() を1回呼び、実際に送られたボディを返す。"""
        sent = {}

        def fake_post(url, json=None, timeout=None):
            sent["url"] = url
            sent["body"] = json
            sent["timeout"] = timeout
            return ok('{"a": "b"}')

        with patch.object(self.llm.requests, "post", fake_post):
            response = self.llm.create(**kwargs)
        return sent, response

    def test_it_uses_the_native_endpoint_not_the_openai_one(self):
        """/v1/chat/completions では num_ctx も think も渡せない。"""
        sent, _ = self._send(messages=[])
        self.assertTrue(sent["url"].endswith("/api/chat"), sent["url"])
        self.assertNotIn("/v1", sent["url"])

    def test_num_ctx_is_always_sent(self):
        """既定の 4096 に落ちるとペルソナが入りきらず、応答が空文字で返る。"""
        sent, _ = self._send(messages=[])
        self.assertEqual(sent["body"]["options"]["num_ctx"], self.llm.OLLAMA_NUM_CTX)

    def test_num_ctx_matches_the_shared_runner(self):
        """bsky-affirmative-bot と同じ値。ずれると 26B runner が再ロードされる。"""
        self.assertEqual(self.llm.OLLAMA_NUM_CTX, 32768)

    def test_thinking_is_always_off(self):
        """切らないと reasoning が生成上限を食って content が空になる。"""
        sent, _ = self._send(messages=[])
        self.assertIs(sent["body"]["think"], False)

    def test_schema_becomes_ollama_format(self):
        sent, _ = self._send(messages=[], response_format={
            "type": "json_schema",
            "json_schema": {"name": "script", "schema": SCHEMA}})
        self.assertEqual(sent["body"]["format"], SCHEMA)

    def test_no_format_when_no_schema(self):
        sent, _ = self._send(messages=[])
        self.assertNotIn("format", sent["body"])

    def test_openai_style_knobs_move_into_options(self):
        sent, _ = self._send(messages=[], temperature=0.7, max_tokens=5)
        self.assertEqual(sent["body"]["options"]["temperature"], 0.7)
        self.assertEqual(sent["body"]["options"]["num_predict"], 5)

    def test_response_looks_like_the_openai_one(self):
        """呼び出し側（shorts/pipeline.py）を書き換えずに済ませるため。"""
        _, response = self._send(messages=[])
        self.assertEqual(response.choices[0].message.content, '{"a": "b"}')
        self.assertEqual(response.choices[0].finish_reason, "stop")

    def test_400_is_not_retried(self):
        """プロンプトかスキーマの問題。投げ直しても無駄で、そのぶん配信が止まる。"""
        calls = []

        def rejects(url, json=None, timeout=None):
            calls.append(1)
            return FakeResponse("bad schema", status_code=400)

        with patch.object(self.llm.requests, "post", rejects):
            with patch.object(self.llm.time, "sleep",
                              lambda *_a: self.fail("待ってはいけない")):
                with self.assertRaises(self.llm.OllamaBadRequest):
                    self.llm.create(messages=[])
        self.assertEqual(len(calls), 1)

    def test_other_errors_are_retried_with_backoff(self):
        slept = []

        def boom(url, json=None, timeout=None):
            return FakeResponse("upstream", status_code=503)

        with patch.object(self.llm.requests, "post", boom):
            with patch.object(self.llm.time, "sleep", slept.append):
                with self.assertRaises(RuntimeError):
                    self.llm.create(attempts_per_model=3, messages=[])
        self.assertEqual(slept, list(self.llm.LLM_RETRY_BACKOFF_SEC[:2]))

    def test_generate_json_sends_the_schema(self):
        """generate_json は経路によらずスキーマを渡す（以前はローカルだけ渡していなかった）。"""
        sent = {}

        def fake_post(url, json=None, timeout=None):
            sent["body"] = json
            return ok('{"a": "b"}')

        with patch.object(self.llm.requests, "post", fake_post):
            data = self.llm.generate_json("system", "user", SCHEMA, debug=False)
        self.assertEqual(data, {"a": "b"})
        self.assertEqual(sent["body"]["format"], SCHEMA)
        self.assertEqual([m["role"] for m in sent["body"]["messages"]],
                         ["system", "user"])


if __name__ == "__main__":
    unittest.main()
