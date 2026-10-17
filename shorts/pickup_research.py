#!/usr/bin/env python3
"""
作品紹介の調べもの（Gemini の Google 検索グラウンディング）

台本（ローカル LLM・構造化出力）とは別の呼び出しにする。構造化出力を付けると検索が
黙って無効になる（common/grounding.py の冒頭を参照）。

**システムプロンプトは1行に保つこと。** 2026-10-17 の PoC で、素のテキストでも
数百字の指示を付けると flash-lite は9本中4本で検索せず、記憶からもっともらしい
推測を返した（放送日を取り違えるなど）。1行にして、検索が走らなかったら1回だけ
やり直す形で 8/9 になった。2回とも走らなければ、その作品は使わない（呼ぶ側で次の候補へ）。

検索が走った回は、数日前のリリースまで出典つきで取れる（学習データの限界ではない）。

リクエスト数: 1作品につき1〜2回。実測で入力約90トークン・出力約400〜500トークン、
検索クエリは1〜4件（Gemini 3 系は月5,000クエリまで無料）。
"""

import requests

from common import grounding

SYSTEM = "Google検索で調べて、確認できた事実だけを日本語の箇条書き8〜12行で答える。推測で埋めない。"

ASK = {
    "anime_new": ("今期放送中のTVアニメ「{title}」（{year}年{month}月放送開始、制作：{studio}）について、"
                  "あらすじ（序盤だけ）、原作の有無、主なキャスト、主題歌、"
                  "地上波の放送局・放送開始日・曜日、話題になっている点を調べて。"),
    "anime_old": ("アニメ「{title}」（{year}年{month}月{day}日放送・公開開始、制作：{studio}）について、"
                  "あらすじ（序盤だけ）、原作の有無、主なスタッフ・キャスト、当時の反響や受賞を調べて。"),
    "music_new": ("{artist}の楽曲「{title}」（{year}年{month}月{day}日配信）について、"
                  "タイアップ（ドラマ・アニメ・CMなど）、作詞作曲、曲の特徴やテーマ、"
                  "話題になっている点を調べて。"),
    "movie_new": ("{year}年{month}月{day}日に日本で公開の映画「{title}」（原題：{original_title}）について、"
                  "あらすじ（序盤だけ）、監督、主なキャスト、原作の有無、主題歌、話題になっている点を調べて。"),
    "movie_old": ("{year}年{month}月{day}日に日本で公開された映画「{title}」（原題：{original_title}）について、"
                  "あらすじ（序盤だけ）、監督、主なキャスト、当時の興行や受賞を調べて。"),
    "drama_new": ("{year}年{month}月放送開始の日本のテレビドラマ「{title}」について、"
                  "放送局・放送曜日と時間、あらすじ（序盤だけ）、主なキャスト、脚本、原作の有無、主題歌、"
                  "話題になっている点を調べて。"),
    "game_new":  ("{year}年{month}月{day}日発売のゲーム「{original_title}」（開発：{developer}）について、"
                  "日本での発売日、どんなゲームか（序盤のあらすじ・遊び方）、シリーズの有無、"
                  "話題になっている点を調べて。"),
}

TIMEOUT_SEC = 60
TRIES = 2


def build_question(item: dict) -> str:
    y, m, d = item["date"].split("-")
    return ASK[item["theme"]].format(
        title=item["title"], year=y, month=int(m), day=int(d),
        studio=item.get("studio", ""), artist=item.get("artist", ""),
        original_title=item.get("original_title", ""), developer=item.get("developer", ""))


def _ask(model: str, question: str, system: str = SYSTEM) -> dict:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.0,
            # 1024 では思考ぶんに食われて、箇条書きが途中で切れた
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingLevel": grounding.LIVE_GROUNDING_THINKING},
        },
    }
    r = requests.post(grounding._ENDPOINT.format(model),
                      headers={"x-goog-api-key": grounding._api_key()},
                      json=body, timeout=TIMEOUT_SEC)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def research(item: dict) -> dict | None:
    """作品の事実を検索で調べる。検索が走らなかったら None（呼ぶ側で次の候補へ）。

    返り値は {"text", "queries", "model"}。API の失敗は例外のまま上げる。
    """
    return search(build_question(item), item["title"])


def search(question: str, label: str, system: str = SYSTEM) -> dict | None:
    """question を検索つきで聞く。検索が走らなかったら1回だけやり直し、それでも走らなければ None。

    夜版の豆知識（shorts/night_research.py）もこれを使う。system は1行に保つこと（冒頭を参照）。
    """
    if not grounding._api_key():
        raise RuntimeError("GEMINI_API_KEY がありません")
    model = grounding.LIVE_GROUNDING_MODELS[0]
    for attempt in range(1, TRIES + 1):
        text, queries = grounding._read(_ask(model, question, system))
        print(f"[調べもの] {label}: 検索{len(queries)}件（{attempt}回目）")
        # 検索せずに返した本文はモデルの記憶からの推測なので、使わない
        if queries and text and not text.strip().upper().startswith("UNKNOWN"):
            return {"text": text, "queries": queries, "model": model}
    print(f"[調べもの] {label}: 検索が走らなかったので使いません")
    return None
