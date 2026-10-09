"""夜の動画の記録（affirmative_bot.night_videos）。

夜の動画は 18:00 に Bluesky へ投稿し、その夜のおやすみポスト（bsky-affirmative-bot の
biorhythm_server）が RP とコメントで紹介する。両者はこのテーブルでつながる。
1 bot日（JST 4時区切り）につき1行。テーブルの定義は bsky-affirmative-bot の
packages/database/src/schema.ts（night_videos）が原典。

YouTube 時代の youtube_shorts には書かない。あちらは気まぐれポストが「新しい Shorts」
として告知する材料なので、ここに書くと Bluesky の動画を YouTube として告知してしまう。
"""

import datetime
import json

from psycopg2.extras import RealDictCursor

from common.db import connect_raw

JST = datetime.timezone(datetime.timedelta(hours=9))
BOT_DAY_START_HOUR = 4

# 直近何日ぶんのテーマを避けるか（以前の fetch_nagi_corner_context と同じ）
EXCLUDE_THEME_DAYS = 3


def bot_day(now: datetime.datetime | None = None) -> str:
    """botたんの日付。JST 0:00〜3:59 は前日（shared-configs の botDayRange と同じ）。"""
    now = now or datetime.datetime.now(JST)
    return (now.astimezone(JST) - datetime.timedelta(hours=BOT_DAY_START_HOUR)).date().isoformat()


def save(job: dict, post_uri: str, post_cid: str) -> None:
    """その日の動画ポストを記録する。同じ日に撮り直したら上書きする（紹介されるのは最新の1本）。"""
    source = job.get("source") or {}
    conn = connect_raw()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO affirmative_bot.night_videos
                    (video_date, post_uri, post_cid, hook, caption,
                     source_network, source_uri, source_display_name,
                     themes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'new')
                ON CONFLICT (video_date) DO UPDATE SET
                    post_uri = EXCLUDED.post_uri,
                    post_cid = EXCLUDED.post_cid,
                    hook = EXCLUDED.hook,
                    caption = EXCLUDED.caption,
                    source_network = EXCLUDED.source_network,
                    source_uri = EXCLUDED.source_uri,
                    source_display_name = EXCLUDED.source_display_name,
                    themes = EXCLUDED.themes,
                    status = 'new',
                    introduced_uri = NULL,
                    updated_at = NOW()
                """,
                (job["date"], post_uri, post_cid, job.get("hook_ja"), job.get("caption_ja"),
                 source.get("network"), source.get("uri"), source.get("display_name"),
                 json.dumps(job.get("themes") or [], ensure_ascii=False)),
            )
        conn.commit()
        print(f"[DB] night_videos に記録しました: {job['date']} {post_uri}")
    finally:
        conn.close()


def fetch_recent_context() -> dict:
    """台本の選択制約。直近数日に扱ったテーマを返す（同じ話題が続かないように）。

    YouTube 時代はいいね数の多い回のテーマを「参考テーマ」として渡していたが、
    その根拠（YouTube の統計）が無くなったので渡さない。
    """
    conn = connect_raw()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT themes FROM affirmative_bot.night_videos
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                ORDER BY video_date DESC
                """,
                (EXCLUDE_THEME_DAYS,),
            )
            themes = []
            for r in cur.fetchall():
                t = r["themes"]
                if isinstance(t, str):
                    t = json.loads(t)
                if isinstance(t, list):
                    themes.extend(x for x in t if isinstance(x, str))
    finally:
        conn.close()

    result = {"excluded_nagi_themes": list(dict.fromkeys(themes))}
    print(f"[night_videos] 除外テーマ={result['excluded_nagi_themes']}")
    return result
