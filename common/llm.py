"""LLM 呼び出し。Gemini の OpenAI 互換エンドポイント、または Ollama。

モデルは LLM_MODELS の左から順に attempts_per_model 回ずつ試し、
400系（リクエスト不正）は即時再送しても意味がないので1回で失敗させる。

以前は shorts/core.py と live/llm.py に同じ実装が2つあった。
"""

import json
import os
import re
import time

from openai import OpenAI, BadRequestError

from common.env import env_flag, env_float

USE_LOCAL_LLM   = env_flag("USE_LOCAL_LLM")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
LOCAL_LLM_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "hf.co/unsloth/gemma-4-12b-it-GGUF:Q4_K_M")
# カンマ区切りで複数指定でき、左から順にフォールバックする
GEMINI_MODELS = [
    m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").split(",") if m.strip()
]

# 1リクエストの上限[秒]。指定しないと OpenAI SDK の既定 600秒 が効き、
# さらに SDK 内部で2回リトライするので、下の create() の 3回 × モデル数 と
# 掛け合わさって理論上数十分ぶん待つことになる。配信ではメインループが
# そのまま止まってコメントに反応できなくなるので必ず縛る。
# 実測は gemini-2.5-flash の構造化出力（2〜3文）で約2秒。
# 録画パイプラインは待てるので、必要なら LLM_TIMEOUT_SEC で伸ばす
LLM_TIMEOUT_SEC = env_float("LLM_TIMEOUT_SEC", 20.0)

if USE_LOCAL_LLM:
    client = OpenAI(api_key="ollama", base_url=LOCAL_LLM_URL,
                    timeout=LLM_TIMEOUT_SEC, max_retries=0)
    LLM_MODELS = [LOCAL_LLM_MODEL]
    LLM_MODEL = LOCAL_LLM_MODEL
    print(f"[LLM] Ollama ({LLM_MODEL}) を使用します")
else:
    client = OpenAI(api_key=GEMINI_API_KEY, base_url=GEMINI_BASE_URL,
                    timeout=LLM_TIMEOUT_SEC, max_retries=0)
    LLM_MODELS = GEMINI_MODELS
    LLM_MODEL = LLM_MODELS[0]
    print(f"[LLM] Gemini ({', '.join(LLM_MODELS)}) を使用します")


# 再試行の間に置く待ち[秒]。左から順に使い、足りなければ最後の値を繰り返す。
#
# 以前は待ちが一切無く、429（レート制限）を食うと3回を一瞬で使い切って
# 次のモデルへ移っていた。間を置かずに投げ直すのはレート制限を悪化させるだけで、
# そのぶん配信のメインループが止まる。長く待ちすぎても止まるので、頭は打たせる。
LLM_RETRY_BACKOFF_SEC = (0.5, 1.0, 2.0)


def create(attempts_per_model: int = 3, **kwargs):
    """LLM_MODELS を左から順に attempts_per_model 回ずつ試す。

    SDK 側のリトライは切ってある（max_retries=0）。ここで数える回数が
    そのまま実際の試行回数なので、最悪の待ち時間は
    LLM_TIMEOUT_SEC × attempts_per_model × len(LLM_MODELS) に
    LLM_RETRY_BACKOFF_SEC の合計を足したもので見積もれる。
    """
    last_exc = None
    for model in LLM_MODELS:
        for attempt in range(1, attempts_per_model + 1):
            try:
                return client.chat.completions.create(model=model, **kwargs)
            except BadRequestError:
                # 400系はリトライ不要（プロンプトやスキーマの問題）
                raise
            except Exception as e:
                last_exc = e
                if attempt < attempts_per_model:
                    delay = LLM_RETRY_BACKOFF_SEC[
                        min(attempt - 1, len(LLM_RETRY_BACKOFF_SEC) - 1)]
                    print(f"[LLM] {model} 試行{attempt}失敗、{delay:.1f}秒待って"
                          f"リトライします... ({e})")
                    time.sleep(delay)
                else:
                    print(f"[LLM] {model} {attempts_per_model}回失敗、次のモデルへ移行します ({e})")
    raise last_exc


def parse_json(raw: str) -> dict:
    """LLM構造化出力のJSONをパースする。```json フェンスを剥がしてから json.loads する。"""
    cleaned = re.sub(r"```json|```", "", raw).strip()
    return json.loads(cleaned)


def generate_json(system_prompt: str, user_prompt: str, schema: dict,
                  schema_name: str = "script", temperature: float = None,
                  debug: bool = True, history: list = None) -> dict:
    """system + user + スキーマ を渡して JSON を得る汎用呼び出し。

    Gemini は response_format(json_schema)、Ollama(Gemma) は構造化出力非対応なので
    extra_body でコンテキストサイズだけ調整する。

    temperature を None にすると **kwarg ごと送らない**。統合前の Shorts 側は
    temperature を指定していなかったので、既定の None で当時と同じリクエストになる。

    history は [{"user": ..., "assistant": ...}, ...] を古い順に。配信で会話を
    つなげるために使う。**既定の None なら messages は従来どおり [system, user] の
    2要素**なので、Shorts 側の呼び出しは何も変わらない。
    Gemini は OpenAI 互換エンドポイント越しなので chat セッションという概念は無く、
    毎回 messages を組み立て直すのが唯一の渡し方になる。
    """
    extra: dict = {}
    if USE_LOCAL_LLM:
        extra["extra_body"] = {
            "options": {"num_ctx": int(os.getenv("LOCAL_LLM_CTX", "8192"))},
            "think": False,
        }
    else:
        # Gemini の OpenAI 互換エンドポイントは response_format で渡す
        # （extra_body に response_schema を入れると400になる）
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        }
    if temperature is not None:
        extra["temperature"] = temperature

    messages = [{"role": "system", "content": system_prompt}]
    # 壊れたターンで配信を止めない。片方でも欠けていれば黙って飛ばす
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue
        said = (turn.get("user") or "").strip()
        answered = (turn.get("assistant") or "").strip()
        if not said or not answered:
            continue
        messages.append({"role": "user",      "content": said})
        messages.append({"role": "assistant", "content": answered})
    messages.append({"role": "user", "content": user_prompt})

    response = create(messages=messages, **extra)
    if debug:
        print(f"[DEBUG] finish_reason: {response.choices[0].finish_reason}")
    return parse_json(response.choices[0].message.content.strip())


def retry(label: str, fn, *args, attempts: int = 3, catch=(Exception,),
          delay: float = 0, **kwargs):
    """最大 attempts 回リトライする共通ヘルパー"""
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except catch as e:
            if attempt == attempts:
                raise
            print(f"[{label}] 試行{attempt}失敗、リトライします... ({e})")
            if delay > 0:
                time.sleep(delay)
