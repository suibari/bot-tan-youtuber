"""Google 検索グラウンディング。**配信でコメントに聞かれたことを調べるためだけに使う。**

## なぜ common/llm.py（OpenAI 互換）と別建てなのか

`common/llm.py` が使う Gemini の OpenAI 互換エンドポイントでは検索が使えない。
`tools:[{"google_search":{}}]` を渡すと `Unknown name 'google_search'` で 400 になり、
公式が案内する `extra_body.google.tools` は画像モデル専用と明記されている。
検索を使えるのは native の `:generateContent` だけなので、ここだけ `requests` で直接叩く。
`google-genai` SDK は入れていない（この1ファイルのために依存を増やす価値がない）。

## なぜ返答生成そのものに検索を載せないのか

**構造化出力（responseJsonSchema / responseFormat）を付けると、検索がエラーにならずに
黙って無効化される。** 2026-08-25 の実測（gemini-3.5-flash-lite / gemini-3.5-flash）:

  構造化なし + googleSearch → webSearchQueries が返り、groundingChunks も付く
  構造化あり + googleSearch → webSearchQueries が None、内容は推測（学習データ由来）

返答は `live/llm.REPLY_SCHEMA` で日英ペアを作らせている以上、構造化出力は外せない。
そこで **調べものだけを素のテキストで先に済ませ、結果を材料としてプロンプトに載せる**。
返答生成は従来どおり OpenAI 互換のまま何も変えない。

## なぜ「構造化出力を外して、プロンプトでJSONを指示する」ではだめなのか

それだと1回の呼び出しで済むが、**検索が走らなくなる。** 2026-08-25 の実測:

  system  30字（JSON指示なし）        → flash-lite 検索走る / flash 検索走る
  system 146字（JSON指示あり）        → flash-lite 走らない / flash 走る（7.7秒）
  ペルソナ全部 9,550字（JSON指示あり）→ flash-lite 走らない / flash 走らない（7.0秒）

「必ず検索してから答えること」とプロンプトで促しても、本物のペルソナ＋コメントの
8ケースすべてで検索は走らず（0/8）、回答はモデルの記憶からの推測になった。
出力形式の指示が長いほど検索を使わなくなり、`live/persona.py` の 9,000字超の
ペルソナはその極端な例になっている。さらに検索ツール付きの長いプロンプトは
45秒を超えて応答しないことがあり、配信のメインループには載せられない。

つまり **調べものを別の呼び出しに分けるのは最適化ではなく、成立させる唯一の形**。

## リクエスト数

コメント1件につき1回増える。ただし増えるのは短いテキスト呼び出しで、検索が
実際に走る（＝グラウンディングとして課金される）のは調べる必要があったときだけ。
さらに `needs_lookup()` が明らかな雑談を呼ぶ前に落とすので、実際の増加は
コメント全件ではなく「何かを聞いているコメント」の数になる。

## 速さ

配信のメインループはこの往復ぶんだけ止まるので、モデルには検索の要否から
判断させて、要らないときは即 `SKIP` を返させる。2026-08-25 の実測
（gemini-3.5-flash-lite / thinkingLevel LOW）:

  雑談・あいさつ・感想・不具合報告 → SKIP  0.7〜1.0秒（検索は走らない）
  事実を聞く質問                   → 事実  1.8〜2.1秒（検索が走る）

gemini-2.5-flash-lite でも試したが、検索したのに本文が空で返ることがあり使えなかった。
Gemini 3 系が要る（`thinkingLevel` も Gemini 3 系専用で、2.5 系に送ると 400 になる）。
"""

import os
import re

import requests

from common.env import env_flag, env_float

# 配信でコメントに聞かれたことを調べるか。**切れば即座に従来の動作へ戻る。**
LIVE_GROUNDING = env_flag("LIVE_GROUNDING", True)
# 調べもの専用のモデル。カンマ区切りで左から順にフォールバックする。
# **GEMINI_MODEL とは分ける。** あちらは Shorts と共用で、こちらは
# 検索が使える Gemini 3 系でなければならない
LIVE_GROUNDING_MODELS = [
    m.strip() for m in
    os.getenv("LIVE_GROUNDING_MODEL", "gemini-3.5-flash-lite").split(",") if m.strip()
]
# 思考の深さ。既定の MEDIUM は配信には遅い（実測で数秒増える）
LIVE_GROUNDING_THINKING = os.getenv("LIVE_GROUNDING_THINKING", "LOW").upper()
# 1回の調べものに許す秒数。ここを過ぎたら諦めて、調べずに返事を作る。
# 実測 0.7〜2.1秒なので、詰まったことを検出するには十分に短くする
LIVE_GROUNDING_TIMEOUT_SEC = env_float("LIVE_GROUNDING_TIMEOUT_SEC", 8.0)

# 「調べる必要があるコメントか」を何で判定するか。
#   llm   … ローカルの ollama に聞く（既定。実測で取りこぼし 0/20・中央値 0.44秒）
#   regex … 下の _ASKING だけで判定する（GPU を一切使わない）
#   off   … 判定せず全コメントを調べる（Gemini へのリクエストがコメント数ぶん増える）
# llm でも ollama に届かなければ regex へ落ちるので、切り替えは緊急用
LIVE_GROUNDING_GATE = os.getenv("LIVE_GROUNDING_GATE", "llm").strip().lower()
# 判定用のローカルモデル。common/ardy.py:75 と同じく OLLAMA_URL は `/v1` を付けない
# （common/llm.py の LOCAL_LLM_URL は OpenAI 互換の `/v1` 付きなので流用できない）
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
LIVE_GROUNDING_GATE_MODEL = os.getenv("LIVE_GROUNDING_GATE_MODEL", "gemma3:4b")
# 判定に許す秒数。超えたら正規表現へ落ちる。実測は温まっていれば 0.44秒、
# 冷えた状態からの読み込み込みで 3.0〜5.9秒（それは warmup() で先に払う）
LIVE_GROUNDING_GATE_TIMEOUT_SEC = env_float("LIVE_GROUNDING_GATE_TIMEOUT_SEC", 5.0)
# 判定モデルを GPU に載せておく時間。配信は1時間で、準備を含めると 20:40〜22:00。
# 短いと配信の途中で降ろされ、次のコメントで読み込み直しの数秒を食う
LIVE_GROUNDING_GATE_KEEPALIVE = os.getenv("LIVE_GROUNDING_GATE_KEEPALIVE", "90m")
# warmup() だけに使う上限。モデルの読み込みぶんを待つ。配信前なので長くてよい
LIVE_GROUNDING_GATE_LOAD_SEC = env_float("LIVE_GROUNDING_GATE_LOAD_SEC", 120.0)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
_OLLAMA_CHAT = "/api/chat"

# ollama を使えないときの保険。**迷ったら調べる側に倒す。**
# 調べ損ねると答えられないが、余分に調べても SKIP が返るだけで実害が小さい。
# これ単体だと「〜知りたい」「〜が気になる」のような疑問語の無い質問を
# 取りこぼす（実測 2/20）ので、ふだんは _gate_ask のほうを使う
_ASKING = re.compile(
    r"[?？]"
    r"|なに|何|なん|だれ|誰|いつ|どこ|どちら|どっち|どれ|どんな|どう|なぜ|なんで|どうして"
    r"|いくら|いくつ|ある\?|教えて|おしえて|知って|しって|分かる|わかる|調べ|しらべ"
    r"|とは|って(?:な|何)|ですか|ますか|かな|かしら|good|what|who|when|where|why|how"
)

# 判定係のプロンプト。**few-shot が要る。**
# 例を付けないと 15/20 まで落ち、しかも外れがすべて「質問なのに NO」だった
# （＝聞かれたのに調べない）。例を4つ足しただけで 19/20・取りこぼし 0 になる。
_GATE_SYSTEM = """あなたは配信のコメントを仕分ける係です。
そのコメントに答えるためにWeb検索が要るかを、YES か NO の一語だけで答えます。

YES … 世の中の事実・最新の情報・固有名詞・数値について知りたがっている。
       疑問符が無くても、知りたがっていれば YES。
NO  … あいさつ、褒め言葉、感想、気持ちの話、雑談、
       VTuber本人やそのSNSの不具合報告（検索しても分からない）。

例:
コメント：こんばんはー！ -> NO
コメント：今日もかわいいね -> NO
コメント：今日仕事つらかった… -> NO
コメント：botたんの日記が出ないんだけど -> NO
コメント：今日の東京の天気どうだった？ -> YES
コメント：ノーベル賞誰がとったの -> YES
コメント：東京タワーの高さしってる -> YES
コメント：今年の夏コミっていつ -> YES
コメント：最近のおすすめアニメ知りたい -> YES"""

# 調べもの係のシステムプロンプト。**botたんの人格は入れない。**
# ここは事実だけを取ってくる係で、口調を作るのは live/persona.py の仕事。
# 人格を混ぜると、事実のはずの行に感想が混ざって返答側が引っ張られる。
_LOOKUP_SYSTEM = """あなたは配信中のVTuberの調べもの係です。視聴者のコメントに答えるために、
必要なら Google 検索をして、事実だけを日本語で簡潔にまとめます。

- 検索しなくても答えられる雑談・あいさつ・感想・気持ちの話には、`SKIP` とだけ返す
- VTuber本人やそのSNSの内部状態（日記が出ない、リプライが来ない、カードが引けない等の
  不具合報告）は検索しても分からないので `SKIP` とだけ返す
- 調べても確かなことが分からなければ `UNKNOWN` とだけ返す
- 答えられるときは、箇条書き2〜3行、各行40文字以内の事実だけ。
  前置き・URL・出典名・感想・呼びかけは書かない"""

# 調べものの結果。status は3つだけ:
#   "facts"   … 事実が取れた（facts に本文、queries に検索クエリ）
#   "unknown" … 調べたが分からなかった（正直に言う材料として使う）
#   "skip"    … 調べる必要が無かった／調べられなかった（プロンプトには何も足さない）
SKIP = "skip"
UNKNOWN = "unknown"
FACTS = "facts"


def _api_key() -> str:
    # import 時ではなく呼ぶたびに読む。テストが環境変数を差し替えられるように
    return os.getenv("GEMINI_API_KEY", "")


def _post(model: str, question: str, timeout: float) -> dict:
    body = {
        "systemInstruction": {"parts": [{"text": _LOOKUP_SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        # urlContext は**渡さない**。視聴者がコメントに貼った URL を開きに行く
        # 導線になり、live/safety.py で弾いている入力側の対策が素通しになる
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            # 事実を取ってくるだけなので振れ幅は要らない
            "temperature": 0.0,
            "maxOutputTokens": 512,
            "thinkingConfig": {"thinkingLevel": LIVE_GROUNDING_THINKING},
        },
    }
    response = requests.post(_ENDPOINT.format(model),
                             headers={"x-goog-api-key": _api_key()},
                             json=body, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def _read(data: dict) -> tuple:
    """レスポンスから本文と検索クエリを取り出す。

    思考の要約が `thought: true` の part として混ざることがあるので落とす。
    混ぜたまま返すと、事実のつもりで思考過程を読み上げることになる。
    """
    candidate = ((data.get("candidates") or [{}])[0]) or {}
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts
                   if isinstance(p, dict) and not p.get("thought")).strip()
    queries = ((candidate.get("groundingMetadata") or {}).get("webSearchQueries")) or []
    return text, list(queries)


# ollama へ届かないことを毎コメント print すると、配信のログが判定の失敗で
# 埋まって肝心の発話が読めなくなる。1回だけ出す
_gate_warned = False


def _gate_ask(text: str, timeout: float = None) -> bool:
    """ローカルの ollama に「調べる必要があるか」を聞く。判定できなければ None。"""
    body = {
        "model": LIVE_GROUNDING_GATE_MODEL,
        "stream": False,
        # 配信のあいだ GPU に載せたままにする。降ろされると次のコメントで
        # 読み込み直しの数秒を食い、そのぶん反応が遅れる
        "keep_alive": LIVE_GROUNDING_GATE_KEEPALIVE,
        "messages": [
            {"role": "system", "content": _GATE_SYSTEM},
            {"role": "user", "content": f"コメント：{text} ->"},
        ],
        # YES / NO の一語しか要らない。num_predict を絞らないと、
        # 理由を喋り始めて判定が1秒を超える
        "options": {"temperature": 0, "num_predict": 4, "num_ctx": 2048},
        "think": False,
    }
    response = requests.post(
        OLLAMA_URL + _OLLAMA_CHAT, json=body,
        timeout=LIVE_GROUNDING_GATE_TIMEOUT_SEC if timeout is None else timeout)
    response.raise_for_status()
    answer = ((response.json().get("message") or {}).get("content") or "").strip().upper()
    if answer.startswith("YES"):
        return True
    if answer.startswith("NO"):
        return False
    return None


def needs_lookup(comment_text: str) -> bool:
    """このコメントは何かを聞いているか。**Gemini を叩く前に、ここで落とす。**

    配信のコメントは「こんばんはー」「かわいい」のような、調べようのないものが
    多数を占める。それを毎回 Gemini に見せて SKIP と言わせるのは、1件につき
    1リクエストと約1秒を捨てているのと同じ。ここはローカルの ollama なので、
    何回呼んでも API のリクエスト数も課金も増えない。

    判定できなければ正規表現へ落ちる。**ollama が落ちていても配信は続ける。**
    """
    global _gate_warned
    text = (comment_text or "").strip()
    # LIVE_GROUNDING を切ったら判定ごと止める。lookup() が SKIP を返すので
    # ここで調べると答えても、ollama を1回叩くだけ無駄になる
    if not text or not LIVE_GROUNDING:
        return False
    if LIVE_GROUNDING_GATE == "off":
        return True
    if LIVE_GROUNDING_GATE == "llm":
        try:
            answer = _gate_ask(text)
        except Exception as e:
            answer = None
            if not _gate_warned:
                _gate_warned = True
                print(f"[調べもの] 判定に ollama を使えません、"
                      f"以降は語句で判定します: {e}")
        if answer is not None:
            return answer
    return bool(_ASKING.search(text))


def warmup() -> None:
    """判定モデルを先に GPU へ載せておく。**配信が始まる前に呼ぶこと。**

    冷えた状態からの初回は 3.0〜5.9秒かかり、**読み込んだ直後の数件も 2.8〜3.0秒**
    かかる（GPU が落ち着くまでの過渡）。温まりきれば 0.45秒で安定する。
    この差はまるごと「配信が始まった直後のコメントへの反応の遅さ」になるので、
    **2発撃って過渡を配信前に済ませる。**

    失敗しても何もしない（needs_lookup が正規表現へ落ちるだけ）。

    **ここだけ待ち時間を長く取る。** LIVE_GROUNDING_GATE_TIMEOUT_SEC は
    温まったあとの1件ぶんの上限で、読み込みには足りない。配信前なので待ってよい。
    """
    if not LIVE_GROUNDING or LIVE_GROUNDING_GATE != "llm":
        return
    try:
        for _ in range(2):
            _gate_ask("ウォームアップ", timeout=LIVE_GROUNDING_GATE_LOAD_SEC)
        print(f"[調べもの] 判定モデルを読み込みました: {LIVE_GROUNDING_GATE_MODEL}")
    except Exception as e:
        print(f"[調べもの] 判定モデルを読み込めません（語句で判定します）: {e}")


def lookup(question: str) -> dict:
    """コメントに答えるための事実を調べる。**例外は投げない。**

    調べられなくても配信は続ける。返り値は必ず dict で、status は SKIP /
    UNKNOWN / FACTS のどれか。呼ぶ側は status だけ見ればよい。
    """
    empty = {"status": SKIP, "facts": "", "queries": []}
    question = (question or "").strip()
    if not question or not LIVE_GROUNDING or not _api_key():
        return empty

    last_exc = None
    for model in LIVE_GROUNDING_MODELS:
        try:
            data = _post(model, question, LIVE_GROUNDING_TIMEOUT_SEC)
        except Exception as e:
            last_exc = e
            # 1モデルにつき1回だけ。配信中にここで粘っても、そのぶん
            # コメントへの反応が遅れるだけで得るものがない
            print(f"[調べもの] {model} で引けません、次のモデルへ移行します ({e})")
            continue
        text, queries = _read(data)
        if not text:
            # 検索は走ったのに本文が空、という応答が実際にある（2.5 系で頻発）
            print(f"[調べもの] {model} が空を返しました、調べずに返事を作ります")
            return empty
        head = text.strip().upper()
        if head.startswith("SKIP"):
            return {"status": SKIP, "facts": "", "queries": queries}
        if head.startswith("UNKNOWN"):
            return {"status": UNKNOWN, "facts": "", "queries": queries}
        return {"status": FACTS, "facts": text, "queries": queries}

    if last_exc is not None:
        print(f"[調べもの] 調べられませんでした（返事は続けます）: {last_exc}")
    return empty
