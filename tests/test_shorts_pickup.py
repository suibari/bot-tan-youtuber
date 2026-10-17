"""朝の作品紹介（shorts/pickup_*.py）。

外の API は叩かない。2026-10-17 の PoC で見つかった問題（「10年前の今日」、
賞の名前の省略、機種の取り違え、字数の超過、成人向けの作品）を、コードの側で
止められることを固定する。台本の質そのものは ollama と Gemini が要るので手動で見る。
"""

import csv
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pickup_prompts
import pickup_sources
from shorts import turn


def _sent(text, reaction="none"):
    return {"text": text, "valence": 0.5, "arousal": 0.5,
            "motion": "A person smiles in a feminine way.", "reaction": reaction, "peak": ""}


def _item(**kw):
    item = {"theme": "movie_new", "key": "tmdb:movie:1", "title": "テスト映画",
            "date": "2026-10-09", "credit": pickup_sources.IMAGE_CREDITS["tmdb"],
            "source_url": "https://www.themoviedb.org/movie/1"}
    item.update(kw)
    return item


class ThemeTest(unittest.TestCase):
    def test_every_weekday_has_a_theme(self):
        monday = datetime.date(2026, 10, 12)
        with mock.patch.dict(os.environ, {"PICKUP_THEME": ""}):
            themes = [pickup_sources.theme_of(monday + datetime.timedelta(days=i)) for i in range(7)]
        self.assertEqual(themes, ["anime_new", "anime_old", "music_new", "game_new",
                                  "movie_new", "movie_old", "drama_new"])
        self.assertEqual(set(themes), set(pickup_sources.THEME_LABELS))

    def test_forced_theme(self):
        with mock.patch.dict(os.environ, {"PICKUP_THEME": "music_new"}):
            self.assertEqual(pickup_sources.theme_of(datetime.date(2026, 10, 12)), "music_new")
        with mock.patch.dict(os.environ, {"PICKUP_THEME": "quiz"}):
            with self.assertRaises(ValueError):
                pickup_sources.theme_of(datetime.date(2026, 10, 12))

    def test_pickup_runs_every_day(self):
        # 作品紹介は隔日の設定に関わらず毎日
        with mock.patch.dict(os.environ, {"SHORTS_ALTERNATE_DAYS": "true"}):
            days = [datetime.date(2026, 10, 1) + datetime.timedelta(days=i) for i in range(4)]
            self.assertTrue(all(turn.is_my_turn("pickup", d) for d in days))

    def test_jp_date(self):
        self.assertEqual(pickup_sources.jp_date("2026-10-09"), "2026年10月9日")

    def test_old_window_on_leap_day(self):
        lo, hi = pickup_sources._old_window(datetime.date(2028, 2, 29), 5)
        self.assertEqual((lo, hi), (datetime.date(2023, 2, 25), datetime.date(2023, 3, 3)))


class LedgerTest(unittest.TestCase):
    def test_mark_and_skip_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "used.csv"
            pickup_sources.mark_used(_item(key="tmdb:movie:1"), "https://youtube.com/watch?v=x", path)
            used = pickup_sources.load_used(path)
            self.assertEqual(used, {"tmdb:movie:1"})
            with path.open(encoding="utf-8") as f:
                self.assertEqual(next(csv.DictReader(f))["url"], "https://youtube.com/watch?v=x")

            batch = [_item(key="tmdb:movie:1"), _item(key="tmdb:movie:2", title="次の映画")]
            with mock.patch.dict(pickup_sources.FETCHERS, {"movie_new": lambda day: batch}), \
                    mock.patch.object(pickup_sources.time, "sleep"):
                got = list(pickup_sources.candidates("movie_new", datetime.date(2026, 10, 16), used))
            self.assertEqual([i["key"] for i in got], ["tmdb:movie:2"])

    def test_old_theme_tries_other_years_lazily(self):
        calls = []

        def fetch(day, years_ago):
            calls.append(years_ago)
            return [] if len(calls) == 1 else [_item(theme="movie_old", key=f"k{years_ago}",
                                                     years_ago=years_ago)]

        with mock.patch.dict(pickup_sources.OLD_FETCHERS, {"movie_old": fetch}), \
                mock.patch.object(pickup_sources.time, "sleep"):
            gen = pickup_sources.candidates("movie_old", datetime.date(2026, 10, 17), set())
            first = next(gen)
        # 1年目に候補が無ければ2年目を引く。取り出したぶんしか API を叩かない
        self.assertEqual(len(calls), 2)
        self.assertEqual(first["years_ago"], calls[1])

    def test_drama_needs_tv_network(self):
        with mock.patch.object(pickup_sources, "_tmdb", return_value={"networks": [{"name": "Netflix"}]}):
            self.assertFalse(pickup_sources.accept_drama(_item(tmdb_id=1)))
        with mock.patch.object(pickup_sources, "_tmdb", return_value={"networks": [{"name": "Nippon TV"}]}):
            self.assertTrue(pickup_sources.accept_drama(_item(tmdb_id=2)))


class SourceFilterTest(unittest.TestCase):
    def _al(self, **kw):
        m = {"id": 1, "title": {"native": "アニメ", "romaji": "anime"},
             "startDate": {"year": 2026, "month": 10, "day": 2}, "genres": ["Drama"],
             "format": "TV", "studios": {"nodes": [{"name": "OLM"}]},
             "coverImage": {"extraLarge": "https://x/y.jpg"}, "siteUrl": "https://anilist.co/anime/1",
             "countryOfOrigin": "JP", "isAdult": False}
        m.update(kw)
        return m

    def test_anilist_drops_ecchi_and_adult(self):
        self.assertIsNotNone(pickup_sources._al_item(self._al(), "anime_new"))
        self.assertIsNone(pickup_sources._al_item(self._al(genres=["Comedy", "Ecchi"]), "anime_new"))
        self.assertIsNone(pickup_sources._al_item(self._al(isAdult=True), "anime_new"))
        self.assertIsNone(pickup_sources._al_item(self._al(countryOfOrigin="CN"), "anime_new"))

    def test_igdb_drops_mature_ratings_and_prefers_switch(self):
        def game(gid, name, ratings=(), platform="PC (Microsoft Windows)", hypes=10):
            return {"id": gid, "name": name, "first_release_date": 1792000000, "hypes": hypes,
                    "themes": [1], "cover": {"image_id": "abc"},
                    "age_ratings": [{"organization": {"name": o}, "rating_category": {"rating": r}}
                                    for o, r in ratings],
                    "release_dates": [{"date": 1792000000, "release_region": 5,
                                       "platform": {"name": platform}}]}
        games = [game(1, "Gore Game", [("PEGI", "18"), ("ESRB", "M")], hypes=99),
                 game(2, "PC Game", hypes=50),
                 game(3, "Switch Game", platform="Nintendo Switch 2", hypes=5)]
        with mock.patch.object(pickup_sources, "_igdb", return_value=games):
            got = pickup_sources.fetch_game_new(datetime.date(2026, 10, 15))
        self.assertEqual([g["original_title"] for g in got], ["Switch Game", "PC Game"])
        # 日本語タイトルが無ければ英語の題名のまま（調べもののカタカナは使わない）
        self.assertEqual(got[0]["title"], "Switch Game")

    def test_igdb_uses_release_in_window(self):
        # 早期アクセスの開始日（1年前）ではなく、今週の正式リリースの日を使う
        g = {"id": 1, "name": "EA Game", "first_release_date": 1791900000, "hypes": 1, "themes": [],
             "cover": {"image_id": "a"},
             "release_dates": [{"date": 1758153600, "release_region": 8, "platform": {"name": "PC"}},
                               {"date": 1791900000, "release_region": 8, "platform": {"name": "PC"}}]}
        with mock.patch.object(pickup_sources, "_igdb", return_value=[g]):
            got = pickup_sources.fetch_game_new(datetime.date(2026, 10, 15))
        self.assertEqual(got[0]["date"], "2026-10-13")


class ScriptCheckTest(unittest.TestCase):
    def test_shortened_award_name_is_caught(self):
        memo = "2022年3月度のギャラクシー賞月間賞を受賞した。第94回アカデミー賞で6部門を受賞。"
        self.assertTrue(pickup_prompts.sentence_problems("ギャラクシー賞を受賞したんだって！", memo))
        # 前を省くのはよい（第94回 → なし）。「受賞」だけの語は賞の名前ではない
        self.assertEqual(pickup_prompts.sentence_problems("アカデミー賞で6部門を受賞したよ！", memo), [])
        self.assertEqual(pickup_prompts.sentence_problems("ギャラクシー賞月間賞を受賞！", memo), [])

    def test_day_words_and_platforms(self):
        self.assertTrue(pickup_prompts.sentence_problems("10年前の今日、始まったよ！", ""))
        self.assertTrue(pickup_prompts.sentence_problems("Switchでも遊べるよ！", ""))
        self.assertTrue(pickup_prompts.sentence_problems("Netflixで配信中だよ！", ""))
        self.assertEqual(pickup_prompts.sentence_problems("10月9日に公開されたよ！", ""), [])

    def test_person_word_must_be_in_memo(self):
        memo = "韓国で大成功した空間プランナーのイ・ユビン（チ・チャンウク）と、農業女子・白浜夏凛"
        self.assertTrue(pickup_prompts.sentence_problems("すべてを失った韓国の女性が島へ。", memo))
        self.assertEqual(pickup_prompts.sentence_problems("島で農業女子と出会うよ。", memo), [])

    def test_bad_sentence_dropped_and_parts_refilled(self):
        script = {"hook": [_sent("今日はこれ！")], "what": [_sent("どんな話か。"), _sent("Steamで出たよ。")],
                  "push": [_sent("推せる！", "joyful")], "info": [_sent("Switchで遊べる！")]}
        dropped = pickup_prompts.drop_bad_sentences(script, "")
        self.assertEqual(len(dropped), 3)
        filled = pickup_prompts.fill_missing(script, _item(theme="game_new", title="ゲーム", date="2026-10-15"))
        self.assertEqual(filled, ["hook", "info"])
        self.assertEqual(script["info"][0]["text"], "2026年10月15日発売だよ。ぜひ遊んでみてね！")
        self.assertEqual([s["text"] for s in script["what"]], ["どんな話か。"])

    def test_fit_length_drops_plain_sentences_first(self):
        long = "あ" * 40
        script = {"hook": [_sent("あ" * 20)], "what": [_sent(long), _sent(long)],
                  "push": [_sent(long, "joyful"), _sent(long)], "info": [_sent("あ" * 20)]}
        dropped = pickup_prompts.fit_length(script, limit=150)
        self.assertEqual(len(dropped), 2)
        self.assertLessEqual(pickup_prompts.total_chars(script), 150)
        # 山場の文は残す
        self.assertEqual(script["push"][0]["reaction"], "joyful")

    def test_old_theme_hook_is_fixed(self):
        script = {"hook": [_sent("10年前の今日、始まったよ！")]}
        pickup_prompts.apply_fixed_hook(script, _item(theme="anime_old", title="舟を編む", years_ago=10))
        self.assertEqual(script["hook"][0]["text"], "10年前の今週、『舟を編む』の放送が始まったよ！")

    def test_peak_is_forced_on_push(self):
        script = {"hook": [_sent("掴み", "joyful")], "push": [_sent("a"), _sent("b")]}
        pickup_prompts.ensure_peak(script)
        self.assertEqual(script["push"][0]["reaction"], "joyful")
        self.assertEqual(script["hook"][0]["reaction"], "none")


class TitleTest(unittest.TestCase):
    def test_title_starts_with_work_and_fits(self):
        import description
        title = description.build_pickup_title(_item(title="作" * 120), "フック")
        self.assertTrue(title.startswith("【作"))
        self.assertTrue(title.endswith(" / botたんの新作映画紹介"))
        self.assertLessEqual(len(title), 100)

    def test_stats_tool_reads_theme(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("shorts_stats", root / "tools" / "shorts_stats.py")
        stats = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"common.youtube_auth": mock.MagicMock()}):
            spec.loader.exec_module(stats)
        import description
        title = description.build_pickup_title(_item(theme="drama_new", title="ドラマ"), "x")
        self.assertEqual(stats.kind_of(title), "新作ドラマ")

    def test_tmdb_notice_only_for_tmdb(self):
        import description
        self.assertIn(description.TMDB_NOTICE, description.build_pickup_description(_item()))
        anime = _item(theme="anime_new", credit=pickup_sources.IMAGE_CREDITS["anilist"])
        self.assertNotIn(description.TMDB_NOTICE, description.build_pickup_description(anime))


if __name__ == "__main__":
    unittest.main()
