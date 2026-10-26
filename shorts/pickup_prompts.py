#!/usr/bin/env python3
"""
朝の作品紹介動画のプロンプト・スキーマ・台本の検証

キャラクター設定は夜版と共有する（prompts.CHARACTER_PROMPT）。

重要な設計方針:
  事実は、作品データ（pickup_sources）と調査メモ（pickup_research）にあるものだけを使う。
  LLM には「口調を整えて要約する」ことだけを任せ、事実を足させない（quiz_prompts と同じ）。

2026-10-17 の PoC（7テーマ・20本）で分かったことを、プロンプトではなくコードで担保する:
  - 旧作の「〇年前の今週」は LLM に書かせない。放送日が3日前なのに「10年前の今日」と書いた
  - 字数は 12B のモデルに数えさせても収まらない（3回作り直しても8本中5本が超えた）。
    合計で判定し、超えたら what / push の最後の文をコードで落とす
  - 賞の名前を縮めて大きく見せる（「ギャラクシー賞月間賞」→「ギャラクシー賞を受賞」）。
    メモの表記と照合する
  - 対応機種・配信サービスは台詞に出させない。調査メモの機種が間違っていた（発売日未定の Switch 版）

2026-10-27 の見直し（5秒で視聴継続が約50%、平均視聴が約10秒だった）:
  - 掴みが毎回「〇〇って知ってる？」になり、一番強い事実は PUSH（11〜15秒）まで出なかった。
    掴みで一番の推しを言い切る。型（HOOK_STYLES）を3つ書かせ、日ごとにコードが選ぶ
  - INFO はパネルに出ている日付を読むだけだったので、日付を言わず一言にする
"""

import re
from textwrap import indent as _indent

from prompts import CHARACTER_PROMPT, REACTION_RULES
from common.motion_safety import MOTION_PROMPT_RULES
from pickup_sources import OLD_THEMES, jp_date

# 発話パート（再生順）。END は quiz_data.build_ending_sentences の固定文
PARTS = ("hook", "what", "push", "info")
# 目安の字数（プロンプトに書く）と、合計の上限（コードで守る）。
# Irodori の実測は 4.5〜6文字/秒（2026-10-17。146字で HOOK〜INFO が32秒、全体40秒になった）。
# 115字で約23秒 + エンディング約8秒（2026-10-27 に INFO で日付を読むのをやめて 120字から減らした）
PART_CHARS = {"hook": 20, "what": 40, "push": 40, "info": 15}
TOTAL_CHARS = 115

# 掴みの型。全部書かせて、日ごとにコードが選ぶ（同じ型が続かないように）。
# 「材料が無ければ空」を許すと、PoC（2026-10-27・3本）で record / gap を毎回空にして fan だけになった。
# 推し（hook_point）を先に書かせ、どの型もその言い換えにする
# 曜日でテーマが決まるので、型の数は 7 と互いに素にする（同じ曜日が同じ型に固定されない）
HOOK_STYLES = {
    "record": "実績・数字・反響で言い切る（例:「初回からトレンド1位の話題作！」）",
    "gap":    "意外な組み合わせ・ギャップ・斬新な仕掛けで言い切る（例:「可愛い絵柄なのに中身はブラック！」）",
    "fan":    "どんな人に刺さるかを、推しの中身ごと言い切る（例:「法廷ドラマ好きは見逃せない一本！」）",
}

# 旧作の INFO。コードで固定する（モジュール冒頭を参照）
OLD_INFOS = {
    "anime_old": "{n}年前の今週に始まった名作、ぜひ観てみてね！",
    "movie_old": "{n}年前の今週に公開された名作、ぜひ観てみてね！",
}

THEME_FRAMES = {
    "anime_new": "今期放送中の新作アニメを紹介する回。",
    "anime_old": "{n}年前の今週に放送・公開が始まった名作アニメを紹介する回。",
    "music_new": "最近リリースされて今よく聴かれている新曲を紹介する回。",
    "movie_new": "今週・先週に公開された新作映画を紹介する回。",
    "movie_old": "{n}年前の今週に公開された名作映画を紹介する回。",
    "drama_new": "今期放送中の新作ドラマを紹介する回。",
    "game_new":  "今週・先週に発売された新作ゲームを紹介する回。",
}

# 観た（聴いた・遊んだ）ことにしないための言い方。テーマで動詞を変える
VERBS = {"music_new": "聴いて", "game_new": "遊んで"}

# 調べもので間違いやすく、台詞に出さないもの（PoC で機種を取り違えた）
PLATFORM_WORDS = re.compile(
    r"Switch|スイッチ|PS5|PS4|PlayStation|プレステ|Xbox|Steam|スチーム|"
    r"Netflix|ネットフリックス|U-NEXT|Amazon|アマゾン|Prime|プライム|Disney|ディズニープラス|"
    r"ABEMA|アベマ|Hulu|dアニメ|Lemino|TVer|Apple TV")
# INFO で日付を読まない（パネルに出ている）
DATE_IN_INFO = re.compile(r"[0-9０-９]+月[0-9０-９]+日|[0-9０-９]{4}年")
# 日付を言い換えない（「〇年前の今日」と取り違えた）
DAY_WORDS = re.compile(r"今日|昨日|明日|きょう|きのう|あした")
# 人物の性別・年代を表す語。メモに無いのに台本に出たら取り違え。PoC の本番設定の試験で、
# 男性の主人公（メリーベリーラブのイ・ユビン）を「韓国の女性」と要約した（2026-10-17）
PERSON_WORDS = re.compile(r"女性|男性|女の子|男の子|少女|少年|女子|男子|彼女|お姉さん|お兄さん|おじさん|おばさん")
# 「〜賞」の名前。前の語（第94回など）を省くのはよいが、後ろ（月間賞・奨励賞）を省くと誇張になる
_AWARD = re.compile(r"[一-龥ァ-ヶーA-Za-z0-9・]+賞(?:[一-龥ァ-ヶー]+賞)*")
_GENERIC_AWARDS = {"受賞", "入賞", "授賞", "賞"}

SYSTEM_PROMPT = CHARACTER_PROMPT + """
【この動画について】
朝6時に投稿する、約30秒の「作品紹介」ショート動画の台本を書く。
視聴者はこれから一日を始める人。軽くて明るいテンションで話すこと。

【絶対に守ること】
- 使ってよい事実は【作品データ】と【調査メモ】に書かれていることだけ
- そこに書かれていない人名・数値・日付・あらすじ・受賞歴・評判を新たに足さないこと
  （削るのは可、足すのは不可。足すと誤情報になる）
- botたんはこの作品を観て（聴いて・遊んで）いない。**観た感想・体験を装わない**
  NG例:「観てて泣いちゃった」「何回も聴いてる」「3話がすごかった」
  OK例:「〇〇なところが推せるポイントなんだって」「ここ、気になるよね！」
- ネタバレになる展開・結末は書かない
- 作品名は【作品データ】の title の表記のまま使う
- 登場人物の性別・年代・関係は、調査メモに書かれているとおりにする（推測で「女性」「少年」などと書かない）
- 賞・記録・ランキングの名前は調査メモの表記を一字一句そのまま使う
  （「〇〇賞月間賞」を「〇〇賞」に縮めるなど、省略して大きく見せない）
- 日付は【作品データ】か調査メモに書かれたものを「10月9日」の形で言う。
  「今日」「昨日」のような言い換えはしない
- 対応機種・配信サービスの名前は言わない（情報が変わりやすく、間違えやすい）
- 暴力・流血・性的な表現を推しポイントにしない（朝の番組なので）
"""

_SENTENCE = {
    "type": "object",
    "properties": {
        "text":     {"type": "string"},
        "valence":  {"type": "number"},
        "arousal":  {"type": "number"},
        "motion":   {"type": "string"},
        # 必須にしないとローカルLLMは peak を省く（quiz_prompts と同じ）
        "reaction": {"type": "string", "enum": [
            "none", "surprising", "down", "thinking", "shame", "joyful", "smug", "sitting"]},
        "peak":     {"type": "string"},
    },
    "required": ["text", "valence", "arousal", "motion", "reaction", "peak"],
}

# hook は型ごとに1文ずつ書かせ、select_hook で1つにする。
# 書く順はスキーマの順になる。hook を先に書かせると、推しの事実を選ぶ前に掴みを書いて
# 「SNS好きなら絶対ハマるよ！」のような中身の無い一言になった（2026-10-27）。本文 → 推し → 掴みの順にする
_BODY_PARTS = tuple(p for p in PARTS if p != "hook")

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        **{key: {"type": "array", "items": _SENTENCE} for key in _BODY_PARTS},
        "hook_point": {"type": "string"},
        "hooks": {"type": "object",
                  "properties": {k: {"type": "string"} for k in HOOK_STYLES},
                  "required": list(HOOK_STYLES)},
        "hook_motion": {"type": "string"},
        "title_hook": {"type": "string"},
        # エンディングは固定文で、紐づける文が無いのでパート単位で受け取る
        "ending_motions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [*_BODY_PARTS, "hook_point", "hooks", "hook_motion", "title_hook", "ending_motions"],
}

_DATA_KEYS = ("title", "artist", "original_title", "genres", "studio", "developer",
              "overview", "chart_rank", "years_ago", "networks")


def build_user_prompt(item: dict, research: str, problems: list[str] = ()) -> str:
    frame = THEME_FRAMES[item["theme"]].format(n=item.get("years_ago", ""))
    data = [f"{k}: {item[k]}" for k in _DATA_KEYS if item.get(k)]
    data.append(f"date: {jp_date(item['date'])}")
    verb = VERBS.get(item["theme"], "観て")
    hooks = "\n".join(f"  - {k}: {v}" for k, v in HOOK_STYLES.items())
    info_rule = ("  - **この回の info はコードで決まっているので、短い1文を書くだけでよい（使われない）**"
                 if item["theme"] in OLD_THEMES else
                 f"  - 「ぜひ{verb}みてね」のような一言だけ。**日付は言わない**（画面に出ている）")
    retry = ""
    if problems:
        retry = "\n【前回の台本の問題（必ず直すこと）】\n" + "\n".join(f"- {p}" for p in problems) + "\n"
    return f"""以下の作品を紹介する YouTube Shorts の台本を書いてください。

【この回】{frame}

【作品データ】（公式のデータベースの値）
{chr(10).join(data)}

【調査メモ】（Google 検索で確認した事実）
{research}
{retry}
【動画の流れ】（再生順）
  hook（最初の一言・約4秒）→ what（どんな作品か）→ push（推しポイント）→ info（締めの一言）
  視聴者は最初の数秒で見続けるかを決める。**hook でこの作品の一番の推しを言い切る**

【書いてほしいパート】（書く順。what / push / info は sentence の配列）

① what（約8秒・{PART_CHARS['what']}文字以内・1〜2文）
  - どんな作品か。調査メモ・作品データのあらすじや概要を要約する
  - 説明でもテンションは落とさない

② push（約8秒・{PART_CHARS['push']}文字以内・1〜2文）
  - 全肯定botたんが推しポイントを全力で褒める。この動画の山場
  - 調査メモにある具体的な事実（キャスト、主題歌、話題になった点など）を根拠にする
  - 「〜なんだって」「〜が話題なんだ」のように、聞いた話として褒める

③ info（約3秒・{PART_CHARS['info']}文字以内・1文）
{info_rule}

④ hook_point — 調査メモの中で、この作品の一番の推し（実績・数字・意外さ・斬新さ）を1文で抜き出す

⑤ hooks（各{PART_CHARS['hook']}文字以内・1文）— 動画の最初の一言。hook_point を3つの型で言い切る
{hooks}
  - **3つとも必ず書く**。どれも hook_point の言い換えにする（どれを使うかはこちらで決める）
  - 問いかけ（「〜って知ってる？」）にしない。答えが「いいえ」なら、そこで見るのをやめられてしまう
  - 作品名とアートワークは画面に大きく出ているので、作品名は入れなくてよい
  - hook_motion に、掴みの動きを英語で1つ書く（カメラは顔のアップで、サムネもここから撮る）

⑥ title_hook（25文字以内）
  - YouTube タイトル用の引きのある一言。作品名は別に付くので入れない

⑦ motion — botたんの体の動き。各 sentence に**英語で**書くこと
  hook_motion はカメラが顔のアップなので、胸から上で見せる動きにする
  （手で顔を隠す動きは書かない）。
  ending_motions には、エンディング（朝のあいさつ →「行ってらっしゃい！」）の動きを
  英文1〜2つの配列で入れる（例: 手を振って見送る）。お辞儀は書かない。
  **その文の内容と動きを一致させること**。作品の題材を体で表すとよい
  （例: ギターの曲 → A person strums an air guitar in a feminine way.）

""" + _indent(MOTION_PROMPT_RULES, "  ") + """

⑧ reaction と peak — 山場の演出。各 sentence に書く。

""" + _indent(REACTION_RULES, "  ") + """

  この動画の決まり:
  - **push のどれか1文は必ず山場にする**（"joyful" か "surprising"）
  - 山場は push を含めて2文まで
  - hook には reaction を付けない（書く欄も無い）

重要：合計の尺は25秒以内。読み上げは約5文字/秒なので、各パートの文字数の目安を守ること。
"""


# ──────────────────────────────────────────────
# 検証と整形
# ──────────────────────────────────────────────

def _text(sentences) -> str:
    return "".join((s or {}).get("text", "") for s in sentences or [])


def _awards(text: str) -> set[str]:
    return {a for a in _AWARD.findall(text) if a not in _GENERIC_AWARDS}


def sentence_problems(text: str, sources: str) -> list[str]:
    """1文の問題。sources は作品データと調査メモを連結したもの。"""
    problems = []
    known = _awards(sources)
    for award in _awards(text):
        # 前を省く（第94回アカデミー賞 → アカデミー賞）のは可、後ろを省くのは不可
        if not any(k.endswith(award) for k in known):
            problems.append(f"「{award}」は調査メモの賞の名前と違う。メモの表記どおりにするか削ること")
    if DAY_WORDS.search(text):
        problems.append(f"「{DAY_WORDS.search(text).group()}」を使っている。日付で言うか削ること")
    for word in set(PERSON_WORDS.findall(text)):
        if word not in sources:
            problems.append(f"「{word}」は調査メモに無い。登場人物の性別・年代を推測で書かないこと")
    if PLATFORM_WORDS.search(text):
        problems.append(f"「{PLATFORM_WORDS.search(text).group()}」（機種・配信サービス）を言っている。削ること")
    return problems


def validate_script(script: dict, sources: str) -> list[str]:
    """作り直しを頼むほどの問題。字数はここでは見ない（fit_length で落とす）。"""
    problems = []
    for key in PARTS:
        sents = script.get(key)
        if not isinstance(sents, list) or not any((s or {}).get("text", "").strip() for s in sents):
            problems.append(f"{key} が空です")
            continue
        for s in sents:
            problems += [f"{key}: {p}" for p in sentence_problems(s.get("text", ""), sources)]
            if key == "info" and DATE_IN_INFO.search(s.get("text", "")):
                problems.append("info: 日付を言っている。日付は画面に出ているので「ぜひ観てみてね」の一言にすること")
            motion = (s.get("motion") or "").strip()
            if motion and not motion.isascii():
                problems.append(f"{key}: motion が英語ではありません: {motion[:40]!r}")
    return problems


def drop_bad_sentences(script: dict, sources: str) -> list[str]:
    """作り直しても残った問題の文を落とす。落とした文を返す（ログ用）。

    パートが空になることもある。hook / info は fill_missing で埋め、what / push が
    空なら呼ぶ側でその台本を使わない。
    """
    dropped = []
    for key in PARTS:
        sents = script.get(key) or []
        keep = [s for s in sents if not sentence_problems(s.get("text", ""), sources)
                and not (key == "info" and DATE_IN_INFO.search(s.get("text", "")))]
        dropped += [s["text"] for s in sents if s not in keep]
        script[key] = keep
    return dropped


def fill_missing(script: dict, item: dict) -> list[str]:
    """空になった hook / info を、データベースの値だけで作った固定文で埋める。埋めたキーを返す。"""
    verb = VERBS.get(item["theme"], "観て")
    fixed = {
        "hook": (f"今週の注目は『{item['title']}』！", "A person waves cheerfully in a feminine way."),
        "info": (f"ぜひ{verb}みてね！", "A person points forward with a smile in a feminine way."),
    }
    filled = []
    for key, (text, motion) in fixed.items():
        if not script.get(key):
            script[key] = [{"text": text, "valence": 0.8, "arousal": 0.5, "motion": motion,
                            "reaction": "none", "peak": ""}]
            filled.append(key)
    return filled


def total_chars(script: dict) -> int:
    return sum(len(_text(script.get(key))) for key in PARTS)


def fit_length(script: dict, limit: int = TOTAL_CHARS) -> list[str]:
    """合計が上限を超えたら、what と push の最後の文から落とす。落とした文を返す。

    山場（reaction が none 以外）の文は後回しにする。各パート1文は必ず残す。
    push の2文がどちらも山場だと落とせず、155字・39秒になった（2026-10-27）ので、
    山場しか無いパートは最後の文を落とす（残る1文が山場なので、山場は無くならない）。
    """
    dropped = []
    while total_chars(script) > limit:
        for key in ("what", "push"):
            sents = script.get(key) or []
            plain = [i for i, s in enumerate(sents) if (s.get("reaction") or "none") == "none"]
            if len(sents) > 1:
                dropped.append(sents.pop(plain[-1] if plain else -1)["text"])
                break
        else:
            break
    return dropped


def hook_order(day) -> list[str]:
    """その日に試す掴みの型の順。日ごとに先頭をずらす。"""
    styles = list(HOOK_STYLES)
    i = day.toordinal() % len(styles)
    return styles[i:] + styles[:i]


def select_hook(script: dict, day, sources: str) -> str:
    """hooks から1文を選んで script["hook"] にする。選んだ型を返す（空なら "" ）。

    その日の型から順に、問題の無い文を選ぶ。どれも問題があれば最初に書かれていた文を使い、
    validate_script で作り直しを頼む。
    """
    hooks = script.get("hooks") or {}
    written = [(k, (hooks.get(k) or "").strip()) for k in hook_order(day)]
    written = [(k, t) for k, t in written if t]
    if not written:
        script["hook"] = []
        return ""
    style, text = next(((k, t) for k, t in written if not sentence_problems(t, sources)), written[0])
    script["hook"] = [{
        "text": text, "valence": 0.8, "arousal": 0.7,
        "motion": (script.get("hook_motion") or "").strip()
                  or "A person waves cheerfully in a feminine way.",
        "reaction": "none", "peak": "",
    }]
    return style


def apply_fixed_info(script: dict, item: dict) -> None:
    """旧作の INFO を固定文に差し替える。motion は LLM が info に書いたものを使う。"""
    template = OLD_INFOS.get(item["theme"])
    if not template:
        return
    first = (script.get("info") or [{}])[0]
    script["info"] = [{
        "text": template.format(n=item["years_ago"]),
        "valence": first.get("valence", 0.8), "arousal": first.get("arousal", 0.5),
        "motion": first.get("motion") or "A person points forward with a smile in a feminine way.",
        "reaction": "none", "peak": "",
    }]


def ensure_peak(script: dict) -> None:
    """push のどれかを必ず山場にする。LLM が付け忘れたら最初の文に付ける。"""
    push = script.get("push") or []
    if push and not any((s.get("reaction") or "none") != "none" for s in push):
        push[0]["reaction"] = "joyful"
        push[0]["peak"] = push[0].get("peak") or ""
    for s in script.get("hook") or []:
        s["reaction"], s["peak"] = "none", ""
