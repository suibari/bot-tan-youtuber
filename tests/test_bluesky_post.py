"""夜の動画ポストの本文（shorts/bluesky.py）と、英語字幕の焼き込み（core）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "shorts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import bluesky  # noqa: E402
import core  # noqa: E402

NAGI_URI = "at://did:plc:abc/com.suibari.nagi.post/3mx"
BSKY_URI = "at://did:plc:abc/app.bsky.feed.post/3my"


class LinksTest(unittest.TestCase):
    def test_strip_links_removes_urls_and_mentions(self):
        self.assertEqual(bluesky.strip_links("見てね https://x.com/foo @someone.bsky.social さん"),
                         "見てね someone.bsky.social さん")

    def test_public_post_url(self):
        self.assertEqual(bluesky.public_post_url(BSKY_URI), "https://bsky.app/profile/did:plc:abc/post/3my")
        self.assertEqual(bluesky.public_post_url(NAGI_URI),
                         "https://nagi.suibari.com/thread/did%3Aplc%3Aabc/3mx")
        self.assertIsNone(bluesky.public_post_url("https://example.com"))


class PostTextTest(unittest.TestCase):
    def job(self, ja="すいばりさんの投稿を紹介したよ", en="I shared a post by Suibari"):
        return {"caption_ja": ja, "caption_en": en,
                "source": {"url": bluesky.public_post_url(NAGI_URI)}}

    def test_link_facet_points_at_url_bytes(self):
        [(text, facets)] = bluesky.build_post_texts(self.job())
        url = bluesky.public_post_url(NAGI_URI)
        self.assertTrue(text.endswith(url))
        raw = text.encode("utf-8")
        idx = facets[0]["index"]
        self.assertEqual(raw[idx["byteStart"]:idx["byteEnd"]].decode(), url)
        self.assertIn("I shared a post", text)

    def test_long_caption_moves_english_to_reply(self):
        texts = bluesky.build_post_texts(self.job(ja="あ" * 150, en="word " * 40))
        self.assertEqual(len(texts), 2)
        self.assertLessEqual(bluesky.grapheme_len(texts[0][0]), bluesky.POST_MAX_GRAPHEMES)
        self.assertNotIn("word", texts[0][0])
        self.assertTrue(texts[0][0].endswith(bluesky.public_post_url(NAGI_URI)))

    def test_llm_urls_never_reach_the_post(self):
        [(text, facets)] = bluesky.build_post_texts(self.job(ja="見てね https://x.com/fake"))
        self.assertNotIn("x.com", text)
        self.assertEqual(len(facets), 1)

    def test_no_source_means_no_link(self):
        [(text, facets)] = bluesky.build_post_texts({"caption_ja": "こんばんは", "source": None})
        self.assertEqual(facets, [])

    def test_grapheme_len_counts_joined_emoji_once(self):
        self.assertEqual(bluesky.grapheme_len("👩‍👩‍👧"), 1)
        self.assertEqual(bluesky.grapheme_len("あいう"), 3)


class EnglishSubtitleTest(unittest.TestCase):
    def test_long_english_is_paged_not_cut(self):
        text = " ".join(f"word{i}" for i in range(60))
        pages = core.paginate_english_cues([{"start": 1.0, "end": 9.0, "text": text}])
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(lines) <= core.EN_SUB_MAX_LINES for _, lines in pages))
        self.assertEqual(" ".join(" ".join(lines) for _, lines in pages), text)
        self.assertEqual(pages[0][0]["start"], 1.0)
        self.assertEqual(pages[-1][0]["end"], 9.0)
        for (a, _), (b, _) in zip(pages, pages[1:]):
            self.assertEqual(a["end"], b["start"])

    def test_wrap_english_limits_lines(self):
        lines = core.wrap_english_lines("word " * 80)
        self.assertEqual(len(lines), core.EN_SUB_MAX_LINES)
        self.assertTrue(lines[-1].endswith("…"))

    def test_english_overlays_stay_below_face_and_inside_video(self):
        import tempfile
        import rich_text
        with tempfile.TemporaryDirectory() as d:
            ovs = rich_text.english_overlays(
                [{"start": 0.0, "end": 4.0, "text": "Give yourself a big hug " * 4},
                 {"start": 4.0, "end": 9.0, "text": "Late page"}], d, end=5.0)
            for ov in ovs:
                self.assertGreater(int(ov["y"]), 1400)   # 顔（y≒1000）より下
                self.assertLessEqual(ov["end"], 5.0)


if __name__ == "__main__":
    unittest.main()
