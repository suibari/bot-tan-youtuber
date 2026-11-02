"""Instagram リール投稿（shorts/instagram.py）と、朝版からのつなぎ込み。

外の API は叩かない。requests をモックして、呼び出しの順番・パラメータと、
失敗しても YouTube 側を止めないこと、投稿（media_publish）を再試行しないことを固定する。
"""

import os
import tempfile
import unittest
from unittest import mock

import instagram
import pickup_sources

ENV = {"INSTAGRAM_ENABLED": "true", "INSTAGRAM_ACCESS_TOKEN": "tok",
       "INSTAGRAM_USER_ID": "ig1", "DISCORD_WEBHOOK_URL": ""}


def _res(status=200, body=None):
    r = mock.Mock(status_code=status, ok=status < 400, text=str(body))
    r.json.return_value = body if body is not None else {}
    return r


def _item(**kw):
    item = {"theme": "movie_new", "key": "tmdb:movie:1", "title": "テスト映画",
            "date": "2026-10-09", "credit": pickup_sources.IMAGE_CREDITS["tmdb"],
            "source_url": "https://www.themoviedb.org/movie/1"}
    item.update(kw)
    return item


class UploadReelTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        self.sleep = mock.patch.object(instagram.time, "sleep")
        self.sleep.start()
        f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        f.write(b"x" * 10)
        f.close()
        self.mp4 = f.name

    def tearDown(self):
        self.env.stop()
        self.sleep.stop()
        os.unlink(self.mp4)

    def run_upload(self, posts, gets):
        with mock.patch.object(instagram.requests, "post", side_effect=posts) as post, \
             mock.patch.object(instagram.requests, "get", side_effect=gets) as get:
            url = instagram.upload_reel(self.mp4, "キャプション")
        return url, post, get

    def test_happy_path_order_and_params(self):
        url, post, get = self.run_upload(
            posts=[_res(body={"id": "c1"}), _res(body={"success": True}), _res(body={"id": "m1"})],
            gets=[_res(body={"status_code": "IN_PROGRESS"}), _res(body={"status_code": "FINISHED"}),
                  _res(body={"permalink": "https://www.instagram.com/reel/abc/"})])
        self.assertEqual(url, "https://www.instagram.com/reel/abc/")
        create, upload, publish = post.call_args_list
        self.assertTrue(create.args[0].startswith("https://graph.facebook.com/"))
        self.assertTrue(create.args[0].endswith("/ig1/media"))
        self.assertEqual(create.kwargs["data"]["media_type"], "REELS")
        self.assertEqual(create.kwargs["data"]["upload_type"], "resumable")
        self.assertEqual(create.kwargs["data"]["is_ai_generated"], "true")
        self.assertEqual(create.kwargs["data"]["caption"], "キャプション")
        self.assertTrue(upload.args[0].startswith("https://rupload.facebook.com/ig-api-upload/"))
        self.assertTrue(upload.args[0].endswith("/c1"))
        self.assertEqual(upload.kwargs["headers"]["Authorization"], "OAuth tok")
        self.assertEqual(upload.kwargs["headers"]["file_size"], "10")
        self.assertEqual(upload.kwargs["headers"]["offset"], "0")
        self.assertEqual(publish.kwargs["data"], {"creation_id": "c1"})
        # トークンは URL やパラメータに載せない
        for c in post.call_args_list + get.call_args_list:
            self.assertNotIn("tok", c.args[0])
            self.assertNotIn("access_token", str(c.kwargs.get("params", {})))

    def test_ai_label_can_be_turned_off(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_AI_GENERATED": "false"}):
            _, post, _ = self.run_upload(
                posts=[_res(body={"id": "c1"}), _res(body={"success": True}), _res(body={"id": "m1"})],
                gets=[_res(body={"status_code": "FINISHED"}), _res(body={"permalink": "p"})])
        self.assertNotIn("is_ai_generated", post.call_args_list[0].kwargs["data"])

    def test_processing_error_does_not_publish(self):
        url, post, _ = self.run_upload(
            posts=[_res(body={"id": "c1"}), _res(body={"success": True})],
            gets=[_res(body={"status_code": "ERROR", "status": "bad codec"})])
        self.assertIsNone(url)
        self.assertEqual(post.call_count, 2)

    def test_publish_failure_is_not_retried(self):
        url, post, _ = self.run_upload(
            posts=[_res(body={"id": "c1"}), _res(body={"success": True}),
                   _res(500, {"error": {"message": "boom", "code": 1}})],
            gets=[_res(body={"status_code": "FINISHED"})])
        self.assertIsNone(url)
        self.assertEqual(post.call_count, 3)

    def test_retriable_upload_is_retried_once_more(self):
        url, post, _ = self.run_upload(
            posts=[_res(body={"id": "c1"}),
                   _res(500, {"debug_info": {"retriable": True, "type": "ProcessingFailedError"}}),
                   _res(body={"success": True}), _res(body={"id": "m1"})],
            gets=[_res(body={"status_code": "FINISHED"}), _res(body={"permalink": "p"})])
        self.assertEqual(url, "p")
        self.assertEqual(post.call_count, 4)

    def test_non_retriable_upload_stops(self):
        url, post, _ = self.run_upload(
            posts=[_res(body={"id": "c1"}),
                   _res(400, {"debug_info": {"retriable": False, "type": "BadRequest"}})],
            gets=[])
        self.assertIsNone(url)
        self.assertEqual(post.call_count, 2)

    def test_disabled_does_nothing(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_ENABLED": "false"}):
            url, post, get = self.run_upload(posts=[], gets=[])
        self.assertIsNone(url)
        self.assertEqual(post.call_count + get.call_count, 0)


class CheckTokenTest(unittest.TestCase):
    def check(self, data):
        with mock.patch.dict(os.environ, ENV), \
             mock.patch.object(instagram.requests, "get", return_value=_res(body={"data": data})), \
             mock.patch.object(instagram._notify, "warn") as warn:
            instagram.check_token(now=1_000_000)
        return warn

    def test_warns_when_data_access_is_near(self):
        warn = self.check({"is_valid": True, "data_access_expires_at": 1_000_000 + 5 * 86400})
        self.assertIn("残り 5 日", warn.call_args.args[0])

    def test_quiet_when_far(self):
        warn = self.check({"is_valid": True, "data_access_expires_at": 1_000_000 + 60 * 86400})
        warn.assert_not_called()

    def test_warns_when_invalid(self):
        warn = self.check({"is_valid": False})
        warn.assert_called_once()


class CaptionTest(unittest.TestCase):
    def test_contents(self):
        import description
        cap = description.build_pickup_caption(_item(), "胸が熱くなる", bgm_generated=True)
        self.assertTrue(cap.startswith("【テスト映画】胸が熱くなる"))
        self.assertIn(description.TMDB_NOTICE, cap)
        self.assertIn("VOICEVOX", cap)
        self.assertIn("AI生成（ACE-Step", cap)
        self.assertNotIn("YouTube", cap)
        self.assertIn("#テスト映画", cap)
        self.assertIn("#新作映画", cap)

    def test_limits(self):
        import description
        cap = description.build_pickup_caption(_item(title="作" * 3000), "掴み")
        self.assertLessEqual(len(cap), description.IG_CAPTION_MAX)
        self.assertLessEqual(cap.count("#"), description.IG_HASHTAG_MAX)

    def test_hashtag_drops_symbols(self):
        import description
        self.assertEqual(description._hashtag("ONE PIECE: 第1話!"), "ONEPIECE第1話")
        self.assertEqual(description._hashtag("!!!"), "")


class PipelineHookTest(unittest.TestCase):
    """朝版の _upload から Instagram が呼ばれる条件。"""

    def run_pipeline(self, privacy="public", yt_url="https://youtube.com/watch?v=v"):
        import pickup_pipeline
        import youtube
        env = {**ENV, "YOUTUBE_PRIVACY": privacy, "PICKUP_NO_CONSUME": "true"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(youtube, "upload_to_youtube", return_value=yt_url), \
             mock.patch.object(youtube, "save_youtube_upload_to_db"), \
             mock.patch.object(youtube, "notify_discord"), \
             mock.patch.object(instagram, "check_token"), \
             mock.patch.object(instagram, "upload_reel", return_value="ig") as reel:
            pickup_pipeline._upload(_item(), {"title_hook": "掴み"}, "x.mp4", "")
        return reel

    def test_posts_after_public_youtube(self):
        reel = self.run_pipeline()
        reel.assert_called_once()
        self.assertEqual(reel.call_args.args[0], "x.mp4")
        self.assertTrue(reel.call_args.args[1].startswith("【テスト映画】掴み"))

    def test_skipped_for_private_youtube(self):
        self.run_pipeline(privacy="private").assert_not_called()

    def test_skipped_when_youtube_failed(self):
        self.run_pipeline(yt_url=None).assert_not_called()


if __name__ == "__main__":
    unittest.main()
