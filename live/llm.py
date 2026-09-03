"""配信の1返答を LLM に作らせる。

クライアントとリトライ・JSONパースは common/llm.py が原典（Shorts と共通）。
ここに残すのは配信固有のスキーマと、返ってきた値の正規化だけ。
"""

from common.llm import (  # noqa: F401
    LLM_MODELS,
    client,
    create as _create,
    generate_json,
    parse_json,
)


# 配信での1返答のスキーマ。
#
# 字幕を日英で出すので、文ごとに日英ペアで返させる。あとから機械分割すると
# 日本語と英語で文数がずれて対応が取れなくなるため、分割自体をLLMにやらせる。
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # 読み上げるのは ja だけ。en は字幕専用
                    "ja": {"type": "string"},
                    "en": {"type": "string"},
                },
                "required": ["ja", "en"],
            },
        },
        "valence": {"type": "number"},
        "arousal": {"type": "number"},
        # プール即応用。ARDY の生成を待たずに合うモーションを引くためのラベル
        "motion_category": {
            "type": "string",
            "enum": ["happy", "surprised", "thinking", "encouraging", "greeting", "neutral"],
        },
        # ARDY への非同期生成用の英文。字幕の en とは無関係なので混同しないこと
        "motion_en": {"type": "string"},
    },
    "required": ["lines", "valence", "arousal", "motion_category", "motion_en"],
}

MOTION_CATEGORIES = REPLY_SCHEMA["properties"]["motion_category"]["enum"]


def generate_reply(system_prompt: str, user_prompt: str,
                   history: list = None) -> dict:
    """配信の1返答を生成する。返り値は REPLY_SCHEMA の形。

    LLM はスキーマを守らないことがあるので、ここで最低限の正規化をしておく。
    配信中に KeyError で落ちるより、多少ぶれても喋り続けるほうが望ましい。

    history は配信中の会話（persona.build_history が作る）。渡さなければ
    従来どおり1問1答になる。
    """
    data = generate_json(system_prompt, user_prompt, REPLY_SCHEMA, "live_reply",
                         temperature=1.0, debug=False, history=history)
    return normalize_reply(data)


def normalize_reply(data: dict) -> dict:
    """欠けたキーを埋め、値域に収める。"""
    lines = []
    for item in (data.get("lines") or []):
        if not isinstance(item, dict):
            continue
        ja = (item.get("ja") or "").strip()
        if not ja:
            continue
        # en が欠けても日本語字幕だけ出して継続する。字幕のために配信は止めない
        lines.append({"ja": ja, "en": (item.get("en") or "").strip()})

    def clamp(name: str) -> float:
        try:
            return max(-1.0, min(1.0, float(data.get(name, 0.0))))
        except (TypeError, ValueError):
            return 0.0

    category = data.get("motion_category")
    if category not in MOTION_CATEGORIES:
        category = "neutral"

    return {
        "lines": lines,
        "valence": clamp("valence"),
        "arousal": clamp("arousal"),
        "motion_category": category,
        "motion_en": (data.get("motion_en") or "").strip(),
    }
