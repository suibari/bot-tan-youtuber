#!/usr/bin/env python3
"""
朝の作品紹介 Shorts の題材選び（曜日ごとのテーマ・候補の取得・紹介済み台帳）

2026-10-17 にクイズから切り替えた。クイズは50問を一巡して再出題に入り、再生数が
戻らなかった。作品紹介は題材が尽きないうえ、作品名で検索から来てもらえる。

曜日とテーマは WEEKDAY_THEMES の1か所で決める。国内の発売・公開の曜日に合わせ、
その週に検索されやすいものをその日に出す。

候補は公式のデータベース・RSS から取る（作品名・日付・画像は台本ではなくここの値を
使う）。足りない事実は pickup_research の検索で補う。

外部への呼び出し（1本あたり）:
  AniList / iTunes / TMDB / IGDB … 1〜3回（ドラマは放送局の確認で候補ぶん増えうる）
  アートワークの画像            … 1回
どれも無料の API。Gemini の回数は pickup_research を参照。
"""

import csv
import datetime
import os
import random
import time
from pathlib import Path

import requests

JST = datetime.timezone(datetime.timedelta(hours=9))
UA = {"User-Agent": "bottan-youtuber/pickup"}

_ROOT = Path(__file__).resolve().parent.parent
# .env に空で置かれても既定に落とす
USED_CSV = Path(os.getenv("PICKUP_USED_CSV") or str(_ROOT / "data" / "pickup_used.csv"))
USED_FIELDS = ["theme", "key", "title", "used_at", "url"]

# 曜日（月=0）→ テーマ。PoC（2026-10-17）で7テーマとも台本まで通した
WEEKDAY_THEMES = {
    0: "anime_new",    # 週末に放送された今期のアニメが週明けに検索される
    1: "anime_old",    # 「〇年前の今週に放送開始」の名作
    2: "music_new",    # 国内の CD・配信の発売は水曜が多い
    3: "game_new",     # 国内のパッケージ・Switch の発売は木曜が多い
    4: "movie_new",    # 国内の劇場公開は金曜
    5: "movie_old",    # 週末に家で観る1本。「〇年前の今週に公開」
    6: "drama_new",    # 日曜の夜は大きいドラマ枠がある。朝に紹介すれば夜の放送に間に合う
}

# テーマの見出し（パネルのラベル・タイトル末尾・集計で使う）
THEME_LABELS = {
    "anime_new": "新作アニメ",
    "anime_old": "名作アニメ",
    "music_new": "新曲",
    "game_new":  "新作ゲーム",
    "movie_new": "新作映画",
    "movie_old": "名作映画",
    "drama_new": "新作ドラマ",
}
OLD_THEMES = {"anime_old", "movie_old"}

# 画像の出典表記（画面と概要欄）。権利は各権利者にある
IMAGE_CREDITS = {
    "anilist": "画像：AniList ©各権利者",
    "tmdb":    "画像：TMDB ©各権利者",
    "itunes":  "画像：Apple Music ©各権利者",
    "igdb":    "画像：IGDB ©各権利者",
}

# 旧作は「〇年前の今週」にする。候補が無い年は別の年で選び直す
# （PoC では10年前の今週に条件に合う映画が無かった）
OLD_YEARS = list(range(5, 31))
OLD_WINDOW_DAYS = 3


def today_jst() -> datetime.date:
    """SHORTS_TURN_DATE=YYYY-MM-DD で差し替えられる（shorts/turn.py と同じ）。"""
    override = os.getenv("SHORTS_TURN_DATE", "").strip()
    if override:
        return datetime.date.fromisoformat(override)
    return datetime.datetime.now(JST).date()


def theme_of(day: datetime.date) -> str:
    """その日のテーマ。PICKUP_THEME で固定できる（手動確認用）。"""
    forced = os.getenv("PICKUP_THEME", "").strip()
    if forced:
        if forced not in THEME_LABELS:
            raise ValueError(f"PICKUP_THEME が不正です: {forced}（{', '.join(THEME_LABELS)}）")
        return forced
    return WEEKDAY_THEMES[day.weekday()]


def jp_date(iso: str) -> str:
    """'2026-10-09' → '2026年10月9日'。台詞に ISO 形式を書かれると読み上げが崩れた。"""
    d = datetime.date.fromisoformat(iso)
    return f"{d.year}年{d.month}月{d.day}日"


# ──────────────────────────────────────────────
# 紹介済み台帳
# ──────────────────────────────────────────────

def load_used(path: Path = None) -> set[str]:
    path = path or USED_CSV
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as f:
        return {row["key"] for row in csv.DictReader(f) if row.get("key")}


def mark_used(item: dict, video_url: str = "", path: Path = None, now=None) -> None:
    """紹介した作品を残す。権利者から申し立てがあったときに、動画を特定できるよう URL も残す。"""
    path = path or USED_CSV
    now = now or datetime.datetime.now(JST)
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=USED_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"theme": item["theme"], "key": item["key"], "title": item["title"],
                    "used_at": now.isoformat(), "url": video_url})


# ──────────────────────────────────────────────
# AniList（アニメ）
# ──────────────────────────────────────────────

ANILIST = "https://graphql.anilist.co"
_AL_FIELDS = ("id title{native romaji} startDate{year month day} genres format "
              "studios(isMain:true){nodes{name}} coverImage{extraLarge} "
              "siteUrl countryOfOrigin isAdult")
# 朝の番組なので、成人向けとお色気を外す
AL_NG_GENRES = {"Ecchi", "Hentai"}


def _anilist(args: str) -> list[dict]:
    query = f"query{{Page(perPage:20){{media({args}){{{_AL_FIELDS}}}}}}}"
    r = requests.post(ANILIST, json={"query": query}, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()["data"]["Page"]["media"]


def _al_item(m: dict, theme: str) -> dict | None:
    sd = m["startDate"]
    if (m["isAdult"] or m["countryOfOrigin"] != "JP" or AL_NG_GENRES & set(m["genres"])
            or not (m["coverImage"] or {}).get("extraLarge") or not m["title"]["native"]
            or not (sd["year"] and sd["month"] and sd["day"])):
        return None
    return {
        "theme": theme,
        "key": f"anilist:{m['id']}",
        "title": m["title"]["native"],
        "date": f"{sd['year']:04d}-{sd['month']:02d}-{sd['day']:02d}",
        "genres": m["genres"],
        "studio": ", ".join(n["name"] for n in m["studios"]["nodes"]),
        "artwork": m["coverImage"]["extraLarge"],
        "credit": IMAGE_CREDITS["anilist"],
        "source_url": m["siteUrl"],
    }


def _season(day: datetime.date) -> str:
    return {1: "WINTER", 4: "SPRING", 7: "SUMMER", 10: "FALL"}[(day.month - 1) // 3 * 3 + 1]


def fetch_anime_new(day: datetime.date) -> list[dict]:
    media = _anilist(f"season:{_season(day)},seasonYear:{day.year},type:ANIME,isAdult:false,"
                     f"sort:POPULARITY_DESC,format_in:[TV,ONA]")
    items = [i for m in media if (i := _al_item(m, "anime_new"))]
    # 放送が始まっているものだけ（未放送の作品は「今週観て」と言えない）
    return [i for i in items if i["date"] <= day.isoformat()]


def _old_window(day: datetime.date, years_ago: int) -> tuple[datetime.date, datetime.date]:
    # 2/29 はその年に無いことがあるので 2/28 に寄せる
    center = datetime.date(day.year - years_ago, day.month,
                           28 if (day.month, day.day) == (2, 29) else day.day)
    span = datetime.timedelta(days=OLD_WINDOW_DAYS)
    return center - span, center + span


def fetch_anime_old(day: datetime.date, years_ago: int) -> list[dict]:
    lo, hi = _old_window(day, years_ago)
    media = _anilist(f"type:ANIME,isAdult:false,sort:POPULARITY_DESC,format_in:[TV,MOVIE],"
                     f"startDate_greater:{int(lo.strftime('%Y%m%d')) - 1},"
                     f"startDate_lesser:{int(hi.strftime('%Y%m%d')) + 1}")
    out = []
    for m in media:
        if (item := _al_item(m, "anime_old")):
            item["years_ago"] = years_ago
            out.append(item)
    return out


# ──────────────────────────────────────────────
# iTunes（新曲）
# ──────────────────────────────────────────────

ITUNES_TOP = "https://rss.marketingtools.apple.com/api/v2/jp/music/most-played/100/songs.json"
# 「新着」のフィードは無い（404）。トップ100から最近出た曲だけを拾う
MUSIC_NEW_DAYS = 14


def fetch_music_new(day: datetime.date) -> list[dict]:
    r = requests.get(ITUNES_TOP, headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for rank, s in enumerate(r.json()["feed"]["results"], 1):
        released = datetime.date.fromisoformat(s["releaseDate"])
        # Apple の綴りは "Explict"。どちらも外す
        if (day - released).days > MUSIC_NEW_DAYS or "xpl" in (s.get("contentAdvisoryRating") or ""):
            continue
        out.append({
            "theme": "music_new",
            "key": f"itunes:{s['id']}",
            "title": s["name"],
            "artist": s["artistName"],
            "date": s["releaseDate"],
            "genres": [g["name"] for g in s.get("genres", []) if g["name"] != "ミュージック"],
            "chart_rank": rank,
            "artwork": s["artworkUrl100"].replace("100x100bb", "1000x1000bb"),
            "credit": IMAGE_CREDITS["itunes"],
            "source_url": s["url"],
        })
    return out


# ──────────────────────────────────────────────
# TMDB（映画・ドラマ）
# ──────────────────────────────────────────────

TMDB = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w780"
# ドラマはテレビ放送の作品だけにする（2026-10-17 決定）。TMDB の networks の英語名の部分一致。
# Netflix などの配信だけの作品は外す
TV_NETWORKS = ("NHK", "Nippon TV", "NTV", "TBS", "Fuji TV", "TV Asahi", "TV Tokyo",
               "BS Asahi", "BS Fuji", "BS-TBS", "BS TV Tokyo", "BS Nippon", "WOWOW",
               "Tokyo MX", "TOKYO MX", "MBS", "Mainichi Broadcasting", "ABC TV", "Asahi Broadcasting",
               "Kansai TV", "Yomiuri TV", "Tokai TV", "CBC", "TV Kanagawa", "TVQ")


def _tmdb(path: str, **params) -> dict:
    params = {"api_key": os.environ["TMDB_API_KEY"], "language": "ja-JP", **params}
    r = requests.get(f"{TMDB}{path}", params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _tmdb_item(m: dict, theme: str, genres: dict) -> dict | None:
    title = m.get("title") or m.get("name") or ""
    # 日本語のタイトル・あらすじ・ポスターがそろっているものだけ
    if (m.get("adult") or not m.get("poster_path") or not m.get("overview")
            or not any("぀" <= ch <= "鿿" for ch in title)):
        return None
    kind = "movie" if "title" in m else "tv"
    return {
        "theme": theme,
        "key": f"tmdb:{kind}:{m['id']}",
        "title": title,
        "original_title": m.get("original_title") or m.get("original_name"),
        "date": m.get("release_date") or m.get("first_air_date"),
        "genres": [genres.get(g, "") for g in m.get("genre_ids", [])],
        "overview": m["overview"],
        "artwork": TMDB_IMG + m["poster_path"],
        "credit": IMAGE_CREDITS["tmdb"],
        "source_url": f"https://www.themoviedb.org/{kind}/{m['id']}",
        "tmdb_id": m["id"],
    }


def _genres(kind: str) -> dict:
    return {g["id"]: g["name"] for g in _tmdb(f"/genre/{kind}/list")["genres"]}


def _discover_movies(lo: datetime.date, hi: datetime.date, **extra) -> list[dict]:
    res = _tmdb("/discover/movie", region="JP", sort_by="popularity.desc",
                include_adult="false", with_release_type="2|3",
                **{"release_date.gte": lo.isoformat(), "release_date.lte": hi.isoformat()},
                **extra)["results"]
    return res


def fetch_movie_new(day: datetime.date) -> list[dict]:
    genres = _genres("movie")
    # 先週・今週に国内公開されたもの（金曜公開を拾う）
    res = _discover_movies(day - datetime.timedelta(days=10), day + datetime.timedelta(days=7))
    return [i for m in res if (i := _tmdb_item(m, "movie_new", genres))]


def fetch_movie_old(day: datetime.date, years_ago: int) -> list[dict]:
    genres = _genres("movie")
    lo, hi = _old_window(day, years_ago)
    # 評価の件数で、知られていない作品を外す
    res = _discover_movies(lo, hi, **{"vote_count.gte": 200})
    out = []
    for m in res:
        if (item := _tmdb_item(m, "movie_old", genres)):
            item["years_ago"] = years_ago
            out.append(item)
    return out


def fetch_drama_new(day: datetime.date) -> list[dict]:
    genres = _genres("tv")
    season_start = day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    res = _tmdb("/discover/tv", with_origin_country="JP", sort_by="popularity.desc",
                without_genres="16",     # アニメを除く
                **{"first_air_date.gte": (season_start - datetime.timedelta(days=14)).isoformat(),
                   "first_air_date.lte": day.isoformat()})["results"]
    return [i for m in res if (i := _tmdb_item(m, "drama_new", genres))]


def accept_drama(item: dict) -> bool:
    """テレビ放送の作品か。一覧には放送局が無いので、詳細を1回引く（候補ぶんだけ増える）。"""
    networks = [n["name"] for n in _tmdb(f"/tv/{item['tmdb_id']}").get("networks", [])]
    item["networks"] = networks
    return any(any(tv in n for tv in TV_NETWORKS) for n in networks)


# ──────────────────────────────────────────────
# IGDB（ゲーム）
# ──────────────────────────────────────────────

IGDB = "https://api.igdb.com/v4"
IGDB_REGIONS = (5, 8)            # japan, worldwide（release_date_regions）
IGDB_NG_THEMES = {42}            # Erotic
# 朝の番組なので、成人向けの区分の作品は選ばない。
# PoC で「無検閲のゴア表現」を推しポイントにした台本が出た（ESRB M / PEGI 18 の作品）
IGDB_NG_RATINGS = {("CERO", "Z"), ("PEGI", "18"), ("ESRB", "AO"), ("ACB", "R 18+")}
_igdb_token: str | None = None


def _igdb(endpoint: str, body: str) -> list:
    global _igdb_token
    if _igdb_token is None:
        # 有効期限は約2か月あるが、1回の実行で1回しか取らないので保存しない
        t = requests.post("https://id.twitch.tv/oauth2/token", timeout=20, params={
            "client_id": os.environ["IGDB_CLIENT_ID"],
            "client_secret": os.environ["IGDB_CLIENT_SECRET"],
            "grant_type": "client_credentials"})
        t.raise_for_status()
        _igdb_token = t.json()["access_token"]
    r = requests.post(f"{IGDB}/{endpoint}", data=body, timeout=30, headers={
        "Client-ID": os.environ["IGDB_CLIENT_ID"], "Authorization": f"Bearer {_igdb_token}"})
    r.raise_for_status()
    return r.json()


def _igdb_score(g: dict) -> tuple:
    """国内で人気の作品を先にする。IGDB の hypes は海外の基準で、PoC では上位3本が
    どれも海外のスタジオの作品だった。Switch 系の版がある作品と日本語タイトルがある作品を上げる。"""
    platforms = {(rd.get("platform") or {}).get("name") or "" for rd in g.get("release_dates") or []}
    switch = any("Switch" in p for p in platforms)
    jp_title = any("Japan" in str((l.get("region") or {}).get("name"))
                   for l in g.get("game_localizations") or [])
    return (switch + jp_title, g.get("hypes") or 0)


def fetch_game_new(day: datetime.date) -> list[dict]:
    noon = datetime.datetime.combine(day, datetime.time(12), JST).timestamp()
    lo, hi = int(noon - 10 * 86400), int(noon + 4 * 86400)
    # 移植（昔の作品の新機種版）が人気順の上位を占めるので、初めて出た日で絞る
    games = _igdb("games", (
        "fields name,first_release_date,hypes,themes,genres.name,cover.image_id,"
        "involved_companies.company.name,involved_companies.developer,"
        "game_localizations.name,game_localizations.region.name,"
        "age_ratings.organization.name,age_ratings.rating_category.rating,"
        "release_dates.date,release_dates.release_region,release_dates.platform.name; "
        f"where first_release_date >= {lo} & first_release_date <= {hi} "
        f"& release_dates.release_region = ({','.join(map(str, IGDB_REGIONS))}) "
        "& cover != null & game_type = 0; sort hypes desc; limit 30;"))
    out = []
    for g in sorted(games, key=_igdb_score, reverse=True):
        ratings = {((a.get("organization") or {}).get("name"), (a.get("rating_category") or {}).get("rating"))
                   for a in g.get("age_ratings") or []}
        if IGDB_NG_THEMES & set(g.get("themes") or []) or IGDB_NG_RATINGS & ratings:
            continue
        # 日本語タイトルは IGDB にあるときだけ使う。調べもののカタカナ表記は、
        # PoC で国内の記事に見当たらないもの（作られた表記）が出たので使わない
        jp = [l["name"] for l in g.get("game_localizations") or []
              if l.get("name") and "Japan" in str((l.get("region") or {}).get("name"))]
        # 今週の範囲に入る国内（全世界）の発売日を使う。一番早い日を取ると、早期アクセスの
        # 開始日（Ratatan は1年前）が出た。範囲に無ければ first_release_date
        jp_dates = sorted(rd["date"] for rd in g.get("release_dates") or []
                          if rd.get("release_region") in IGDB_REGIONS and rd.get("date")
                          and lo <= rd["date"] <= hi)
        released = datetime.datetime.fromtimestamp(jp_dates[0] if jp_dates else g["first_release_date"], JST)
        out.append({
            "theme": "game_new",
            "key": f"igdb:{g['id']}",
            "title": jp[0] if jp else g["name"],
            "original_title": g["name"],
            "date": released.date().isoformat(),
            "genres": [x["name"] for x in g.get("genres") or []],
            "developer": ", ".join(c["company"]["name"] for c in g.get("involved_companies") or []
                                   if c.get("developer")),
            "artwork": f"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{g['cover']['image_id']}.jpg",
            "credit": IMAGE_CREDITS["igdb"],
            "source_url": f"https://www.igdb.com/games/{g['id']}",
        })
    return out


# ──────────────────────────────────────────────
# 候補の列
# ──────────────────────────────────────────────

FETCHERS = {
    "anime_new": fetch_anime_new,
    "music_new": fetch_music_new,
    "movie_new": fetch_movie_new,
    "drama_new": fetch_drama_new,
    "game_new":  fetch_game_new,
}
OLD_FETCHERS = {"anime_old": fetch_anime_old, "movie_old": fetch_movie_old}
ACCEPT = {"drama_new": accept_drama}
# 旧作で、年を変えて探し直す回数の上限（1回につき API 1〜2回）
OLD_YEAR_TRIES = 4


def candidates(theme: str, day: datetime.date, used: set[str] = None, rng=None):
    """紹介済みを除いた候補を、選ぶ順に1つずつ返す（ジェネレーター）。

    呼ぶ側は調べものが通らなかったら次を取る。必要なぶんしか API を叩かないよう、
    旧作の年の切り替えとドラマの放送局の確認は、取り出されたときに行う。
    """
    used = used if used is not None else load_used()
    accept = ACCEPT.get(theme, lambda item: True)
    if theme in OLD_FETCHERS:
        rng = rng or random.Random(day.isoformat())
        years = rng.sample(OLD_YEARS, min(OLD_YEAR_TRIES, len(OLD_YEARS)))
        batches = (lambda y=y: OLD_FETCHERS[theme](day, y) for y in years)
    else:
        batches = [lambda: FETCHERS[theme](day)]
    for fetch in batches:
        for item in fetch():
            if item["key"] in used:
                continue
            if accept(item):
                yield item
        time.sleep(0.5)


def download_artwork(item: dict, out_dir: Path) -> Path:
    """アートワークを一時ディレクトリへ落とす（リポジトリには保存しない）。"""
    path = Path(out_dir) / f"pickup_art_{item['key'].replace(':', '_')}.jpg"
    r = requests.get(item["artwork"], headers=UA, timeout=30)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path
