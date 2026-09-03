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

## いまは配信から呼んでいない（LIVE_GROUNDING の既定は False）

**配信のホットパスから Gemini を外した。** 外の世界のことを聞かれたら、その場で
調べるのをやめて `bot_memory_research_jobs` へ積み、biorhythm_server の
botMemoryResearchWorker に **SearXNG（自前）** で調べさせる。次に同じ話題が来た
ときには `source_type='web_research'` として記憶から引ける（`live/recall.py`）。

その場では「知らない」と正直に答える。これは Bluesky / Nagi のリプライ経路が
前からやっていることで（bsky-affirmative-bot の botMemoryResearchWorker.ts の
docstring を参照）、配信だけが輪から外れて同期で Gemini を叩いていた。

このファイルを残してあるのは、**ゲート（`classify`）を使い続けるから**と、
下に書いた実測（構造化出力と検索は同居できない）を失わないため。
`LIVE_GROUNDING=true` を置けば `lookup()` はそのまま動く。

## リクエスト数

`lookup()` はコメント1件につき1回増える。ただし増えるのは短いテキスト呼び出しで、
検索が実際に走る（＝グラウンディングとして課金される）のは調べる必要があった
ときだけ。さらに `classify()` が明らかな雑談を呼ぶ前に落とすので、実際の増加は
コメント全件ではなく「何かを聞いているコメント」の数になる。

`classify()` はローカルの ollama なので、何回呼んでも課金は増えない。

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
from common.llm import LOCAL_LLM_MODEL, OLLAMA_URL

# 配信でコメントに聞かれたことを Gemini で調べるか。**既定は False。**
# 外のことは非同期の調査キューへ回す（モジュール冒頭の説明を参照）。
# true を置けば従来どおり同期で調べる動作に戻る。
LIVE_GROUNDING = env_flag("LIVE_GROUNDING", False)
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
# 判定用のローカルモデル。**返答生成と同じモデルを使うこと。**
#
# 以前は専用に gemma3:4b を指していたが、返答生成が Gemma 4 26B（13.1GB）に
# 移ったことで、4b を別に載せると 16GB の VRAM に収まらなくなった。実際 2026-08-30 の
# 配信では 503 で載らず、判定は正規表現へ降格していた。
# モデルを揃えれば ollama は同じ runner を使い回すので、追加の VRAM は要らない。
LIVE_GROUNDING_GATE_MODEL = os.getenv("LIVE_GROUNDING_GATE_MODEL", LOCAL_LLM_MODEL)
# 判定に許す秒数。超えたら正規表現へ落ちる。
#
# 26B に寄せたあとの実測（アイドル時）: 0.37〜0.47秒・取りこぼし 0/8。
# 専用の gemma3:4b（中央値 0.44秒）より速い。読み込みは warmup() で先に払う（実測16秒）。
# **CPU が飽和しているとここを超える。** 収録パイプラインの Unity は Xvfb（llvmpipe＝
# ソフトウェア描画）で CPU を食い切るため、その最中の判定は軒並みタイムアウトした
# （load average 9.6 のとき 4/8 が 5秒超）。配信の Unity は :99 の実GPU なので
# 条件は違うが、OBS の x264 と CPU 版 VOICEVOX が同時に走る。配信で実測すること
LIVE_GROUNDING_GATE_TIMEOUT_SEC = env_float("LIVE_GROUNDING_GATE_TIMEOUT_SEC", 5.0)
# 判定モデルを GPU に載せておく時間。**空にすると keep_alive を送らない。**
#
# 判定モデルが返答生成と同じ共有 runner になったので、ここで keep_alive を送ると
# ollama.service の OLLAMA_KEEP_ALIVE=-1（常駐）を上書きしてしまい、
# 同じ ollama を使う他のサービスまで巻き込んで降ろされる。既定は送らない。
LIVE_GROUNDING_GATE_KEEPALIVE = os.getenv("LIVE_GROUNDING_GATE_KEEPALIVE", "")
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

# 「botたん自身のことを聞いている」の保険。**_ASKING と同時に立ったときだけ効く。**
# これ単体では「今日」「最近」のような語で雑談まで拾ってしまう。
# ollama が落ちているときしか通らない道なので、精度より落ちないことを優先する。
_SELF_ASKING = re.compile(
    # botたん自身か、botたんのものを名指ししている
    r"あなた|きみ|君|botたん|ボットたん|ぼっとたん"
    r"|Nagi|nagi|なぎ|Bluesky|bluesky|ブルースカイ|ブルスカ|日記|カード|配信"
    # 「いつ」＋「何かをしていた」。**時を表す語だけでは効かせない。**
    # 「今日の東京の天気どうだった？」まで自分のことになってしまう
    r"|(?:最近|今日|きょう|昨日|きのう|一昨日|おととい|この前|前に)"
    r"[^。！？!?]{0,12}(?:して|やって|やった|読ん|見て|聴い|遊ん|食べ)"
)

# 判定係のプロンプト。**few-shot が要る。**
# 例を付けないと 15/20 まで落ち、しかも外れがすべて「質問なのに NO」だった
# （＝聞かれたのに調べない）。例を4つ足しただけで 19/20・取りこぼし 0 になる。
#
# **調べる語もこの1回で出させる。** 語だけ別に取ろうとすると、形態素解析器を
# 入れるか LLM をもう1回呼ぶかになる。bsky-affirmative-bot のリプライ経路も
# 「生成と同じ1回のリクエストで unknownTerms を申告」で済ませており、それに揃える。
_GATE_SYSTEM = """あなたは配信のコメントを仕分ける係です。
コメントを WEB / SELF / NONE のどれかに分けて、一行だけで答えます。

WEB  … 世の中の事実・最新の情報・作品名・固有名詞・数値について知りたがっている。
        `WEB ` のあとに、調べるべき語をひとつだけ書く（語だけ。文にしない）。
SELF … VTuber本人の過去の出来事について聞いている。
        やったゲーム、読んだ本、昨日していたこと、自分のSNS（NagiやBluesky）で
        見た投稿やもらった反応、前の配信の話。
NONE … あいさつ、褒め言葉、感想、気持ちの話、雑談、
        VTuber本人やそのSNSの不具合報告（調べても分からない）。

例:
コメント：こんばんはー！ -> NONE
コメント：今日もかわいいね -> NONE
コメント：今日仕事つらかった… -> NONE
コメント：botたんの日記が出ないんだけど -> NONE
コメント：最近やったゲームなに？ -> SELF
コメント：Blueskyで今日なんかあった？ -> SELF
コメント：きのうは何してたの -> SELF
コメント：この前の配信で話してたやつなんだっけ -> SELF
コメント：今日の東京の天気どうだった？ -> WEB 東京の天気
コメント：ノーベル賞誰がとったの -> WEB ノーベル賞
コメント：東京タワーの高さしってる -> WEB 東京タワー
コメント：今年の夏コミっていつ -> WEB 夏コミ
コメント：ブルアカやったことある？ -> WEB ブルアカ"""

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

# コメントの仕分け（classify）。**status とは別物なので値を重ねない。**
#   WEB  … 外の世界のこと。調べる語を調査キューへ積む（live/recall.py）
#   SELF … botたん自身の過去のこと。記憶を引く（live/recall.py）
#   NONE … 調べることも思い出すことも要らない
WEB = "web"
SELF = "self"
NONE = "none"


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


def _gate_ask(text: str, timeout: float = None):
    """ローカルの ollama にコメントを仕分けさせる。

    返すのは `(kind, subject)`。判定できなければ None。
    kind は WEB / SELF / NONE、subject は WEB のときだけ入る（調べる語）。
    """
    body = {
        "model": LIVE_GROUNDING_GATE_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": _GATE_SYSTEM},
            {"role": "user", "content": f"コメント：{text} ->"},
        ],
        # 「WEB 東京タワー」の一行しか要らない。num_predict を絞らないと、
        # 理由を喋り始めて判定が1秒を超える。**語を返させるぶんだけ広げてある**
        # （YES/NO だけだった頃は 4）。
        #
        # **num_ctx は送らない。** 以前ここだけ 2048 にしていて、判定のたびに 26B が
        # もう1つロードされていた。ollama は num_ctx が違うと runner を作り直すので、
        # サーバの OLLAMA_CONTEXT_LENGTH に全員で乗る（common/llm.py の定数コメント参照）。
        "options": {"temperature": 0, "num_predict": 24},
        "think": False,
    }
    if LIVE_GROUNDING_GATE_KEEPALIVE:
        body["keep_alive"] = LIVE_GROUNDING_GATE_KEEPALIVE
    response = requests.post(
        OLLAMA_URL + _OLLAMA_CHAT, json=body,
        timeout=LIVE_GROUNDING_GATE_TIMEOUT_SEC if timeout is None else timeout)
    response.raise_for_status()
    answer = ((response.json().get("message") or {}).get("content") or "").strip()
    return _read_gate(answer)


def _read_gate(answer: str):
    """判定係の一行を `(kind, subject)` に。読めなければ None。

    YES / NO も受ける。**モデルが古い言い方に戻ることがある**うえ、
    tests/test_grounding.py の既存ケースがこの形で書かれている。
    """
    head = (answer or "").strip()
    if not head:
        return None
    label, _, rest = head.partition(" ")
    label = label.strip().upper().rstrip(":：")
    subject = rest.strip().strip("「」『』\"'").split("\n")[0].strip()
    if label.startswith("WEB") or label.startswith("YES"):
        return (WEB, subject)
    if label.startswith("SELF"):
        return (SELF, "")
    if label.startswith("NONE") or label.startswith("NO"):
        return (NONE, "")
    return None


def classify(comment_text: str) -> tuple:
    """コメントを仕分ける。返すのは `(kind, subject)`。

    配信のコメントは「こんばんはー」「かわいい」のような、調べようも思い出しようも
    ないものが多数を占める。それを毎回 LAN の外へ出すのは往復を1回捨てているのと
    同じ。ここはローカルの ollama なので、何回呼んでも API のリクエスト数も課金も
    増えない。

    subject（調べる語）が空で返ることはある。**呼ぶ側で補うこと**（`live/recall.py`
    の `subject_of`）。ここは common なので live/topics.py に依存させない。

    判定できなければ正規表現へ落ちる。**ollama が落ちていても配信は続ける。**
    LIVE_GROUNDING は見ない。あれは Gemini を叩くかどうかの旗で、仕分け自体は
    Gemini を使わなくなったいまも要る。
    """
    global _gate_warned
    text = (comment_text or "").strip()
    if not text:
        return (NONE, "")
    if LIVE_GROUNDING_GATE == "off":
        return (WEB, "")
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
    if not _ASKING.search(text):
        return (NONE, "")
    # 何かを聞いてはいる。自分のことを聞いていそうなら記憶へ回す
    return (SELF, "") if _SELF_ASKING.search(text) else (WEB, "")


def needs_lookup(comment_text: str) -> bool:
    """このコメントを Gemini で調べるか。**既定では常に False。**

    `LIVE_GROUNDING` を立てたときだけ、従来どおり WEB 判定で True を返す。
    配信は `classify()` を直に呼ぶので、これは後方互換のための薄い包みでしかない。
    """
    if not LIVE_GROUNDING:
        return False
    return classify(comment_text)[0] == WEB


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
    # LIVE_GROUNDING は見ない。Gemini を使わなくなっても仕分けは走り続けるので、
    # 冷えた初回の3秒は配信前に払っておく必要がある
    if LIVE_GROUNDING_GATE != "llm":
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
