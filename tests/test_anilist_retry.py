import unittest
from unittest.mock import Mock, patch

import requests
from shorts import pickup_sources as s


class AniListRetryTest(unittest.TestCase):
    def response(self, status, headers=None):
        r = requests.Response()
        r.status_code = status
        r.headers.update(headers or {})
        r._content = b'{"data":{"Page":{"media":[]}}}'
        return r

    @patch.object(s.time, 'sleep')
    @patch.object(s.requests, 'post')
    def test_recovers_after_server_requested_wait(self, post, sleep):
        post.side_effect = [self.response(429, {'Retry-After': '30'}), self.response(200)]
        self.assertEqual(s._anilist('type:ANIME'), [])
        sleep.assert_called_once_with(31)
        self.assertEqual(post.call_count, 2)

    @patch.object(s.time, 'sleep')
    @patch.object(s.requests, 'post')
    def test_stops_after_three_attempts(self, post, sleep):
        post.return_value = self.response(429)
        with self.assertRaises(requests.HTTPError):
            s._anilist('type:ANIME')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch.object(s.time, 'sleep')
    @patch.object(s.requests, 'post')
    def test_does_not_retry_other_errors_or_long_limits(self, post, sleep):
        for response in [self.response(400), self.response(429, {'Retry-After': '600'})]:
            post.reset_mock()
            post.return_value = response
            with self.assertRaises(requests.HTTPError):
                s._anilist('type:ANIME')
            self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    @patch.object(s.time, 'time', return_value=1000)
    def test_reset_date_and_invalid_headers(self, clock):
        self.assertEqual(s._anilist_retry_delay({'X-RateLimit-Reset': '1040', 'Retry-After': '10'}), 41)
        self.assertEqual(s._anilist_retry_delay({'Retry-After': 'Thu, 01 Jan 1970 00:17:10 GMT'}), 31)
        self.assertEqual(s._anilist_retry_delay({'Retry-After': 'nan', 'X-RateLimit-Reset': 'bad'}), 61)
