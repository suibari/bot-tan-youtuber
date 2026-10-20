import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from common import http_retry
from shorts import pickup_sources, pickup_research


def response(status=200, body=b'{}', headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.headers.update(headers or {})
    return r


class HttpRetryTest(unittest.TestCase):
    @patch.object(http_retry.time, 'sleep')
    def test_transient_errors_recover(self, sleep):
        for error in (requests.ReadTimeout(), requests.ConnectTimeout(),
                      requests.ConnectionError(), requests.exceptions.ChunkedEncodingError()):
            with self.subTest(error=type(error).__name__):
                call = Mock(side_effect=[error, response()])
                http_retry.request(call, 'https://example.test', label='test', timeout=(10, 20))
                self.assertEqual(call.call_count, 2)
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                call = Mock(side_effect=[response(status), response()])
                http_retry.request(call, 'https://example.test', label='test')
                self.assertEqual(call.call_count, 2)

    @patch.object(http_retry.time, 'sleep')
    def test_permanent_errors_are_not_retried(self, sleep):
        for error in (response(400), response(401), response(403), response(404),
                      requests.exceptions.SSLError()):
            call = Mock(side_effect=[error] if isinstance(error, Exception) else None,
                        return_value=error)
            with self.assertRaises(requests.RequestException):
                http_retry.request(call, 'https://example.test', label='test')
            self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    @patch.object(http_retry.time, 'sleep')
    def test_exhaustion_and_long_retry_after(self, sleep):
        call = Mock(side_effect=requests.ReadTimeout())
        with self.assertRaises(requests.ReadTimeout):
            http_retry.request(call, 'https://example.test', label='test')
        self.assertEqual(call.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])
        sleep.reset_mock()
        call = Mock(return_value=response(503, headers={'Retry-After': '600'}))
        with self.assertRaises(requests.HTTPError):
            http_retry.request(call, 'https://example.test', label='test')
        self.assertEqual(call.call_count, 1)
        sleep.assert_not_called()

    @patch.object(http_retry.time, 'sleep')
    @patch.object(pickup_sources.requests, 'get')
    def test_apple_timeout_and_image_disconnect(self, get, sleep):
        get.side_effect = [requests.ReadTimeout(), response(body=b'{"feed":{"results":[]}}')]
        self.assertEqual(pickup_sources.fetch_music_new(datetime.date(2026, 10, 21)), [])
        get.side_effect = [requests.ConnectionError(), response(body=b'image')]
        with tempfile.TemporaryDirectory() as directory:
            path = pickup_sources.download_artwork({'key': 'itunes:1', 'artwork': 'https://example.test/image'}, Path(directory))
            self.assertEqual(path.read_bytes(), b'image')

    @patch.object(http_retry.time, 'sleep')
    @patch.object(pickup_research.grounding, '_api_key', return_value='test')
    @patch.object(pickup_research.requests, 'post')
    def test_gemini_timeout_then_server_error(self, post, key, sleep):
        post.side_effect = [requests.ReadTimeout(), response(503), response(body=b'{"ok":true}')]
        self.assertEqual(pickup_research._ask('test', 'question'), {'ok': True})
        self.assertEqual(post.call_count, 3)
