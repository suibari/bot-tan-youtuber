#!/usr/bin/env python3
"""
朝のクイズ動画用のデータ層

- data/quiz.csv        : 人間がファクトチェックして編集する問題データ（read-only）
- data/quiz_used.csv   : パイプラインが追記する消費台帳（append-only）

人間所有ファイルと機械所有ファイルを分離しているのは、人間が問題を追加編集して
いる最中にパイプラインが上書きする事故を防ぐため。

季節の挨拶もここに置く（LLMを通さず決定論的に決める）。
"""

import os
import csv
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

_JST = timezone(timedelta(hours=9))

DATA_DIR  = Path(__file__).parent / "data"
QUIZ_CSV  = Path(os.getenv("QUIZ_CSV",  DATA_DIR / "quiz.csv"))
USED_CSV  = Path(os.getenv("QUIZ_USED_CSV", DATA_DIR / "quiz_used.csv"))

# 最終使用からこの日数が経過したら再利用してよい
COOLDOWN_DAYS = int(os.getenv("QUIZ_COOLDOWN_DAYS", "30"))

REQUIRED_COLUMNS = ("id", "問題", "選択肢A", "選択肢B", "正解", "解説")

# ffmpeg drawtext で事故る文字。CSV側で潰しておく（描画側の expansion=none と二重防御）
_DANGEROUS = ("\\", "%{")


# ──────────────────────────────────────────────
# クイズCSVの読み込み
# ──────────────────────────────────────────────

class QuizDataError(Exception):
    """CSVの内容が不正で、そのまま動画にすると事故になる場合に投げる"""


def _sanitize(text: str, where: str) -> str:
    cleaned = text
    for bad in _DANGEROUS:
        if bad in cleaned:
            print(f"[クイズ] 警告: {where} に描画できない文字 {bad!r} が含まれるため除去します")
            cleaned = cleaned.replace(bad, "")
    return cleaned.strip()


def load_quizzes(path: Path = None) -> list[dict]:
    """quiz.csv を読み込んでバリデーションする。

    不正な行は例外にする。無音や空欄のまま投稿されるより、止まったほうがましなため。
    """
    path = Path(path or QUIZ_CSV)
    if not path.exists():
        raise QuizDataError(f"クイズCSVが見つかりません: {path}")

    quizzes = []
    seen_ids = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise QuizDataError(f"必須列が不足しています: {missing} (実際の列: {reader.fieldnames})")

        for lineno, row in enumerate(reader, start=2):
            if not (row.get("id") or "").strip():
                continue  # 空行はスキップ

            try:
                quiz_id = int(row["id"])
            except ValueError:
                raise QuizDataError(f"{path}:{lineno} id が整数ではありません: {row['id']!r}")
            if quiz_id in seen_ids:
                raise QuizDataError(f"{path}:{lineno} id が重複しています: {quiz_id}")
            seen_ids.add(quiz_id)

            for col in REQUIRED_COLUMNS:
                if not (row.get(col) or "").strip():
                    raise QuizDataError(f"{path}:{lineno} (id={quiz_id}) 列 '{col}' が空です")

            answer = row["正解"].strip().upper()
            if answer not in ("A", "B"):
                raise QuizDataError(
                    f"{path}:{lineno} (id={quiz_id}) 正解は A か B である必要があります: {row['正解']!r}")

            quiz = {
                "id":       quiz_id,
                "問題":     _sanitize(row["問題"],   f"id={quiz_id} の問題"),
                "選択肢A":  _sanitize(row["選択肢A"], f"id={quiz_id} の選択肢A"),
                "選択肢B":  _sanitize(row["選択肢B"], f"id={quiz_id} の選択肢B"),
                "正解":     answer,
                "解説":     _sanitize(row["解説"],   f"id={quiz_id} の解説"),
                "カテゴリ": (row.get("カテゴリ") or "").strip(),
                "出典メモ": (row.get("出典メモ") or "").strip(),
            }

            # レイアウトが崩れるだけなので警告に留める
            if len(quiz["問題"]) > 40:
                print(f"[クイズ] 警告: id={quiz_id} の問題文が{len(quiz['問題'])}文字（推奨40文字以内）")
            for c in ("選択肢A", "選択肢B"):
                if len(quiz[c]) > 14:
                    print(f"[クイズ] 警告: id={quiz_id} の{c}が{len(quiz[c])}文字（推奨14文字以内）")

            quizzes.append(quiz)

    if not quizzes:
        raise QuizDataError(f"クイズが1件もありません: {path}")

    quizzes.sort(key=lambda q: q["id"])
    return quizzes


def answer_text(quiz: dict) -> str:
    """正解の選択肢テキストを返す"""
    return quiz["選択肢A"] if quiz["正解"] == "A" else quiz["選択肢B"]


def wrong_text(quiz: dict) -> str:
    """不正解の選択肢テキストを返す"""
    return quiz["選択肢B"] if quiz["正解"] == "A" else quiz["選択肢A"]


def shuffle_choices(quiz: dict, now=None) -> dict:
    """選択肢A/Bを入れ替えたコピーを返す（入れ替えないこともある）。

    CSVは「誤解されている説がA・正しい説がB」という作問パターンのせいで
    正解がBに偏っているので、ここで均す。日付とidをシードにした決定論的な
    入れ替えなので、同じ日の再実行では同じ並びになる（Unity録画のリトライや
    --stage ffmpeg での再合成で正解位置がズレない）。
    """
    now = now or datetime.now(_JST)
    rng = random.Random(f"{now.strftime('%Y-%m-%d')}-{quiz['id']}")
    if not rng.getrandbits(1):
        return dict(quiz)

    swapped = dict(quiz)
    swapped["選択肢A"], swapped["選択肢B"] = quiz["選択肢B"], quiz["選択肢A"]
    swapped["正解"] = "B" if quiz["正解"] == "A" else "A"
    return swapped


# ──────────────────────────────────────────────
# 消費台帳
# ──────────────────────────────────────────────

def load_last_used(path: Path = None) -> dict[int, datetime]:
    """{quiz_id: 最終使用日時} を返す。台帳が無ければ空辞書。"""
    path = Path(path or USED_CSV)
    if not path.exists():
        return {}

    last: dict[int, datetime] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                quiz_id = int(row["id"])
                used_at = datetime.fromisoformat(row["used_at"])
            except (KeyError, ValueError):
                continue
            if used_at.tzinfo is None:
                used_at = used_at.replace(tzinfo=_JST)
            if quiz_id not in last or used_at > last[quiz_id]:
                last[quiz_id] = used_at
    return last


def mark_used(quiz_id: int, video_url: str = "", path: Path = None, now=None) -> None:
    """消費台帳に追記する。アップロード成功後にだけ呼ぶこと。"""
    path = Path(path or USED_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["id", "used_at", "video_url"])
        writer.writerow([quiz_id, (now or datetime.now(_JST)).isoformat(), video_url])
    print(f"[クイズ] 消費を記録: id={quiz_id}")


def next_quiz(quiz_id: int = None, now=None, quizzes: list[dict] = None) -> dict:
    """その日に使うクイズを1件選ぶ。

    未使用を最優先し、その中ではランダム。全て使用済みなら
    COOLDOWN_DAYS 以上経過したものからランダムに再利用する。
    在庫が尽きてもエラーにはせず、最も古く使ったものを返す。

    日付をシードにしているので、同じ日に再実行すると同じ問題が選ばれる
    （Unity録画のリトライで別の問題にすり替わらない）。

    返す直前に shuffle_choices() を通すので、選択肢A/Bの並びはCSVのままとは
    限らない。下流（描画・台本・概要欄）はこの dict の内容しか見ないため、
    ここで入れ替えておけば全体の整合が取れる。
    """
    quizzes = quizzes if quizzes is not None else load_quizzes()
    now = now or datetime.now(_JST)

    if quiz_id is not None:
        for q in quizzes:
            if q["id"] == quiz_id:
                return shuffle_choices(q, now)
        raise QuizDataError(f"id={quiz_id} のクイズが見つかりません")

    last_used = load_last_used()
    rng = random.Random(now.strftime("%Y-%m-%d"))

    unused = [q for q in quizzes if q["id"] not in last_used]
    if unused:
        chosen = rng.choice(unused)
        print(f"[クイズ] 未使用から選択: id={chosen['id']} (未使用 {len(unused)}/{len(quizzes)}件)")
        return shuffle_choices(chosen, now)

    cooled = [q for q in quizzes
              if (now - last_used[q["id"]]).days >= COOLDOWN_DAYS]
    if cooled:
        chosen = rng.choice(cooled)
        print(f"[クイズ] 再利用可能から選択: id={chosen['id']} "
              f"(クールダウン明け {len(cooled)}/{len(quizzes)}件)")
        return shuffle_choices(chosen, now)

    chosen = min(quizzes, key=lambda q: last_used[q["id"]])
    print(f"[クイズ] 全問クールダウン中のため最古を再利用: id={chosen['id']} "
          f"(最終使用 {last_used[chosen['id']].date()})")
    return shuffle_choices(chosen, now)


# ──────────────────────────────────────────────
# 季節の挨拶
# ──────────────────────────────────────────────

# 固定日（MM-DD）
SEASONAL_GREETINGS = {
    "01-01": "あけましておめでとう！今年もよろしくね。",
    "01-07": "今日は七草がゆの日だよ。",
    "02-03": "今日は節分だね。豆まきする？",
    "02-14": "今日はバレンタインだね。",
    "03-03": "今日はひな祭りだよ。",
    "03-14": "今日はホワイトデーだね。",
    "04-01": "今日はエイプリルフールだよ。優しい嘘なら大歓迎。",
    "04-29": "今日は昭和の日だね。",
    "05-03": "今日は憲法記念日だよ。",
    "05-05": "今日はこどもの日だね。",
    "06-01": "今日から6月だね。衣替えの季節だよ。",
    "07-07": "今日は七夕だよ。お願いごとは決まった？",
    "08-11": "今日は山の日だね。",
    "09-01": "今日は防災の日だよ。",
    "10-31": "今日はハロウィンだよ。",
    "11-03": "今日は文化の日だね。",
    "11-15": "今日は七五三だね。",
    "11-23": "今日は勤労感謝の日だよ。いつもおつかれさま。",
    "12-24": "今日はクリスマスイブだね。",
    "12-25": "メリークリスマス！",
    "12-31": "今日は大晦日だよ。一年おつかれさま。",
}

# 移動祝日: (月, 第n週, 曜日[月曜=0], テキスト)
NTH_WEEKDAY_GREETINGS = [
    (1,  2, 0, "今日は成人の日だね。"),
    (7,  3, 0, "今日は海の日だよ。"),
    (9,  3, 0, "今日は敬老の日だね。"),
    (10, 2, 0, "今日はスポーツの日だよ。"),
]

# 該当日以外のデフォルト（曜日で決まるので同日再実行でも同じ）
DEFAULT_GREETINGS = [
    "今日も一週間がはじまるね。",          # 月
    "火曜日、まだまだこれからだよ。",      # 火
    "水曜日、ちょうど折り返しだね。",      # 水
    "木曜日、もうちょっとだよ。",          # 木
    "金曜日、今日を乗り切ろうね。",        # 金
    "土曜日、ゆっくりできるといいね。",    # 土
    "日曜日、いい一日にしようね。",        # 日
]

CLOSING_TEXT = "全肯定SNSのNagiから来たbotたんでした。行ってらっしゃい！"


def pick_greeting(now=None) -> str:
    """その日の挨拶を返す。記念日があればそれ、なければ曜日別のデフォルト。"""
    now = now or datetime.now(_JST)

    key = now.strftime("%m-%d")
    if key in SEASONAL_GREETINGS:
        return SEASONAL_GREETINGS[key]

    for month, nth, weekday, text in NTH_WEEKDAY_GREETINGS:
        if now.month == month and now.weekday() == weekday and (now.day - 1) // 7 + 1 == nth:
            return text

    return DEFAULT_GREETINGS[now.weekday()]


def build_ending_sentences(now=None) -> list[dict]:
    """エンディング（季節の挨拶 + 固定クロージング）。LLMを通さない。"""
    return [
        {"text": pick_greeting(now), "valence": 0.8, "arousal": 0.4},
        {"text": CLOSING_TEXT,       "valence": 1.0, "arousal": 0.6},
    ]
