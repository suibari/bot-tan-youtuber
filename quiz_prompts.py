#!/usr/bin/env python3
"""
朝のクイズ動画用のプロンプトとスキーマ

キャラクター設定は夜版と共有する（prompts.CHARACTER_PROMPT）。
番組構成は朝版固有なのでここに置く。

重要な設計方針:
  クイズの事実（正解・解説の内容）は人間がファクトチェック済みのCSVで担保している。
  LLMには「口調を整える」ことだけを任せ、事実の追加・改変を厳しく禁じる。
  ここを緩めるとCSV方式にした意味がなくなる。
"""

from prompts import CHARACTER_PROMPT
from quiz_data import answer_text, wrong_text

QUIZ_SYSTEM_PROMPT = CHARACTER_PROMPT + """
【この動画について】
朝6時に配信する、約55秒の「日常の勘違い・雑学クイズ」ショート動画の台本を書く。
視聴者はこれから一日を始める人。眠い頭でも聞ける、軽くて明るいテンションで話すこと。

【絶対に守ること】
- 与えられた【クイズデータ】の事実（正解・解説の内容）を一切改変しないこと
- 解説に書かれていない数値・年号・人名・地名・研究名・団体名を新たに追加しないこと
  クイズデータは人間がファクトチェックしたものなので、補足を足すと誤情報になる
- 解説はbotたんの口調に整えるだけ。情報量を増やさない、減らしすぎない
- 選択肢の文言はデータのまま使うこと（言い換えない）
- 「諸説あります」のような逃げの言い回しは入れない

【出力ルール】
- 日本語のみで出力する
- textフィールドに[Happy][Sad]などの感情タグを含めないこと。感情はvalence/arousalで表現する
- 1文は短く区切る（字幕が読みやすくなる）
- valence/arousalは -1.0〜1.0 の実数。パートの雰囲気に合わせて使い分けること
  - valence: ネガティブ(-1.0) 〜 ポジティブ(+1.0)
  - arousal: 落ち着き(-1.0) 〜 興奮(+1.0)
"""

_SENTENCE = {
    "type": "object",
    "properties": {
        "text":    {"type": "string"},
        "valence": {"type": "number"},
        "arousal": {"type": "number"},
    },
    "required": ["text", "valence", "arousal"],
}

QUIZ_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "question_intro": {"type": "array", "items": _SENTENCE},
        "answer_reveal":  {"type": "array", "items": _SENTENCE},
        "explanation":    {"type": "array", "items": _SENTENCE},
        "affirmation":    {"type": "array", "items": _SENTENCE},
        "thumbnail_text": {"type": "string"},
        "title_hook":     {"type": "string"},
    },
    "required": ["question_intro", "answer_reveal", "explanation",
                 "affirmation", "thumbnail_text", "title_hook"],
}


def build_quiz_user_prompt(quiz: dict) -> str:
    ans = quiz["正解"]
    return f"""以下のクイズをもとに、YouTube Shorts用の台本を書いてください。

【クイズデータ】（ファクトチェック済み。事実を改変しないこと）
問題　　: {quiz["問題"]}
選択肢A : {quiz["選択肢A"]}
選択肢B : {quiz["選択肢B"]}
正解　　: {ans}（{answer_text(quiz)}）
不正解　: {"B" if ans == "A" else "A"}（{wrong_text(quiz)}）
解説　　: {quiz["解説"]}

【書いてほしいパート】

① question_intro（約5秒・25文字程度・2〜3文）
  - 「今日の勘違いクイズ！」のような短い掛け声で始める
  - 続けて問題文を読み上げる（データの問題文をほぼそのまま使う）
  - 「AとB、どっちだと思う？」で締める
  - わくわくした雰囲気にする（valence 高め、arousal 高め）

② answer_reveal（約3秒・15文字程度・1〜2文）
  - 「正解は……{ans}！」の形で発表する
  - 驚きが伝わるように arousal をいちばん高くする

③ explanation（約22秒・80文字程度・3〜5文）
  - 【クイズデータ】の解説を、botたんの口調に整える
  - 「〜なんだ」「〜なんだって」「〜って言われてるよ」など柔らかい言い回しにする
  - 事実・数値は解説に書かれているものだけを使う
  - 落ち着いたトーン（arousal は低め）

④ affirmation（約10秒・35文字程度・2〜3文）
  - この豆知識を知らなかった視聴者を全肯定する
  - 「間違えても大丈夫」「知らなかったってことは、今日ひとつ知れたってこと」の方向性
  - 説教くさくしない。朝の背中をそっと押す一言にする
  - **必ずこのクイズの内容に紐づけること**（どのクイズでも使える一般論にしない）
  - いちばん温かいトーン（valence を最大に）

⑤ thumbnail_text（20文字以内）
  - サムネイルに出す一言。問題を端的に表し、思わず考えたくなるもの

⑥ title_hook（25文字以内）
  - YouTubeタイトル用の引きのある一言

重要：合計の尺は55秒以内。各パートの文字数の目安を大きく超えないこと。
"""


def build_fallback_script(quiz: dict) -> dict:
    """LLMが全モデルで失敗したときに、CSVだけから組む台本。

    無人実行なので、LLMが落ちても動画が作れるようにしておく。
    口調の自然さは落ちるが、事実は完全にCSVどおりになる。
    """
    ans = quiz["正解"]
    return {
        "question_intro": [
            {"text": "今日の勘違いクイズ！",       "valence": 0.8, "arousal": 0.8},
            {"text": quiz["問題"],                  "valence": 0.5, "arousal": 0.6},
            {"text": "AとB、どっちだと思う？",      "valence": 0.6, "arousal": 0.7},
        ],
        "answer_reveal": [
            {"text": f"正解は、{ans}の{answer_text(quiz)}！", "valence": 0.9, "arousal": 0.9},
        ],
        "explanation": [
            {"text": quiz["解説"], "valence": 0.7, "arousal": 0.0},
        ],
        "affirmation": [
            {"text": "知らなかったってことは、今日ひとつ知れたってことだよ。", "valence": 1.0, "arousal": 0.2},
            {"text": "それってすごく素敵なことだと思うんだ。",                 "valence": 1.0, "arousal": 0.3},
        ],
        "thumbnail_text": quiz["問題"][:20],
        "title_hook":     quiz["問題"][:25],
    }


def validate_script(script: dict, quiz: dict) -> list[str]:
    """LLM出力の妥当性を検査して、警告メッセージのリストを返す。

    事実誤認の完全な検出はできないが、明らかな逸脱は拾えるようにしておく。
    """
    warnings = []

    for key in ("question_intro", "answer_reveal", "explanation", "affirmation"):
        sents = script.get(key)
        if not isinstance(sents, list) or not sents:
            warnings.append(f"{key} が空です")
            continue
        for s in sents:
            if not (s.get("text") or "").strip():
                warnings.append(f"{key} に空のtextがあります")

    # 正解発表に正解のラベルが含まれているか
    reveal = "".join(s.get("text", "") for s in script.get("answer_reveal", []))
    if quiz["正解"] not in reveal and answer_text(quiz) not in reveal:
        warnings.append(
            f"answer_reveal に正解（{quiz['正解']}／{answer_text(quiz)}）が見当たりません: {reveal!r}")

    # 解説に元データに無い数字が入っていないか（ハルシネーションの簡易検出）
    import re
    src_nums = set(re.findall(r"\d+", quiz["解説"] + quiz["問題"]))
    expl = "".join(s.get("text", "") for s in script.get("explanation", []))
    new_nums = [n for n in re.findall(r"\d+", expl) if n not in src_nums]
    if new_nums:
        warnings.append(f"explanation に元データに無い数字があります: {new_nums}（要確認）")

    for key, limit in (("thumbnail_text", 20), ("title_hook", 25)):
        val = (script.get(key) or "").strip()
        if not val:
            warnings.append(f"{key} が空です")
        elif len(val) > limit:
            warnings.append(f"{key} が{len(val)}文字（上限{limit}）")

    return warnings
