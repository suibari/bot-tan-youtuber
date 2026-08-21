"""PostgreSQL（botたんの記憶DB）への接続。

接続情報は Shorts パイプラインとライブ配信で同じ。以前は shorts/core.py・
shorts/youtube.py・live/config.py の3箇所に同じ辞書が書かれていた。
"""

import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor  # noqa: F401  (呼び出し側が再輸出して使う)

from common.env import env_int

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "192.168.1.200"),
    "port":     env_int("DB_PORT", 5432),
    "dbname":   os.getenv("DB_NAME", ""),
    "user":     os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
}

# ライブ配信のログを置くスキーマ。
# bsky-affirmative-bot の scripts/deploy.sh が drizzle-kit push を自動実行しており、
# schemaFilter が ['public','affirmative_bot'] なので、drizzle の定義に無いテーブルを
# affirmative_bot に置くと DROP 候補になってしまう。別スキーマなら触られない。
LIVE_SCHEMA = "bottan_live"

# 接続待ちの上限[秒]。DB は LAN 内なので、返ってこないなら待つ意味がない
CONNECT_TIMEOUT = 10


@contextmanager
def connect():
    """接続を貸して、抜けるときに必ず閉じる。"""
    conn = psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)
    try:
        yield conn
    finally:
        conn.close()


def connect_raw():
    """with を使わずに接続だけ取る。呼び出し側が close する責任を持つ。"""
    return psycopg2.connect(**DB_CONFIG, connect_timeout=CONNECT_TIMEOUT)
