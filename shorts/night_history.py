"""夜の動画で話したことの台帳（data/night_used.jsonl）と、次の回の型・切り口の選び方。

2026-10-10〜17 の夜版は、8本中7本の「意外な事実」が脳・ホルモンの話だった
（報酬系だけで3回）。毎回同じ問いを1回で投げていたので、LLM がいちばん
ありがちな答え（感情 → 脳）に収束していた。禁止語で蓋をするのではなく、

  1. 企画の段で、型ごと・切り口ごとに候補を出させ
  2. 直近で使っていない型・切り口をコードが選び
  3. 使ったものをここに残して、次の回の選択とプロンプトに渡す

ことで、毎回違う話になるようにする。台帳は Bluesky に投稿できた回だけ書く
（試し撮りで消費しない。クイズの QUIZ_NO_CONSUME と同じ考え方）。
"""

import datetime
import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
LEDGER = Path(os.getenv("NIGHT_USED_JSONL") or str(_ROOT / "data" / "night_used.jsonl"))

# ②（NagiCorner）の2ステップ目の型。1（状況）と3（本音）は共通
NIGHT_TYPES = ("fact", "question", "reframe", "metaphor")

# 型 fact の切り口。脳や心理学は「心と体」に入る。禁止はしないが、他と同じく
# 直近で使っていれば選ばれない
FACT_ANGLES = ("語源・言葉", "歴史・由来", "世界の習慣", "動物・植物",
               "数字・記録", "道具・技術", "季節・暮らし", "心と体")

# ③決め台詞の形。どれも言い終わったところに①掴みが続く（ループ）。
# 1つの形だけだと「〜なあなたに、この言葉を贈りたいな」ばかりになった（2026-10-18 の試し書きで7本中6本）
CLOSING_FORMS = ("announce", "call", "because", "ask_back")

# 何日ぶんの台帳を選択とプロンプトに使うか
RECENT_DAYS = 14


def load_recent(days: int = RECENT_DAYS, path: Path = None, today: str = None) -> list[dict]:
    """直近 days 日の記録を古い順に返す。同じ日に撮り直した回は最後の1件だけ残す。"""
    path = path or LEDGER
    if not path.exists():
        return []
    today = datetime.date.fromisoformat(today) if today else datetime.date.today()
    since = (today - datetime.timedelta(days=days)).isoformat()
    by_date = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and str(r.get("date", "")) >= since:
            by_date[r["date"]] = r
    return [by_date[d] for d in sorted(by_date)]


def append(job: dict, path: Path = None) -> None:
    """投稿できた回を残す。job は pipeline.py が書く投稿用 JSON。"""
    path = path or LEDGER
    plan = job.get("plan") or {}
    row = {"date": job["date"], "type": plan.get("type"), "angle": plan.get("angle"),
           "about": plan.get("about"), "idea": plan.get("idea"), "closing_form": plan.get("closing_form"),
           "hook": job.get("hook_ja"),
           "closing": job.get("closing_ja")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _least_recent(options, used: list) -> object:
    """options のうち、used（古い順）で最後に使ったのがいちばん古いもの。
    一度も使っていないものが最優先。並びが同じなら options の順。"""
    def last_use(o):
        return max((i for i, u in enumerate(used) if u == o), default=-1)
    return min(options, key=last_use)


def choose_plan(ideas: dict | None, history: list[dict], exclude: tuple = ()) -> dict:
    """企画の候補（ideas）から、直近で使っていない型・切り口を選ぶ。

    ideas は企画 LLM の出力（{"chosen_post": int, "ideas": {...}}）。
    None（企画に失敗した）でも型と切り口は台帳から選ぶので、多様さは保てる。
    中身（idea）が空なら、台本の LLM がその型・切り口で自分で考える。
    exclude に入れた型は選ばない（豆知識を調べられなかった日に fact を外す）。
    """
    ideas = ideas or {}
    pool = ideas.get("ideas") or {}
    # 何についての事実か（about）が無い候補は、気持ちの一般論になりやすいので使わない
    facts = [f for f in (pool.get("fact") or [])
             if isinstance(f, dict) and f.get("angle") in FACT_ANGLES
             and (f.get("idea") or "").strip() and (f.get("about") or "").strip()]

    def has_idea(t):
        return bool(facts) if t == "fact" else bool((pool.get(t) or "").strip())

    allowed = [t for t in NIGHT_TYPES if t not in exclude] or list(NIGHT_TYPES)
    types = [t for t in allowed if has_idea(t)] or allowed
    kind = _least_recent(types, [h.get("type") for h in history])

    angle, idea, about = None, "", ""
    if kind == "fact":
        used_angles = [h.get("angle") for h in history if h.get("type") == "fact"]
        if facts:
            # 同点（どれも未使用など）のときは FACT_ANGLES の順。LLM は「心と体」を先頭に
            # 書きがちで、候補の順のままだと台帳が空の日に脳の話が選ばれた（2026-10-18 の試し書き）
            angles = sorted({f["angle"] for f in facts}, key=FACT_ANGLES.index)
            best = _least_recent(angles, used_angles)
            pick = next(f for f in facts if f["angle"] == best)
            angle, idea, about = pick["angle"], pick["idea"].strip(), pick["about"].strip()
        else:
            angle = _least_recent(FACT_ANGLES, used_angles)
    else:
        idea = (pool.get(kind) or "").strip()

    closing_form = _least_recent(CLOSING_FORMS, [h.get("closing_form") for h in history])
    chosen = ideas.get("chosen_post")
    return {"type": kind, "angle": angle, "about": about, "idea": idea, "closing_form": closing_form,
            "picked_post": chosen if isinstance(chosen, int) else None}
