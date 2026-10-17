"""夜版の豆知識の調べもの（Gemini の Google 検索グラウンディング）。

型 fact の日だけ、企画で選んだ具体物（about）と切り口（angle）を調べ、
確認できた事実の箇条書きを台本の材料にする。ローカル LLM（Gemma 12B）に
豆知識を書かせると事実を作ることがあった（2026-10-18 の試し書きで
「ブックオフは1980年に誕生」。実際は1990年）。

検索の呼び出しは shorts/pickup_research.search を使う（構造化出力を付けない・
system を1行に保つ・検索が走らなければ1回だけやり直す）。

リクエスト数: fact の日に1〜2回。型は4つを日替わりで回すので、週に2回前後。
2回とも検索が走らなければ None を返し、呼ぶ側はその日の型を fact 以外に替える
（推測の豆知識は出さない。Gemini をそれ以上は呼ばない）。
"""

from pickup_research import search

SYSTEM = "Google検索で調べて、確認できた事実だけを日本語の箇条書き5〜8行で答える。推測で埋めない。"


def build_question(plan: dict) -> str:
    return f"「{plan['about']}」の{plan['angle']}にまつわる、意外で面白い豆知識を調べて。"


def research(plan: dict) -> str | None:
    """plan（night_history.choose_plan の戻り値、type が fact）の豆知識を調べる。

    返り値は箇条書きの本文。検索が走らなかったら None。API の失敗は例外のまま上げる。
    """
    found = search(build_question(plan), plan["about"], SYSTEM)
    return found["text"].strip() if found else None
