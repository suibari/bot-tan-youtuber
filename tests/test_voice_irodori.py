"""Irodori の API 契約と VOICEVOX へのフォールバック。"""
import io
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from common import voice


def wav_bytes(samples=(1000, -1000), rate=24000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        wav.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    return output.getvalue()


class IrodoriTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'out.wav'
        for name, value in [('TTS_ENGINE', 'irodori'), ('IRODORI_SPEED', 1.2),
                            ('IRODORI_VOICE', 'tsumugi'), ('IRODORI_HOOK_VOICE', 'hook')]:
            patcher = patch.object(voice, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(voice, 'apply_pronunciations', side_effect=lambda text: text)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pronunciation_speed_hook_volume_and_wait(self):
        response = Mock(content=wav_bytes((30000, -30000)))
        with patch.object(voice, 'apply_pronunciations', return_value='ボットたん'), \
                patch.object(voice.requests, 'post', return_value=response) as post:
            voice.synthesize('botたん', self.output, voice.HOOK_VOICE_PARAMS, wait_load=False)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['text'], 'ボットたん')
        self.assertEqual(payload['voice'], 'hook')
        self.assertAlmostEqual(payload['speed'], 1.2 * .85)
        self.assertFalse(payload['wait_load'])
        self.assertNotIn('pitchScale', payload)
        with wave.open(str(self.output)) as wav:
            self.assertEqual(struct.unpack('<2h', wav.readframes(2)), (32767, -32768))

    def test_long_rewritten_text_is_split_and_joined(self):
        text = 'あ' * 299 + '。' + 'い' * 301
        with patch.object(voice.requests, 'post', return_value=Mock(content=wav_bytes())) as post:
            voice.synthesize(text, self.output)
        chunks = [call.kwargs['json']['text'] for call in post.call_args_list]
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))
        with wave.open(str(self.output)) as wav:
            self.assertEqual(wav.getnframes(), 6)

    def test_http_errors_and_connection_failure_fall_back_without_retry(self):
        for error in [requests.HTTPError('503'), requests.HTTPError('400'),
                      requests.ConnectionError(), requests.Timeout()]:
            with self.subTest(error=error), \
                    patch.object(voice.requests, 'post', side_effect=error) as post, \
                    patch.object(voice, '_synthesize_voicevox') as fallback:
                voice.synthesize('こんにちは', self.output, voice.HOOK_VOICE_PARAMS)
                post.assert_called_once()
                fallback.assert_called_once_with('こんにちは', self.output, voice.HOOK_VOICE_PARAMS)

    def test_invalid_wav_falls_back(self):
        for audio in [b'not wav', wav_bytes(rate=48000), wav_bytes(samples=())]:
            with patch.object(voice.requests, 'post', return_value=Mock(content=audio)), \
                    patch.object(voice, '_synthesize_voicevox') as fallback:
                voice.synthesize('こんにちは', self.output)
                fallback.assert_called_once()

    def test_voicevox_selection_skips_irodori(self):
        with patch.object(voice, 'TTS_ENGINE', 'voicevox'), \
                patch.object(voice.requests, 'post') as post, \
                patch.object(voice, '_synthesize_voicevox') as fallback:
            voice.synthesize('こんにちは', self.output)
        post.assert_not_called()
        fallback.assert_called_once()

    def test_live_does_not_wait_for_load(self):
        def synthesize(text, output, params, **kwargs):
            Path(output).write_bytes(wav_bytes())
        with patch.object(voice, 'synthesize', side_effect=synthesize) as synth:
            voice.synthesize_lines([{'ja': 'こんにちは'}], self.tmp.name, 'live')
        self.assertFalse(synth.call_args.kwargs['wait_load'])

    def test_warmup_waits_for_load(self):
        with patch.object(voice, 'synthesize') as synth:
            voice.warmup()
        self.assertTrue(synth.call_args.kwargs['wait_load'])

    def test_health_checks_fallback_and_irodori(self):
        fallback = Mock()
        fallback.json.return_value = [{'name': 'つむぎ', 'styles': [{'id': voice.VOICEVOX_SPEAKER, 'name': 'ノーマル'}]}]
        primary = Mock()
        primary.json.return_value = {'voices': ['tsumugi'], 'loaded': False}
        with patch.object(voice.requests, 'get', side_effect=[fallback, primary]):
            self.assertIn('Irodori', voice.health_check())
        with patch.object(voice.requests, 'get', side_effect=[fallback, requests.ConnectionError()]):
            self.assertIn('つむぎ', voice.health_check())

    def test_unload_failure_is_nonfatal(self):
        with patch.object(voice.requests, 'post', side_effect=requests.ConnectionError()) as post:
            voice.unload_irodori()
        self.assertTrue(post.call_args.args[0].endswith('/unload'))


if __name__ == '__main__':
    unittest.main()
