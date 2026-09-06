"""RESTとstreamListの共通OAuth取得。ネットワークとトークン保存は模擬する。"""

import unittest
from unittest.mock import Mock, patch

from common import youtube_auth as auth


class CredentialsTest(unittest.TestCase):
    def credentials(self, expired=False):
        return Mock(expired=expired, valid=True, refresh_token="refresh",
                    scopes=auth.SCOPES)

    def test_valid_credentials_do_not_refresh(self):
        credentials = self.credentials()
        with patch.object(auth, "_load_creds", return_value=credentials), \
             patch.object(auth, "_save_creds") as save:
            self.assertIs(auth.get_credentials(), credentials)
        credentials.refresh.assert_not_called()
        save.assert_not_called()

    def test_expired_or_rejected_token_refreshes_with_bounded_request(self):
        for expired, force in ((True, False), (False, True)):
            with self.subTest(expired=expired, force=force):
                credentials = self.credentials(expired)
                with patch.object(auth, "_load_creds", return_value=credentials), \
                     patch.object(auth, "_save_creds") as save:
                    self.assertIs(auth.get_credentials(force_refresh=force), credentials)
                self.assertEqual(credentials.refresh.call_args.args[0].keywords["timeout"], 20)
                save.assert_called_once_with(credentials)

    def test_missing_token_never_starts_interactive_auth(self):
        with patch.object(auth, "_load_creds", return_value=None):
            with self.assertRaises(auth.YouTubeAuthError): auth.get_credentials()

    def test_missing_scope_is_rejected(self):
        credentials = self.credentials()
        credentials.scopes = []
        with patch.object(auth, "_load_creds", return_value=credentials):
            with self.assertRaises(auth.YouTubeAuthError): auth.get_credentials()

    def test_rest_client_still_caches_and_uses_common_credentials(self):
        with patch.object(auth, "_client", None), \
             patch.object(auth, "get_credentials", return_value="credentials") as get, \
             patch("googleapiclient.discovery.build", return_value="client") as build:
            self.assertEqual(auth.get_client(), "client")
            self.assertEqual(auth.get_client(), "client")
            get.assert_called_once_with(False)
            build.assert_called_once_with("youtube", "v3", credentials="credentials", cache_discovery=False)


if __name__ == "__main__":
    unittest.main()
