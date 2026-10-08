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

import random
from textwrap import indent as _indent

from prompts import CHARACTER_PROMPT, REACTION_RULES
from common.motion_safety import MOTION_PROMPT_RULES
from quiz_data import answer_text, wrong_text

QUIZ_SYSTEM_PROMPT = CHARACTER_PROMPT + """
【この動画について】
朝6時に配信する、約30秒の「日常の勘違い・雑学クイズ」ショート動画の台本を書く。
テンポが命なので、どのパートも指定の文字数を必ず守ること。
視聴者はこれから一日を始める人。眠い頭でも聞ける、軽くて明るいテンションで話すこと。

【絶対に守ること】
- 与えられた【クイズデータ】の事実（正解・解説の内容）を一切改変しないこと
- 解説に書かれていない数値・年号・人名・地名・研究名・団体名を新たに追加しないこと
  クイズデータは人間がファクトチェックしたものなので、補足を足すと誤情報になる
- 解説はbotたんの口調に整えたうえで、指定の文字数まで要約してよい。
  削ってよいのは修飾・言い換え・出典の調査名や団体名まで。
  **核となる事実（なぜその答えが正解なのか）は必ず残すこと**
- 書かれていない事実・数値を足すのは引き続き禁止（削るのは可、足すのは不可）
- 選択肢の文言はデータのまま使うこと（言い換えない）
- 「諸説あります」のような逃げの言い回しは入れない

【出力ルール】
- **ローカルLLM（Ollama）では JSONのキーが名前のアルファベット順で出力される。**
  その場合の書く順番は affirmation → answer_reveal → explanation → motions →
  question_intro → thumbnail_text → title_hook になる。
  下の①〜⑦は動画の**再生順**であって、書く順ではない。
  **各キーには、そのキーの担当ぶんだけを入れること。**
  いちばん最初に書く affirmation に台本を丸ごと入れてしまう事故が実際に起きた。
  書き始める前に、頭の中で①〜⑦を全部組み立ててから、キーの順に取り出して書くこと。
- sentence の中のフィールドも同じ理由で arousal → motion → peak → reaction → text →
  valence の順になりうる。**text より先に motion・peak を書かされる**ので、
  「どんな文を言うか」を決めてから motion・peak を書くこと。peak は、そのあと書く
  text に一字一句そのまま含まれていなければならない。
  motion は必ず英文で書く（"motions.think" のようなキー名を入れてはいけない）。
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

# AI(ARDY)で毎回生成する体の動き。英語で書かせる:
# 日本語を投げると FuguMT の英訳が崩れ、その崩れた英文がモーションの条件になってしまう
#
# 夜版と同じく**文ごとに1つ**持たせる。パートの尺で按分する旧方式は、
# どの文に対応するかを見ていないので「話している内容と動きが合わない」原因だった。
_SENTENCE_M = {
    "type": "object",
    "properties": {
        "text":    {"type": "string"},
        "valence": {"type": "number"},
        "arousal": {"type": "number"},
        "motion":  {"type": "string"},
        # 山場の演出（ちびキャラの差分名）と強調語句。夜版と同じ（prompts.REACTION_RULES）。
        # 必須にしないとローカルLLMは peak を省く
        "reaction": {"type": "string", "enum": [
            "none", "surprising", "down", "thinking", "shame", "joyful", "smug", "sitting"]},
        "peak":     {"type": "string"},
    },
    "required": ["text", "valence", "arousal", "motion", "reaction", "peak"],
}

# シンキングタイムは発話が無く（カウントダウン音のみ）、エンディングは固定文
# （quiz_data.pick_greeting + CLOSING_TEXTS）でペルソナが書く文が無い。
# 紐づける文が無いこの2つだけはパート単位で受け取る
_MOTIONS = {
    "type": "object",
    "properties": {
        "think":  {"type": "array", "items": {"type": "string"}},
        "ending": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["think", "ending"],
}

QUIZ_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "question_intro": {"type": "array", "items": _SENTENCE_M},
        "answer_reveal":  {"type": "array", "items": _SENTENCE_M},
        "explanation":    {"type": "array", "items": _SENTENCE_M},
        "affirmation":    {"type": "array", "items": _SENTENCE_M},
        "thumbnail_text": {"type": "string"},
        "title_hook":     {"type": "string"},
        "motions":        _MOTIONS,
    },
    "required": ["question_intro", "answer_reveal", "explanation",
                 "affirmation", "thumbnail_text", "title_hook", "motions"],
}


# 冒頭の掛け声の例。例を1つに固定すると LLM が毎回それをそのまま使うので、回ごとに抽選する
# （毎日同じ出だしだと量産コンテンツに見える。shorts/turn.py の冒頭を参照）
OPENING_CALL_EXAMPLES = [
    "勘違いクイズ！",
    "朝のクイズだよ！",
    "ねえ、知ってた？",
    "ちょっと問題！",
    "今日のクイズ！",
]


def build_quiz_user_prompt(quiz: dict) -> str:
    ans = quiz["正解"]
    opening = random.choice(OPENING_CALL_EXAMPLES)
    return f"""以下のクイズをもとに、YouTube Shorts用の台本を書いてください。

【クイズデータ】（ファクトチェック済み。事実を改変しないこと）
問題　　: {quiz["問題"]}
選択肢A : {quiz["選択肢A"]}
選択肢B : {quiz["選択肢B"]}
正解　　: {ans}（{answer_text(quiz)}）
不正解　: {"B" if ans == "A" else "A"}（{wrong_text(quiz)}）
解説　　: {quiz["解説"]}

【書いてほしいパート】

① question_intro（約6秒・40文字以内・2文）— 問題文の文字数を含む
  - 「{opening}」のような**短い**掛け声で始める
  - 続けて問題文を読み上げる（データの問題文をほぼそのまま使う）
  - **選択肢A/Bの文言は画面に表示されるので、音声では読み上げないこと**
  - 「どっち？」の一言で締める（「AとB、どっちだと思う？」と長く言わない）
  - わくわくした雰囲気にする（valence 高め、arousal 高め）

② answer_reveal（約3秒・15文字以内・1〜2文）
  - 「正解は……{ans}！」の形で発表する
  - 驚きが伝わるように arousal をいちばん高くする

③ explanation（約7秒・45文字以内・2〜3文）
  - 【クイズデータ】の解説を、botたんの口調に整えて**45文字以内**に要約する
    （CSVの解説は60〜75文字あるので、必ず削る作業が要る。そのまま口調だけ直すと必ず超える）
  - 要約で落としてよいのは修飾・言い換え・出典の調査名や団体名。
    「なぜその答えが正解なのか」の核だけは必ず残すこと
  - 「〜なんだ」「〜なんだって」「〜って言われてるよ」など柔らかい言い回しにする
  - 事実・数値は解説に書かれているものだけを使う
  - 落ち着いたトーン（arousal は低め）

④ affirmation（約4秒・25文字以内・1文）
  - この豆知識を知らなかった視聴者を全肯定する
  - 「間違えても大丈夫」「知らなかったってことは、今日ひとつ知れたってこと」の方向性
  - 説教くさくしない。朝の背中をそっと押す一言にする
  - **必ずこのクイズの内容に紐づけること**（どのクイズでも使える一般論にしない）
  - いちばん温かいトーン（valence を最大に）

⑤ thumbnail_text（20文字以内）
  - サムネイルに出す一言。問題を端的に表し、思わず考えたくなるもの

⑥ title_hook（25文字以内）
  - YouTubeタイトル用の引きのある一言

⑦ motion — botたんの体の動き。**英語で**書くこと（日本語だと翻訳で崩れる）
  AIが3Dモデルを動かすための指示文です。

  **question_intro / answer_reveal / explanation / affirmation の各sentenceに
  "motion" を付ける**こと。
  question_intro はカメラが顔のアップで、サムネもここから撮る。胸から上で見せる動きにし、
  **手で顔を隠す動き（頬に手を添える・顔の前で手を動かす）は書かない**
  （例: 両手を胸の前で合わせてわくわくする、首をかしげて問いかける）。
  さらに motions.think に、シンキングタイム中の動きを**英文1つの配列**で入れること
  （ここだけ発話が無いので文に紐づけられない）。
  シンキングタイムは3秒しかないので、2つ入れても後ろは再生されずに捨てられる。
  motions.ending には、エンディング（朝のあいさつ →「行ってらっしゃい！」）の動きを
  **英文1〜2つの配列**で入れること。視聴者を朝に送り出す動きにする
  （例: 手を振って見送る）。**お辞儀は書かない**（モーション生成AIが深く頭を下げ、
  画面に頭頂部しか映らなくなる）。

  **最重要: その文の内容と動きが一致していること**。ただ動いていればよいのではない。
  文で言っていることを体で表す。合っていないと、見ていて不安になる画になる。
    例: 「正解はAだよ！」       → 片手を高く上げて発表する動き
    例: 「実はこうなんだって」  → 人差し指を立てて説明する動き
    例: 「知らなくて大丈夫だよ」→ 両手を胸の前で合わせて落ち着かせる動き

""" + _indent(MOTION_PROMPT_RULES, "  ") + """

  朝版だけの注意:
  - **手や腕を同じ場所で往復させ続ける動作は書かない**。3秒間ずっと手を震わせている
    画になり、見ていて不安になる（例: 胸の前で両手を上下に繰り返す）
  - **使ってはいけない動作**（実測で不自然な震えが出た）:
    頬に手を添える / 胸に手を当てる / 腕を組む
  - カメラは引いた全身の画になる。小さくまとまらず、思い切って大きく動かすこと
  - explanation の動きは**このクイズの題材そのものを体で表現する**。
    どのクイズでも使い回せる動きにしない
    （例: 猫がテーマ → A person makes cat paws with both hands in a feminine way.）

⑧ reaction と peak — 山場の演出。question_intro / answer_reveal / explanation /
  affirmation の各 sentence に書く。

""" + _indent(REACTION_RULES, "  ") + """

  朝版だけの決まり:
  - **answer_reveal は必ず山場にする**（正解を明かす文。"surprising" か "joyful"）。
    peak は正解を言う部分（例:「正解は……B！」）
  - 山場は answer_reveal を含めて**2〜3文**。どこを山場にするかは毎回このクイズに
    合わせて選ぶ（問いかけの「どっち？」、解説の意外な事実、全肯定の一言など）。
    毎回同じ場所にしないこと
  - question_intro の最初の文は "none"（顔のアップでサムネイルを撮るため）

重要：合計の尺は30秒以内。
**VOICEVOXの読み上げ速度は約6.5文字/秒**なので、各パートの文字数の目安を超えると
必ず尺オーバーになる。秒数より文字数を優先して、目安を超えないこと。
"""


def build_fallback_script(quiz: dict) -> dict:
    """LLMが全モデルで失敗したときに、CSVだけから組む台本。

    無人実行なので、LLMが落ちても動画が作れるようにしておく。
    口調の自然さは落ちるが、事実は完全にCSVどおりになる。
    """
    ans = quiz["正解"]
    return {
        "question_intro": [
            {"text": "勘違いクイズ！",  "valence": 0.8, "arousal": 0.8,
             "motion": "A person waves cheerfully in a feminine way."},
            {"text": quiz["問題"],      "valence": 0.5, "arousal": 0.6,
             "motion": "A person talks while gesturing with one hand in a feminine way."},
            {"text": "どっち？",        "valence": 0.6, "arousal": 0.7,
             "motion": "A person thinks while tilting their head in a feminine way."},
        ],
        "answer_reveal": [
            {"text": f"正解は、{ans}の{answer_text(quiz)}！", "valence": 0.9, "arousal": 0.9,
             "motion": "A person raises one hand proudly in a feminine way."},
        ],
        "explanation": [
            {"text": quiz["解説"], "valence": 0.7, "arousal": 0.0,
             "motion": "A person points upward while explaining in a feminine way."},
        ],
        "affirmation": [
            {"text": "知らなかったってことは、今日ひとつ知れたってことだよ。", "valence": 1.0, "arousal": 0.2,
             "motion": "A person clasps their hands in front of their chest in a feminine way."},
        ],
        "thumbnail_text": quiz["問題"][:20],
        "title_hook":     quiz["問題"][:25],
        # LLMが落ちたときは題材に紐づけられないので、どのクイズでも成立する汎用動作にする
        "motions": {
            "think": [
                "A person thinks while tilting their head in a feminine way.",
            ],
            "ending": [
                "A person waves cheerfully in a feminine way.",
            ],
        },
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

    # 文ごとの motion。ARDY に渡す英文なので、日本語のまま返ってくると
    # FuguMT の英訳が崩れて、その崩れた英文がモーションの条件になってしまう
    for key in ("question_intro", "answer_reveal", "explanation", "affirmation"):
        for i, sent in enumerate(script.get(key) or []):
            text = ((sent or {}).get("motion") or "").strip()
            if not text:
                warnings.append(f"{key}[{i}] に motion がありません")
            elif not text.isascii():
                warnings.append(f"{key}[{i}].motion が英語ではありません: {text[:40]!r}")

    think = (script.get("motions") or {}).get("think")
    if not isinstance(think, list) or not think:
        warnings.append("motions.think が空です")
    else:
        for i, text in enumerate(think):
            text = (text or "").strip()
            if not text:
                warnings.append(f"motions.think[{i}] が空です")
            elif not text.isascii():
                warnings.append(f"motions.think[{i}] が英語ではありません: {text[:40]!r}")

    # ending は無くても埋め草の動きで代わりが利くので、台本ごと捨てる
    # 「が空です」にはしない（quiz_pipeline がフォールバックの判定に使っている）
    ending = (script.get("motions") or {}).get("ending")
    if not isinstance(ending, list) or not any((t or "").strip() for t in ending):
        warnings.append("motions.ending がありません")
    else:
        for i, text in enumerate(ending):
            text = (text or "").strip()
            if text and not text.isascii():
                warnings.append(f"motions.ending[{i}] が英語ではありません: {text[:40]!r}")

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

    # 発話パートの文字数。超えるとそのまま尺オーバーになるので、
    # プロンプト調整の材料としてログに残す（生成は止めない）
    for key, limit in (("question_intro", 40), ("answer_reveal", 15),
                       ("explanation", 45), ("affirmation", 25)):
        text = "".join(s.get("text", "") for s in script.get(key) or [])
        if len(text) > limit:
            warnings.append(f"{key} が{len(text)}文字（目安{limit}）→ 尺オーバーの原因")

    for key, limit in (("thumbnail_text", 20), ("title_hook", 25)):
        val = (script.get(key) or "").strip()
        if not val:
            warnings.append(f"{key} が空です")
        elif len(val) > limit:
            warnings.append(f"{key} が{len(val)}文字（上限{limit}）")

    return warnings
