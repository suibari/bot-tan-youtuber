"""GPU不足時も音声を優先し、ARDY停止・音声再試行を一度に制限する。"""
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from common import ardy, gpu_recovery, voice
from tests.test_voice_irodori import wav_bytes


def http_error(code, status=503):
    response = requests.Response()
    response.status_code = status
    response._content = ('{"detail":{"code":"' + code + '"}}').encode()
    return requests.HTTPError(response=response)


class RecoveryTest(unittest.TestCase):
    def tearDown(self):
        gpu_recovery.set_handler(None)

    def test_only_explicit_memory_errors_trigger_recovery(self):
        handler = Mock()
        gpu_recovery.set_handler(handler)
        for error in ['loading', '503 Service Unavailable', 'Read timed out', 'out of memory on CPU']:
            self.assertFalse(gpu_recovery.recover('test', error))
        self.assertTrue(gpu_recovery.recover('test', 'CUDA error: out of memory'))
        handler.assert_called_once()

    def synthesize(self, side_effect, fallback):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(voice, 'TTS_ENGINE', 'irodori'), \
                patch.object(voice, '_irodori_wav', side_effect=side_effect) as primary, \
                patch.object(voice, '_synthesize_voicevox', fallback):
            result = voice.synthesize('クイズだよ！', Path(tmp) / 'out.wav', wait_load=False)
        return result, primary

    def test_oom_stops_ardy_then_retries_same_sentence(self):
        handler, fallback = Mock(), Mock()
        gpu_recovery.set_handler(handler)
        result, primary = self.synthesize([http_error('cuda_oom'), wav_bytes()], fallback)
        self.assertEqual(result, 'irodori')
        handler.assert_called_once()
        self.assertIsNotNone(handler.call_args.kwargs['deadline'])
        self.assertTrue(primary.call_args.args[2])
        self.assertEqual(primary.call_count, 2)
        fallback.assert_not_called()

    def test_loading_waits_without_stopping_ardy(self):
        handler, fallback = Mock(), Mock()
        gpu_recovery.set_handler(handler)
        with patch.object(voice.time, 'sleep'):
            result, _ = self.synthesize([http_error('loading'), wav_bytes()], fallback)
        self.assertEqual(result, 'irodori')
        handler.assert_not_called()
        fallback.assert_not_called()

    def test_repeated_oom_falls_back_only_once(self):
        handler, fallback = Mock(), Mock()
        gpu_recovery.set_handler(handler)
        result, primary = self.synthesize([http_error('insufficient_vram'), http_error('cuda_oom')], fallback)
        self.assertEqual(result, 'voicevox')
        handler.assert_called_once()
        fallback.assert_called_once()
        self.assertEqual(primary.call_count, 2)

    def test_expired_deadline_prevents_another_http_request(self):
        token = voice._deadline.set(5)
        try:
            with patch.object(voice.time, 'monotonic', return_value=6), \
                    patch.object(voice.requests, 'post') as post:
                with self.assertRaises(requests.Timeout):
                    voice._request_timeout()
                post.assert_not_called()
        finally:
            voice._deadline.reset(token)

    def test_ollama_oom_releases_ardy_before_retry(self):
        from tests.test_llm_local import load_local_llm, FakeResponse
        local = load_local_llm()
        handler = Mock()
        gpu_recovery.set_handler(handler)
        responses = [FakeResponse('CUDA error: out of memory', 500),
                     FakeResponse({'message': {'content': 'はい'}, 'done_reason': 'stop'})]
        with patch.object(local.requests, 'post', side_effect=responses), \
                patch.object(local.time, 'sleep'):
            result = local.create(messages=[{'role': 'user', 'content': 'hi'}])
        handler.assert_called_once()
        self.assertEqual(result.choices[0].message.content, 'はい')

    def test_ardy_generation_oom_notifies_session(self):
        handler = Mock()
        gpu_recovery.set_handler(handler)
        response = Mock()
        response.json.return_value = {'error': 'CUDA out of memory'}
        with patch.object(ardy.requests, 'post', return_value=response):
            self.assertIsNone(ardy.generate_spec('/tmp/unused.json', text='wave', duration=1))
        handler.assert_called_once()

    def test_failed_release_prevents_ardy_start(self):
        with patch.object(ardy, 'available', return_value=True), \
                patch.object(voice, 'unload_irodori', return_value=False), \
                patch.object(ardy, 'wait_memory') as memory, \
                patch.object(ardy.subprocess, 'Popen') as spawn:
            self.assertIsNone(ardy.start())
        memory.assert_not_called()
        spawn.assert_not_called()

    def test_live_start_does_not_unload_tts(self):
        with patch.object(ardy, 'available', return_value=True), \
                patch.object(voice, 'unload_irodori') as unload, \
                patch.object(ardy, 'wait_memory', return_value=False):
            ardy.start(release_tts=False)
        unload.assert_not_called()


class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # motion.py の単体テストで配信・DB・LLM接続を起動しない。
        config = types.ModuleType('config')
        config.MOTION_POOL_DIR = Path('/tmp/unused-motion-test')
        config.WORK_DIR = Path('/tmp')
        config.env_float = lambda key, default: float(default)
        config.env_int = lambda key, default: int(default)
        llm = types.ModuleType('llm')
        llm.MOTION_CATEGORIES = []
        safety = types.ModuleType('safety')
        spec = importlib.util.spec_from_file_location('motion_recovery_test', Path(__file__).parents[1] / 'live/motion.py')
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, config=config, llm=llm, safety=safety):
            spec.loader.exec_module(cls.module)

    def test_concurrent_oom_stops_once_and_disables_restart(self):
        worker = self.module.ArdyWorker(Mock())
        worker.enabled = True
        worker._proc = Mock()
        worker._queue.put(('one', 'test'))
        worker._prewarm.put(('two', 'test'))
        with patch.object(ardy, 'stop') as stop, patch.object(ardy, 'vram_free_gb', return_value=3):
            threads = [threading.Thread(target=worker.disable_for_oom, args=('cuda_oom',)) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            stop.assert_called_once()
        self.assertFalse(worker.enabled)
        self.assertTrue(worker._queue.empty())
        self.assertTrue(worker._prewarm.empty())
        with patch.object(self.module, 'start_server') as start:
            self.assertFalse(worker.start())
        start.assert_not_called()

    def test_stop_from_worker_does_not_join_itself(self):
        worker = self.module.ArdyWorker(Mock())
        worker._thread = threading.current_thread()
        with patch.object(self.module, 'stop_server'):
            worker.stop()


if __name__ == '__main__':
    unittest.main()
