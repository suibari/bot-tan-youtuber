"""LLM 呼び出し。Ollama（ローカル）、または Gemini の OpenAI 互換エンドポイント。

モデルは LLM_MODELS の左から順に attempts_per_model 回ずつ試し、
400系（リクエスト不正）は即時再送しても意味がないので1回で失敗させる。

以前は shorts/core.py と live/llm.py に同じ実装が2つあった。

## ローカルは OpenAI 互換ではなくネイティブ /api/chat を使う

`/v1/chat/completions` では `think` を渡す手段が無い。`extra_body` に `options` を
入れても **Ollama は黙って無視する**（400 にもならない）。思考するモデルは reasoning を
別に吐き、そのぶんが生成上限を食うので、`think` を切らないと短い出力（分類など）が
content 空のまま返る。bsky-affirmative-bot の移行（commit 020ae31）が実機で潰した罠。

**num_ctx はネイティブでも送らない**（2026-09-03〜）。サーバの OLLAMA_CONTEXT_LENGTH が
唯一の源で、送らないクライアントは全員そこに乗る。詳細は OLLAMA_NUM_CTX の定数コメント。
なおサーバ側の設定が消えると Ollama 既定の 4096 に落ち、`live/persona.py` のペルソナだけで
9,000字を超えるので**応答が空文字で返る**（エラーにならないので気付けない）。

## 構造化出力のキーは**アルファベット順**で生成される

Ollama の `format` は llama.cpp の文法へ変換されるが、その過程で**オブジェクトの
プロパティ名がソートされる**。スキーマに書いた順ではない。実測:

    スキーマ {zebra, apple, mango} → 出力 {"apple":…, "mango":…, "zebra":…}

Gemini は宣言順（propertyOrdering）を尊重するので、ここは経路で**振る舞いが違う**。

これは見た目の問題では済まない。LLM は前から順に書くので、**キーの順序が
そのまま思考の順序になる**。実際、朝のクイズの台本
（question_intro → answer_reveal → explanation → affirmation の順で書かせたい）は、
先頭に来た affirmation に台本が丸ごと入り、他が空で返った。

**スキーマを新しく足すときは `sorted(properties)` を見て、その順で書かされても
成立するプロンプトにすること。** 順序に意味があるなら、プロンプト側で
「キーはアルファベット順に出力される」と明示して担当範囲を釘刺しする。
"""

import json
import os
import re
import time
from types import SimpleNamespace

import requests
from openai import OpenAI, BadRequestError

from common.env import env_flag, env_float

USE_LOCAL_LLM   = env_flag("USE_LOCAL_LLM")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# ネイティブ API のルート。`/v1` は付けない（common/ardy.py・common/grounding.py と同じ）
OLLAMA_URL      = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/").removesuffix("/v1")
# **bsky-affirmative-bot と同じモデルにすること。** 同じ ollama を共用しており、
# ずらすと runner が2つ立って 16GB に収まらない（下の num_ctx の注意と同じ壊れ方）。
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL",
                            "hf.co/unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL")
# カンマ区切りで複数指定でき、左から順にフォールバックする
GEMINI_MODELS = [
    m.strip() for m in os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite").split(",") if m.strip()
]

# サーバ側 OLLAMA_CONTEXT_LENGTH のミラー。**リクエストには載せない。**
#
# **Ollama は num_ctx が違うと同じモデルでも runner を作り直す。** 2026-09-01 の
# 配信では live の 16384 と別ホストの 32768 が交互に来て、11GB のモデルを数秒おきに
# SSD から再ロードした。I/O wait が約30%まで上がり、ARDY・VOICEVOX・Unity の
# ローカル HTTP がすべてタイムアウトした。2026-09-02 の実測では load_tensors が
# 1時間に114回起き、ARDY の生成が300秒でタイムアウトして朝夜の Shorts から
# モーションが消えた。
#
# 以前は「全経路で同じ値を送って揃える」方式で、setup/ollama-override.conf と
# bsky-affirmative-bot の3箇所を人手で合わせていた。いまは
# **どこからも送らない**（2026-09-03）。サーバの OLLAMA_CONTEXT_LENGTH が唯一の源で、
# 送らないクライアントは全員そこに乗るので、揃え忘れが起こりようがない。
# 送る側が1つでも残っていると、サーバ側だけ下げたときにそこだけ古い値を要求して
# runner を往復させる（これがいちばん原因を読みにくい壊れ方）。
#
# 既定の 4096 ではペルソナだけで溢れて応答が空文字になる（上のモジュール docstring）
# ので、サーバ側の設定が消えていないかだけは気にすること。
# この定数は起動ログの表示と、値が食い違っていないかの目視確認にだけ使う。
OLLAMA_NUM_CTX = 32768

# 1リクエストの上限[秒]。指定しないと OpenAI SDK の既定 600秒 が効き、
# さらに SDK 内部で2回リトライするので、下の create() の 3回 × モデル数 と
# 掛け合わさって理論上数十分ぶん待つことになる。配信ではメインループが
# そのまま止まってコメントに反応できなくなるので必ず縛る。
# 実測は gemini-2.5-flash の構造化出力（2〜3文）で約2秒。
# 録画パイプラインは待てるので、必要なら LLM_TIMEOUT_SEC で伸ばす
LLM_TIMEOUT_SEC = env_float("LLM_TIMEOUT_SEC", 20.0)


class OllamaBadRequest(Exception):
    """Ollama が 400 を返した。プロンプトかスキーマの問題なので投げ直さない。"""


# 400系はどちらの経路でも再試行しない
_NO_RETRY = (BadRequestError, OllamaBadRequest)

if USE_LOCAL_LLM:
    # Gemini 用のクライアントは作らない（API キーが無くても動くようにする）。
    # ただしテストが差し替えられるよう属性は残す
    client = None
    LLM_MODELS = [LOCAL_LLM_MODEL]
    LLM_MODEL = LOCAL_LLM_MODEL
    # num_ctx は送らない。ここに出すのはサーバ既定と食い違っていないかの目視確認用。
    print(f"[LLM] Ollama ({LLM_MODEL}) を使用します "
          f"(num_ctx はサーバ既定に従う。想定 {OLLAMA_NUM_CTX})")
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


def _response(content: str, finish_reason: str):
    """OpenAI SDK と同じ形に包む。

    呼び出し側（shorts/pipeline.py の _llm_create など）は
    `response.choices[0].message.content` と `.finish_reason` を読む。
    ローカル経路のためだけに呼び出し側を書き換えたくないので、ここで形を合わせる。
    """
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)])


def _ollama_options(kwargs: dict) -> dict:
    """OpenAI 互換の引数を Ollama の options へ写す。"""
    # num_ctx は送らない。サーバの OLLAMA_CONTEXT_LENGTH が唯一の源（上の定数コメント参照）。
    options = {}
    if kwargs.get("temperature") is not None:
        options["temperature"] = kwargs["temperature"]
    if kwargs.get("max_tokens") is not None:
        options["num_predict"] = kwargs["max_tokens"]
    if kwargs.get("top_p") is not None:
        options["top_p"] = kwargs["top_p"]
    if kwargs.get("stop") is not None:
        options["stop"] = kwargs["stop"]
    return options


def _ollama_format(kwargs: dict):
    """response_format(json_schema) を Ollama の format へ写す。

    Ollama は format に JSON Schema をそのまま渡すと文法拘束で生成させる。
    「Gemma は構造化出力に対応していない」わけではなく、OpenAI 互換経由で
    渡せなかっただけ。既存スキーマ（SCRIPT_SCHEMA / REPLY_SCHEMA）は
    型名が小文字なのでそのまま通る。
    """
    fmt = kwargs.get("response_format")
    if not isinstance(fmt, dict):
        return None
    return (fmt.get("json_schema") or {}).get("schema")


def _call_ollama(model: str, **kwargs):
    body = {
        "model": model,
        "messages": kwargs.get("messages") or [],
        "stream": False,
        # 思考を切る。切らないと reasoning が生成上限を食い、content が空で返る
        "think": False,
        "options": _ollama_options(kwargs),
    }
    schema = _ollama_format(kwargs)
    if schema is not None:
        body["format"] = schema

    response = requests.post(f"{OLLAMA_URL}/api/chat", json=body,
                             timeout=LLM_TIMEOUT_SEC)
    if response.status_code == 400:
        raise OllamaBadRequest(response.text[:300])
    response.raise_for_status()
    data = response.json()
    content = ((data.get("message") or {}).get("content") or "").strip()
    return _response(content, data.get("done_reason") or "stop")


def _call_gemini(model: str, **kwargs):
    # client は import 時に決まるが、参照はここで行う（テストが差し替えるため）
    return client.chat.completions.create(model=model, **kwargs)


def create(attempts_per_model: int = 3, **kwargs):
    """LLM_MODELS を左から順に attempts_per_model 回ずつ試す。

    SDK 側のリトライは切ってある（max_retries=0）。ここで数える回数が
    そのまま実際の試行回数なので、最悪の待ち時間は
    LLM_TIMEOUT_SEC × attempts_per_model × len(LLM_MODELS) に
    LLM_RETRY_BACKOFF_SEC の合計を足したもので見積もれる。
    """
    call = _call_ollama if USE_LOCAL_LLM else _call_gemini
    last_exc = None
    for model in LLM_MODELS:
        for attempt in range(1, attempts_per_model + 1):
            try:
                return call(model, **kwargs)
            except _NO_RETRY:
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

    スキーマは経路によらず response_format(json_schema) の形で渡す。
    Gemini はそのまま OpenAI 互換で受け、Ollama は create() 側で format へ写す
    （extra_body に response_schema を入れると Gemini は400になるので使わない）。

    temperature を None にすると **kwarg ごと送らない**。統合前の Shorts 側は
    temperature を指定していなかったので、既定の None で当時と同じリクエストになる。

    history は [{"user": ..., "assistant": ...}, ...] を古い順に。配信で会話を
    つなげるために使う。**既定の None なら messages は従来どおり [system, user] の
    2要素**なので、Shorts 側の呼び出しは何も変わらない。
    Gemini は OpenAI 互換エンドポイント越しなので chat セッションという概念は無く、
    毎回 messages を組み立て直すのが唯一の渡し方になる。
    """
    extra: dict = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema},
        }
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
