"""Shorts の英訳。

朝版（YouTube）: 英語圏の視聴者向けに、YouTube の字幕トラック（SRT）と英語版の
タイトル・説明文を付ける。**動画には焼き込まない。** 字幕帯は下にしか伸ばせず
（core.SUBTITLE_BAND_Y）、英語の行は Shorts の UI（タイトル・チャンネル名）が重なる位置に入るため。

夜版（Bluesky）: CC 字幕が無いので、英訳を日本語字幕の下段に焼き込む
（core.build_subtitle_filters の en_cues）。Bluesky の動画には Shorts の UI の重なりが無い。
動画ポストの添え文の英語版も、同じ呼び出しの最後の行として訳す。

英訳は台本が決まってから ollama に別途まとめて頼む（1本につき1回）。台本のスキーマに
en を足すと、12B のモデルに日英を同時に書かせることになり日本語台本が崩れうる
（prompts.py の mood_en を渡さない理由と同じ）。

朝のクイズは日本語の言葉そのものを題材にするので、「」で引用された語は訳さず
日本語のまま残させる。ローマ字にさせると 12B は読みを誤る（実測: 姑息 → "Koshoku"）。
語の意味を添えさせると問題文で答えを漏らす（実測: 「気の置けない友人」(close friend)）。

**英語まわりの失敗で投稿を止めないこと。** 訳せなければ日本語だけで公開する。
"""

import re

from common.env import env_flag
from common import llm as _llm

# 英語圏でのチャンネルの呼び名。タイトル末尾の「/ 全肯定botたん」の代わり
EN_NAME = "Bot-tan"

TRANSLATE_SYSTEM_PROMPT = f"""You translate Japanese YouTube Shorts lines into natural English subtitles.

The speaker is "{EN_NAME}" (全肯定botたん), a cheerful high-school girl character who affirms and
encourages everyone. Keep her voice: casual, warm, friendly, short sentences.

Rules:
- Translate each numbered line into exactly one English line, in the same order.
- Return exactly as many lines as you were given. Never merge, split, skip or add lines.
- Do not include the numbers in your output.
- Keep proper nouns as they are: "Nagi" is the name of a social network.
- Write "botたん" as "{EN_NAME}".
- Keep Japanese words or kanji quoted in 「」 exactly as written in Japanese (e.g. 「姑息」).
  Do not romanize them.
- Never add anything that is not in the Japanese line. Some lines are quiz questions,
  so never explain the quoted words or hint at the answer.
- Subtitles must be easy to read quickly. Prefer short, natural English over literal translation.
- The title must be under 70 characters and must not contain < or >."""

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
    },
    "required": ["lines", "title"],
}

# YouTube の制約（タイトル100文字・< > 不可）。超えると更新が丸ごと 400 になる
_TITLE_MAX = 100
_ANGLE = re.compile(r"[<>]")


def translate(lines: list[str], title: str) -> dict | None:
    """台本の各行とタイトルを英訳する。返り値は {"lines": [...], "title": str}。

    行数が合わなければ None（どの英文がどの台詞か分からないので、字幕にできない）。
    例外も None にする。英語のために投稿を止めない。
    """
    # 既定で有効。切るときは SHORTS_ENGLISH=false。
    # import 時に読むと load_dotenv より先に評価されうるので、呼ぶたびに読む
    if not env_flag("SHORTS_ENGLISH", True) or not lines:
        return None
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
    user_prompt = (f"Lines ({len(lines)}):\n{numbered}\n\n"
                   f"Title (translate into a short catchy English title):\n{title}")
    try:
        data = _llm.generate_json(TRANSLATE_SYSTEM_PROMPT, user_prompt, TRANSLATE_SCHEMA,
                                  schema_name="translation", temperature=0.3, debug=False)
    except Exception as e:
        print(f"[英語] 英訳に失敗しました（日本語だけで続行）: {e}")
        return None

    out = [(t or "").strip() for t in (data.get("lines") or []) if isinstance(t, str)]
    if len(out) != len(lines) or not all(out):
        print(f"[英語] 英訳の行数が合いません（{len(lines)}行 → {len(out)}行）。"
              f"日本語だけで続行します")
        return None
    en_title = _ANGLE.sub("", (data.get("title") or "").strip())
    print(f"[英語] 英訳完了: {len(out)}行 / {en_title}")
    return {"lines": out, "title": en_title}


def build_title(en_hook: str, suffix: str = EN_NAME) -> str:
    """英語版タイトル。日本語版の「{一言} / 全肯定botたん」に合わせる。"""
    tail = f" / {suffix}"
    head = _ANGLE.sub("", en_hook or "").strip()
    if not head:
        return f"{EN_NAME}'s daily affirmation"[:_TITLE_MAX]
    return head[:_TITLE_MAX - len(tail)].rstrip() + tail


def _srt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[dict]) -> str:
    """[{"start", "end", "text"}] を SRT にする。空の text と長さ0の区間は飛ばす。"""
    blocks = []
    for cue in cues:
        text = (cue.get("text") or "").strip()
        if not text or cue["end"] <= cue["start"]:
            continue
        blocks.append(f"{len(blocks) + 1}\n{_srt_time(cue['start'])} --> "
                      f"{_srt_time(cue['end'])}\n{text}\n")
    return "\n".join(blocks)


# ──────────────────────────────────────────────
# 英語版の説明文。訳さずに固定文で持つ（LLM に URL やクレジットを触らせない）
# ──────────────────────────────────────────────

_EN_FOOTER = """━━━━━━━━━━━━━━━━━━
👧 Who is Bot-tan?
A cheerful high-school girl who loves cheering everyone up on social media.
Reply to her and she will affirm anything you say!
http://bot-tan.com

🌐 Bot-tan lives on the social network "Nagi"
Post there and you'll get an affirming reply 💬
https://nagi.suibari.com

😎 About this project
Bot-tan wants to affirm the whole world!
Run by the developer suibari
https://note.com/suibari/n/n36e699f32479
━━━━━━━━━━━━━━━━━━"""


def build_description(kind: str, credits: str, quiz_line: str = "") -> str:
    """英語版の説明文。credits は日本語版と同じクレジット欄をそのまま使う。

    kind: "night" | "quiz"
    """
    if kind == "quiz":
        lead = ("Bot-tan brings you a morning quiz about common misconceptions.\n"
                "It's okay not to know. Learning one thing today is more than enough!")
    else:
        lead = "Bot-tan talks about what she felt on Nagi today."
    if quiz_line:
        lead += f"\n\n{quiz_line}"
    return f"""{lead}

🤖 This video is fully automated by AI.
Script, recording and upload are all done automatically.
English subtitles are available (turn on CC).

{_EN_FOOTER}
#bottan #VTuber #affirmation

{credits}"""
