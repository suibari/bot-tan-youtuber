# bot-tan-youtuber

全肯定botたんの YouTube 運用一式。Shorts の自動生成・投稿と、AITuber ライブ配信を
1つのリポジトリで扱う。

| | 中身 | 起動 | 時刻 |
|---|---|---|---|
| `shorts/` | 夜の Shorts（Nagi のポスト紹介）| `run.sh` | 毎日 18:00 JST |
| `shorts/` | 朝の勘違いクイズ Shorts | `run_quiz.sh` | 毎日 06:00 JST |
| `live/` | 当日ライブの配信枠だけを先行作成 | `live/prepare_broadcast.py` | 毎日 04:00 JST |
| `live/` | YouTube ライブ配信（AITuber）| `run_live.sh` | 毎日 20:40 起動 / 21:00–22:00 配信 |
| `common/` | 上記が共有する処理 | — | — |

## ディレクトリ構成

```
common/           両方から呼ばれるもの。ここを直すと両方に効く
  env.py            パス解決（ROOT/DATA_DIR/LOGS_DIR）と環境変数の読み取り
  llm.py            LLM クライアント・モデルのフォールバック・JSON 取得
  voice.py          VOICEVOX 合成・結合・無音・モーラタイミング
  ardy.py           ARDY サーバの起動/待機/停止と .vrma 生成
  motion_safety.py  モーション指示文の禁止語・主語の正規化・待機動作
  db.py             PostgreSQL 接続
  youtube_auth.py   YouTube OAuth（トークンは Shorts と配信で共用）
  xvfb.py           仮想ディスプレイの起動
  notify.py         Discord 通知
shorts/           Shorts の生成と投稿
  core.py           収録・字幕・ffmpeg 仕上げ。common/ の再輸出ファサードでもある
  pipeline.py       夜版 / quiz_pipeline.py 朝版
live/             ライブ配信
  config.py         配信の設定。common/ の再輸出ファサードでもある
  schedule.py       JSTの開始・終了時刻と配信タイトル
  prepare_broadcast.py  当日枠だけを作り共有DBへ保存する4時ジョブ
  live.py           配信のメインループ
tools/            手動で使う道具（プール構築・OBS シーン構築・Unity 単体確認）
setup/            systemd ユニットと Xorg/openbox の設定＋インストーラ
data/             quiz.csv / bgm/ / motions/（生成物）
                  ※ OBS が読む字幕などは SUBTITLE_DIR（リポジトリの外）に書く
logs/             pipeline_* quiz_* live_* ardy_*
.env              **リポジトリのルートに1本だけ**。shorts と live が同じものを読む
```

```
                    YouTube Live
                   ↗ RTMP      ↖ liveChatMessages.list
              [OBS Studio]          │
                   ↑ ウィンドウキャプチャ │
        [Unity 常駐 :99]             │
                   ↑ HTTP :2338      │
                   └──────────┬──────┘
                       [live/live.py]
                              ├→ VOICEVOX      localhost:10101 (speaker=8)
                              ├→ Gemini        OpenAI互換エンドポイント
                              ├→ ARDY          127.0.0.1:2337（非同期）
                              ├→ PostgreSQL    192.168.1.200:5432
                              ├→ biorhythm     localhost:3002
                              └→ $SUBTITLE_DIR/*.txt（OBS が読む字幕・コメント欄）
```

## 関連リポジトリ

| | 役割 |
|---|---|
| `bsky-affirmative-bot` | ペルソナ（`SYSTEM_INSTRUCTION`）・energy・記憶DB の**原典** |
| `bottan-video` | Unity プロジェクト（Shorts の収録用） |
| `bottan-video-dev` | Unity プロジェクト（配信用。`-liveMode` で常駐させる） |
| `text-to-vrma` | ARDY エンジン本体 |

## OBS が読むファイルの置き場

字幕・コメント欄・時計・BGM は **`~/.local/share/bottan-live/`**（リポジトリの外）に置く。

OBS のシーンはファイルを絶対パスで掴むので、これらをリポジトリの中に置くと
「systemd は本番から起動しているのに、OBS は dev のファイルを見ている」状態になる。
統合前に実際にそうなっていて、本番の配信では字幕とコメント欄が出ない状態だった。

```
~/.local/share/bottan-live/
├── obs/     subtitle_ja.txt / subtitle_en.txt / comments.txt / clock.txt
│            energy.html（すべて実行時に書かれる）
└── bgm/     ohirusugi.mp3（data/bgm/ からのコピー。差し替えたら両方直すこと）
```

`.env` の `SUBTITLE_DIR` がここを指す。本番と dev の両方で同じ値にしておくこと。

### energy ゲージ

画面左下の `energy` ブラウザソースは `file://.../obs/energy.html` を読む。
`live/gauge.py` が `energy.get_energy()`（共有DBの `bot_state`）の値で
HTML を書き直し、HTML 側に埋めたスクリプトが
`ENERGY_REFRESH_SEC`（既定30秒）ごとに自分を読み直す。
**ブラウザソースはローカルファイルの変更を自前では監視しない**ので、この
自己リロードが無いと数値が固まったままになる。

数値は**小数第一位まで**出す。配信コメント1件あたりの加算が +0.1% なので、
整数だと視聴者から見て変化が分からない。

色は energy で変わる: 70以上=橙 / 35以上=緑 / それ未満=青。
背景が明るい日でも読めるよう、半透明のパネルを敷いてある。

## セットアップ

### 1. Python

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env      # Shorts と配信で同じ .env を読む。キーは1本にまとまっている
```

### 2. GPU 仮想ディスプレイ（必須）

**Xvfb では配信できない。** Mesa の llvmpipe（CPU 描画）になり、1920x1080 の
URP + VRM では実測 **1.7fps** しか出ない。NVIDIA ドライバでモニタ非接続の
仮想スクリーンを作って GPU に描かせる。

```sh
sudo bash setup/install_xorg.sh
```

既存デスクトップ（GDM が管理する `:0`）には触らない。`/etc/X11/xorg.conf.d/` ではなく
`/etc/X11/xorg-bottan-live.conf` を置き、`Xorg -config` で明示的に読ませている。

確認:
```sh
DISPLAY=:99 glxinfo -B | grep -i renderer   # NVIDIA GeForce ... と出れば成功
```

**`:99` はこのサービスの予約番号。** 他のプロセスに先に取られると Xorg が起動できず、
`Restart=always` で永久にリトライする状態になる（実際に踏んだ）。
`unity_live._start_xvfb` のフォールバック用 Xvfb は 120 番以降から探すようにしてある。

### 3. OBS

**OBS は `:99` 側で起動すること。** X11 のウィンドウキャプチャは
「OBS 自身が接続している X ディスプレイ」の窓しか列挙できない。デスクトップ（`:0`）で
起動した OBS のプルダウンに `:99` の Unity は**絶対に出てこない**。

```sh
DISPLAY=:99 flatpak run --env=DISPLAY=:99 com.obsproject.Studio
```

配信時は `live.py` が `obs.launch()` で自動的にこの形で起動する（`OBS_LAUNCH=false` で抑止）。

**`:99` にはウィンドウマネージャが必須。** OBS の XComposite キャプチャは EWMH 準拠の
WM が居ないと起動時に

```
window manager does not support Extended Window Manager Hints (EWMH).
XComposite capture disabled.
```

と言ってプラグインごと読み込みを諦め、「ウィンドウキャプチャ」という選択肢自体が
消える。`setup/install_xorg.sh` が `bottan-live-wm.service`（openbox）まで入れる。

`obs-websocket`（[ツール]→[WebSocket サーバー設定]）を有効にし、パスワードを
`.env` の `OBS_PASSWORD` に書く。

**配信用の設定は普段使いの OBS と分けてある。** シーンコレクションとプロファイルは
どちらも `bottan-live`（`.env` の `OBS_COLLECTION` / `OBS_PROFILE`）で、`obs.launch()` が
`--collection` / `--profile` で名指しして開く。デスクトップ側で OBS を触って別の
コレクションを開いたままでも、配信は必ず `bottan-live` で始まる。

ただし **OBS を2つ同時に起動しないこと。** 設定ディレクトリは共有なので、
後から終了したほうがシーンのファイルを上書きする。`:99` の様子を見たいときは
OBS をもう一つ起動するのではなく `tools/obs_shot.py` を使う。

**シーンは `tools/build_scene.py` が組む。**

```sh
./venv/bin/python tools/build_scene.py     # 何度でも実行できる（作り直す）
./venv/bin/python tools/obs_shot.py /tmp/now.png   # 出力を PNG で確認
```

配信中にプログラムが触るのは RTMP 設定と配信の開始・停止だけ。

| レイヤ | ソース | 中身 |
|---|---|---|
| 最背面 | ウィンドウキャプチャ (XComposite) | Unity の Game View（`DISPLAY=:99`） |
| 字幕(日) | テキスト (FreeType 2) | `data/obs/subtitle_ja.txt` をファイル読み込み |
| 字幕(英) | テキスト (FreeType 2) | `data/obs/subtitle_en.txt`（日本語より小さめ） |
| コメント欄 | テキスト (FreeType 2) | `data/obs/comments.txt` |
| energy | ブラウザ | `http://localhost:3002/image.png`（biorhythm_server が公開済み） |
| 時計 | テキスト (FreeType 2) | `data/obs/clock.txt` |
| botたんの声 | 音声入力キャプチャ (PulseAudio) | `bottan_live.monitor`（下記） |
| BGM | メディア | `data/bgm/ohirusugi.mp3` をループ（-20dB）+ botたんの声にダッキング |

#### 音声の経路

**Unity の声は OBS のシーンに音声ソースを1本置かないと配信に乗らない。**
BGM は OBS の中（ffmpeg_source）で完結しているので鳴るが、Unity の音は
OS のサウンドサーバを経由する。初回の配信は「BGM は流れるのに声だけ出ない」で
これが原因だった。

```
Unity ──(PULSE_SINK=bottan_live)──▶ bottan_live ──▶ bottan_live.monitor ──▶ OBS
```

配信専用の null シンクを1本使う（`audio.ensure_sink()` が `live.py` から自動で作る。
名前は `.env` の `LIVE_AUDIO_SINK`）。既定の出力をそのまま拾わない理由は2つ:

- PipeWire は実デバイスが1つも無いときだけ `auto_null` を自動生成する。実デバイスが
  現れると消えるので、OBS 側で名前を決め打ちできない
- 既定の出力を拾うと、ブラウザの音や通知音まで配信に乗る

BGM のダッキング（botたんが喋る間だけ BGM を沈める）は、この音声ソースを
サイドチェイン入力にしたコンプレッサーで `tools/build_scene.py` が仕込む。

切り分けは `pactl list short sink-inputs`（Unity がシンクに繋がっているか）と
OBS のログの `pulse-input: Started recording from 'bottan_live.monitor'`。

Linux の OBS にテキスト(GDI+)は無い（Windows 専用）。**テキスト (FreeType 2)** を使う。
テキストソースは**「テキストファイルから読み取る」**に設定すること。Python 側は
一時ファイル→`os.replace` でアトミックに差し替えるので、書き込み途中の
空ファイルを読まれることはない。

#### エンコーダは x264（CPU）固定

`obs.launch()` が起動前にプロファイルの `basic.ini` へ書き込む。**websocket 経由で
書いても効かない**（OBS は「シンプル」出力のエンコーダをプロファイル読み込み時に
一度だけ決めるため）。

既定の NVENC を使うと、この機械では配信開始が失敗する:

```
cuda_ctx_init: ... CUDA_ERROR_OUT_OF_MEMORY (2): out of memory
Already in non_texture encoder, can't fall back further!
Stream output type 'rtmp_output' failed to start!
```

GPU は 8GB しかなく、ARDY・Unity・VOICEVOX（と常駐している ollama）で埋まっていて
NVENC のぶんが残らない。**OBS はここからソフトウェアエンコーダへ落ちてくれない**
ので、はじめから x264 を指定する。1080p30 なら CPU 側に余裕がある。

`obsws` の `StartStream` はリクエストが通っただけで成功を返すので、
`Obs.start_stream()` は実際に `output_active` になるまで確かめる。これを見て
いなかったせいで、YouTube 側の 180秒 タイムアウトまで原因が分からなかった。

### 3.5 背景（フリー素材）

背景は Unity のシーン内 Quad に貼る。配信モードでは `LiveStage.cs` が実行時に
`Assets/background_live.jpg` を読むので、**Unity を開かずに差し替えられる**。

```sh
./venv/bin/python tools/install_background.py ~/ダウンロード/room_night.png \
    --credit "作者名 / 配布元URL"
```

1920x1080 に中央基準で切り抜いて入れる。クレジットは `data/background_credit.txt`
に残るので、要クレジットの素材なら配信の概要欄に転記すること。素材を選ぶときは
**商用利用の可否・加工の可否・クレジット表記の要否**の3点を必ず確認する。

このファイルが無いときは既存の `background_classroom.jpg` のままになる（配信は
止まらない）。

現在使っている素材は `data/background_credit.txt`、BGM は `data/bgm/CREDIT.md` を見ること。

背景の色みに合わせてライトの色も変えられる（既定は電球色）:

| キー | 意味 |
|---|---|
| `LIVE_LIGHT_COLOR` | キーライトの色（`#RRGGBB`）。月明かりの部屋なら寒色に |
| `LIVE_AMBIENT_COLOR` | 環境光の色（`#RRGGBB`） |

構図と明るさは `.env` で振れる:

| キー | 意味 |
|---|---|
| `LIVE_CAMERA_X` | 負にすると botたんが画面の左へ寄る（右にコメント欄の余白ができる） |
| `LIVE_CAMERA_Z` | 大きくすると引きの画になる（既定 0.5） |
| `LIVE_LIGHT_INTENSITY` | 平行光の明るさ（既定 0.85、色は電球色に固定） |
| `LIVE_CHARACTER_YAW` | 体の向きの補正（度）。既定はカメラの方を向く。負にするとさらに画面の内側（コメント欄側）へ振れる |
| `LIVE_MOUTH_CLOSE` | 無音時に表情の口成分を打ち消す強さ 0〜1（既定 1.0） |

**カメラを左へ寄せたら体の向きも直すこと。** カメラを平行移動しても
モデルの向きは +Z のままなので、寄せたぶんだけ「画面の外を向いている」ように
見える（視線だけは LookAtOffset がカメラの子なので追従するが、首から下が
付いてこない）。`LiveStage.AimCharacter()` がルートをヨーだけ回して
カメラを向かせる。`LIVE_CAMERA_X=-0.25 / Z=0.70` なら yaw は -19.7度 になる。

**`LIVE_MOUTH_CLOSE` を渡さないと、喋り終わったあと口が開いたままになる。**
`ApplyEmotion` は常に合計 `_maxEmotionWeight` を5表情に配分するので「無表情」が
存在せず、表情に含まれる口成分がそのまま残る。`VRM1FaceAnimation` 側には
`Fcl_MTH_*` に負のウェイトを与えて口成分だけを差し引く補正が入っている
（朝版パイプラインの `MORNING_MOUTH_CLOSE` と同じ仕組み）が、
`-mouthCloseOnSilence` を渡さない限り無効のままになる。

配信中に追い込むときは Unity を再起動せず HTTP で動かせる:

```sh
curl -X POST 127.0.0.1:2338/camera -d '{"x":-0.25,"y":1.35,"z":0.7}'
```

背景の合わせ直しと体の向きの取り直しも `/camera` の中でやる。

### 4. YouTube

トークンは Shorts パイプラインと共用（`~/.bottan_youtube_token.pickle`）。
スコープに `youtube.force-ssl` が含まれているので追加の認可は要らない。
無い場合は `shorts/youtube_reauth.py` で通しておくこと
（配信は無人なので、実行時に対話フローへは入らない）。

### 5. 自動起動

```sh
sudo bash setup/install_units.sh
```

04:00 に当日21時の配信枠と視聴URLだけを作成し、20:40 の本配信が同じ枠へ
ストリームを紐づける。OBS・Unity・RTMP送出は20:40まで起動しない。
20:40 起動 → 21:00 live → 21:55 クロージング → 22:00 complete。
`Persistent=false` にしてあるので、起動に失敗した日を後から取り返さない
（変な時刻に配信枠が増えるのを防ぐ）。

> **`systemctl start bottan-quiz.service` / `bottan-pipeline.service` は本番そのもの。**
> 通し切ると YouTube に投稿され、クイズは `data/quiz_used.csv` を1問消費する。
> 投稿せずに service 経由で動かしたいなら drop-in で環境変数を渡す:
> ```sh
> sudo systemctl edit bottan-quiz.service   # [Service] Environment=SKIP_YOUTUBE=true
> ```
> 手元で試すだけなら `./run_quiz.sh SKIP_YOUTUBE=true QUIZ_NO_CONSUME=true --preview`。

## 動作確認

```sh
# Unity 単体（HTTP・発話キュー・モーション・表情・時計の追随）
./venv/bin/python tools/test_unity.py

# 一気通貫（YouTube・OBS・ARDY 抜き、偽コメントを流し込む）
./run_live.sh DRY_RUN=true SKIP_ARDY=true FAKE_COMMENTS=data/fake_comments.json

# 会話が続くか（同じ人が話しかけ続ける偽コメント）。前のやりとりを受けた返事に
# なっているか、同じ人の2件目が60秒待たずに返っているかをログで見る
./run_live.sh DRY_RUN=true SKIP_ARDY=true \
              FAKE_COMMENTS=data/fake_comments_conversation.json

# 同上を「いま」から数分で一周させる。LIVE_START_HHMM の既定は 21:00 で、
# 未来だとその時刻まで sleep するので、3つとも近い時刻に上書きすること
S=$(date -d '+5 min' +%H:%M); C=$(date -d '+11 min' +%H:%M); E=$(date -d '+13 min' +%H:%M)
./run_live.sh DRY_RUN=true FAKE_COMMENTS=data/fake_comments.json \
              LIVE_START_HHMM=$S LIVE_CLOSING_HHMM=$C LIVE_END_HHMM=$E

# 本番と同じ経路で限定公開の配信を1本
./run_live.sh LIVE_YOUTUBE_PRIVACY=private
```

Unity の HTTP は `curl` でも叩ける:
```sh
curl -X POST localhost:2338/speak   -d '{"wav_path":"/tmp/a.wav","valence":0.8,"arousal":0.4}'
curl -X POST localhost:2338/motion  -d '{"vrma_path":"/path/to.vrma"}'
curl -X POST localhost:2338/emotion -d '{"valence":-0.5,"arousal":0.2}'
curl localhost:2338/status
```

## 設計上の注意

### 配信は落とさない

コメント1件の処理が失敗しても、LLM が応答しなくても、ARDY が使えなくても配信は続ける。

| 落ちたもの | 動作 |
|---|---|
| ARDY | プールのモーションだけで継続。プールも空なら Animator の Idle |
| LLM | `FALLBACK_LINES` の定型で返す |
| biorhythm_server | 配信側は DB の `bot_state` を読むだけなので影響なし。落ちている間はコメントぶんの加算が溜まり、復帰時にまとめて入る |
| チャット取得 | フリートークで場をつなぐ |
| VOICEVOX | その回の発話を諦めて次へ。Discord に通知 |
| Unity | 配信を終了する（映像が無いので続ける意味がない） |

### コメントへの往復を遅くしない

初回の配信で「コメントを書いてから画面に出るまで10秒、そこから返事まで10秒」に
なっていた。内訳と対処:

| どこ | 時間 | 対処 |
|---|---|---|
| YouTube がコメントを配る | 数秒 | **どうにもならない。** liveChatMessages は `pollingIntervalMillis` を守る義務があり、無視するとクォータを焼き切って配信の途中からコメントが読めなくなる |
| コメント欄への反映 | 最大10秒 | `ChatPoller._accept` が受信と同時に `comments.txt` を書く。以前は10秒ごとの雑務に任せていた |
| 直前の発話が終わるのを待つ | 5〜15秒 | **ここがいちばん効いている。** フリートークは `interruptible=True` で、文と文の切れ目でコメントの有無を見て切り上げる。コメントへの返信は途中で切らない方針なので、ここは残る |
| DB（気分・energy） | 0〜数秒 | `_bot_context()` は `BOT_CONTEXT_TTL_SEC`（既定20秒）キャッシュし、DB は1往復だけ。以前は同じ行を2回引いて接続を2本張っていた |
| LLM | 約2秒 | 実測（gemini-2.5-flash、2〜3文の構造化出力）。`LLM_TIMEOUT_SEC`（既定20秒）で頭を打たせる。会話履歴（`LIVE_HISTORY_TURNS` ぶん）は入力トークンだけを増やす。1ターン100〜150字で、system prompt の約8500字に対して6ターンでも1割ほど |
| VOICEVOX + 送信 | 0.1〜0.7秒 | 全文まとめてではなく**1文ずつ**合成して Unity のキューへ流し込む。喋り出しまで実測 0.12秒 |
| ARDY のモーション生成 | 4.6〜12.8秒 | **待たない。** プールから即座に1本引いて再生し、生成は別スレッドで走らせて次回以降に回す（`live/motion.py`） |

実測は `[live] 反応まで X.X秒 (待ち a / DB b / LLM c / 合成 d)` としてログに出る。
「待ち」がコメント受信から取り出しまで＝ほぼ直前の発話が終わるのを待った時間。

**メインループから DB・LAN を触らないこと。** energy ゲージ・RAG 先読み・記憶の
引き直しは `_start_chores()` の別スレッドで回している。以前は `run_loop` の中で
直接呼んでおり、DB のホスト（`DB_HOST`）が不調だと**コメントが1件も無くても
十数秒止まった**（2026-08-21 の配信。同じホストの 3200/3204 も全滅していた）。
`DB_CONNECT_TIMEOUT` は既定3秒。

`_speak` は1文ごとに「合成 → `/speak` → いまの文が終わる 0.7秒 前まで待つ」を
繰り返す。0.7秒 あれば次の文の合成が間に合う（実測 0.05〜0.7秒）ので音は途切れず、
その待ち時間が割り込みの窓になる。字幕は `SubtitleScheduler.push()` で後から
足していく（発話を始める時点では2文目以降の尺がまだ分からないため）。

### 字幕は obs-websocket で流し込む

OBS の `text_ft2_source_v2` を `from_file` で使うと、**約1秒に1回しか**
ファイルを見に行かない（`video_tick` の中の `stat()`。間隔を指定する
プロパティは無い）。これがそのまま「発話より字幕が遅れる」になる。しかも
`st_mtime` は秒解像度なので、同じ秒に2回差し替えると2回目が丸ごと落ちる。

`Obs.bind_text_sources()` が配信開始時に字幕ソースを探して `from_file` を切り、
`subtitle.set_sink()` で `SetInputSettings` へ流す経路に差し替える。
**シーンを手で直す必要はない**（終了時に `restore_text_files()` で戻す）。
ソースは名前ではなく `text_file` が `SUBTITLE_JA` / `SUBTITLE_EN` を指しているかで
見つけるので、手で組んだシーンでも動く。

ファイルへの書き込みは保険として残してあり、websocket が落ちたら
（`set_text` が3回続けて失敗したら）自動でファイル読み込みへ戻す。
音声は PulseAudio の null sink 経由で OBS に入るぶん遅れるので、
ずれが気になるときは `SUBTITLE_LEAD_SEC` で前後に振る。

文と文の間の 0.25秒 の無音は `voice.synthesize_lines(tail_gap=...)` が作る。
1文ずつ別々に送るので、こちらで作らないと詰まって聞こえる。

**コメント欄は配信の始めに空にすること**（`subtitle.clear_comments()`、`prepare()` で呼ぶ）。
`comments.txt` はファイルに残り続けるので、消さないと配信開始から最初のコメントが
来るまで前回配信のコメントが並んだままになる。`clear()` は字幕（ja/en）だけを消す
（発話のたびに呼ばれるので、ここでコメント欄まで消してはいけない）。

### 会話をつなぐ

**コメント1件＝独立したLLM呼び出し、ではない。** 配信中のやりとりは
`live/conversation.py` の `ConversationLog` に1本の時系列として積み、
`common/llm.py:generate_json(history=...)` が `messages` のマルチターン
（`user` / `assistant` の交互）に展開して渡す。Gemini は OpenAI 互換
エンドポイント越しなので chat セッションは無く、毎回 messages を組み立て直す形になる。

- **フリートークとオープニング・クロージングも同じ時系列に積む。**
  積まないと「さっき自分が何を話していたか」が飛ぶ
- `assistant` に入れるのは読み上げた日本語だけ。返答のJSON全体を積むと
  `en` / `motion_en` / valence / arousal のぶんトークンが数倍になる。出力形式は
  毎回 `response_format` で強制されるので、履歴が平文でも崩れない
- `user` に入れるのも「送り主：/ 内容：」の短い形。プロンプト本文まるごとを積むと、
  botの状態ブロックと記憶ブロックがターン数ぶん重複する
- 場の流れから押し出されたぶんも含めて、**いま返す相手とのやりとり**だけは
  本文に「## この人とさっきまで話していたこと」として添える。
  流れに残っているターンは二重にならないよう取り除く（`live.py:reply_to_comment`）
- 履歴は**当日の配信内だけ**のメモリ。DB へは書かない（`memory.save_comment()` が
  既に `bottan_live.comments` へ書いている）

統合前は botたん自身の発話を8件持つだけで、**視聴者が何を言ったかはどこにも
残っていなかった**。しかもそれを「## 直前に自分が話したこと（同じ言い回しを
繰り返さないこと）」という*逆向き*の制約で渡していたので、話を続けるどころか
話題を逸らす方向に効いていた。「一つ前の会話を忘れている」の正体がこれ。
いまはコメント返信からこのブロックを外し、フリートーク（同じネタの繰り返し防止が
主目的）にだけ残している。

### 同一ユーザーのクールダウンは条件付き

`COMMENT_USER_COOLDOWN_SEC`（既定60秒）は「1人が喋り続けて他の視聴者のコメントが
読まれなくなる」のを防ぐためのもので、**他に返事できるコメントが無いときは無視する**
（`chat.CommentQueue._next_index`）。待っているのがその人だけなのに60秒黙って
フリートークへ流れると、1対1で話しかけられている状況で会話が続かない。

`has_pending()` も同じ判定を通るので、フリートークはその人のコメントで切り上がる。

### ペルソナは原典のコピーで、放っておくとドリフトする

`live/persona.py` の `_CHARACTER_TEMPLATE` は
`bsky-affirmative-bot/packages/shared-configs/src/config/index.ts` の
`SYSTEM_INSTRUCTION` のコピー（語彙リストだけ実行時に読む）。原典は活発に更新されるので、
**触るときは必ず原典と差分を取ること。** docstring に同期時点のコミットを書いてある。

実際、統合時のコピーには「# 住んでいる場所（Nagi と Bluesky）」が無く、
Nagi が SNS の名前だと書いていないのに、プロンプト側は「Nagiで見かけた投稿」を
渡していた。結果、配信で「Nagiさん」と人名のように呼んでいた。

### モーションを途切れさせない

`/motion` を投げるのは **`idle.IdleAnimator` だけ**。送出者を1つにしておかないと、
待機スレッドが投げた直後に発話側が投げたとき、Unity（`VrmaMotionPlayer.EnqueueMotion`）が
フェードイン途中＝重み1未満のクリップを打ち切る。合成後の重み `Min(1, wa+wb)` が
1に届かず Idle が一瞬混ざって、ポーズが跳ねて見えていた。

- **発話中**（`speak_begin` 〜 `speak_end`）: 「クリップの尺 − `VRMA_CHUNK_OVERLAP`(0.5秒)」
  ごとに次を投げ、常に2本が重なった状態を保つ。録画パイプラインが
  時刻表を事前計算して 0.5秒 重ねて置いているのと同じことを、尺が先に分からない
  配信では時間で刻んでやっている。表情は `_speak` が決めるのでここでは振らない
- **待機中**: 6〜20秒おきにプールのモーションを1本（落ち着いたカテゴリを厚めに抽選）。
  1本が約9秒なので、下限が9秒だと待機時間の4割が Animator の Idle（棒立ち）になる
- **待機中の表情**: 3〜8秒おきに動かす。1/4 の確率で「ほほえみパルス」
  （valence を 0.85〜0.95 まで上げて `IDLE_SMILE_HOLD_SEC` 保持してから戻す）、
  1/10 で「はっと顔」（arousal を上げる）、残りは従来のランダムウォーク。
  直前の発話の感情を素の顔として引き継ぐ

  Unity が受け取れるのは valence/arousal の2軸だけで、**ウィンクのような個別の
  表情は指定できない**（`LiveController` に `/expression` が無い）。表情の
  バリエーションを作れるのは値の動かし方だけなので、はっきり分かる山を混ぜている。
  ウィンクを入れるなら Unity 側に手を入れること（VRM には `blinkLeft`/`blinkRight`
  プリセットも `Fcl_EYE_Close_L/R` モーフもある）

`IDLE_ENABLED=false` で止まるのは待機中のぶんだけで、発話中は動く。

生成モーション1本は **ARDY のセグメント連結**で作る（`LIVE_MOTION_SEGMENTS` ×
`ARDY_LIVE_DURATION` ＝ 既定 3×3.0秒 ≒ 9秒、つなぎ目は `ARDY_BLEND_SEC`=0.7秒 の
クロスフェード）。3秒の単発だと発話中の継ぎ足しが 2.5秒 に1回になり、
Unity が `/motion` を受けるたびにメインスレッドで `.vrma` をパースする
（`VrmaMotionPlayer.LoadVrma`）ぶんフレームが落ちやすい。尺は
`data/motions/<cat>/<hash>.json` に残す（無い古いクリップは3秒とみなす）。

### 生成モーションの見た目は Shorts と共通

`common/vrma_style.py` の `vrma_unity_args()` が返す `-vrmaSmooth` などを、
録画（`shorts/pipeline.py`）と配信（`live/unity_live.py`）の**両方**が Unity へ渡す。
渡さないと `VrmaMotionPlayer` は「改修前の見た目」の既定値で動き、
素の ARDY 出力がそのまま出る。統合前の配信は1つも渡しておらず、Unity のログが

```
[Vrma] keepIdleHands=False elbowBend=0deg wristBend=0deg headTilt=0deg smooth=0s
```

になっていた（`-vrmaSmooth 0.10` は実測でカクつき −64%）。配信だけ変えたいときは
`LIVE_VRMA_SMOOTH` のように `LIVE_` を頭に付けた環境変数を置く。

### モーションの安全化は緩めない

`safety.BANNED_MOTION_RE` は実測で事故った履歴に基づく。しゃがむ・跳ぶ系は
ARDY が予備動作として「膝を深く曲げて脚を大きく開くしゃがみ」を必ず作り、
スカート＋腰高カメラで下着が映る（2026-08-11 に実際に発生）。
拍手は ARDY が描けず手が震えて見える。プロンプトでも禁止しているが
**LLM は普通に破る**ので、コード側が最後の砦になっている。

### Unity の時刻源

配信モードでは `Time.time` を使ってはいけない。`Time.deltaTime` の累積で、
`maximumDeltaTime`（既定0.333秒）にクランプされるため、フレームが飛ぶと
実時間から遅れる（実測で実時間7秒に対して5.0秒）。音声は実時間で再生されるので、
モーションだけ遅れて口と動きがズレる。`LiveMode.Now`（`Time.realtimeSinceStartup`）を使うこと。

### ロック

`run_live.sh` は `/tmp/bottan-render.lock` を取る。録画パイプラインの
`core.py:record_with_unity` が `pkill -9 -f "Unity -projectPath"` で Unity を
無差別に殺すため、直列化しないと配信中の Unity が巻き添えで落ちる。

### 配信ログの置き場所

`bottan_live` スキーマに置いている。`affirmative_bot` ではない理由は、
`bsky-affirmative-bot/scripts/deploy.sh` が `drizzle-kit push` を自動実行しており、
`schemaFilter` が `['public','affirmative_bot']` なので、drizzle の定義に無い
テーブルをそこへ置くと **DROP 候補になる**ため。

### 記憶は SQL と Bot Memory API の2本立て

**素の SQL**（`live/memory.py`）は、その日の行動・Nagi と Bluesky の高得点ポスト・
直近のショート・前回配信のコメントを決め打ちで引く。フリートークの固定枠はこちら。

**Bot Memory API**（`live/bot_memory_client.py`）が本来の RAG。biorhythm_server の
LAN 内 API `POST /memory/search` を叩き、埋め込みと `pg_trgm` のハイブリッド検索は
**サーバ側**で走る。`affirmative_bot.bot_memory_documents` に実体があり、
`source_type` は `bsky_affirmed_post` / `nagi_affirmed_post` / `bsky_received_reply` /
`nagi_received_reply` / `bsky_received_like` / `nagi_received_reaction` /
`biorhythm` / `youtube_live_comment` の8種。

配信で届いたコメントは `memory.BotMemoryWriter` が
`bot_memory_documents`（`source_type='youtube_live_comment'`）へ非同期で upsert する。
本文が変わったら `embedding` を NULL に落として、サーバに埋め直させる。

検索はクエリが長いほど遅い（実測: 100文字 1.9秒 / 300文字 3.2秒 / 1000文字 9.1秒）。
サーバがクエリ全文を埋め込んだうえ、`similarity` と `ilike` をクエリ全文で全行に
当てるため。`BOT_MEMORY_QUERY_MAX_CHARS`（既定500）で頭を打たせている。

**メインループから叩かないこと。** 先読みは `_start_chores()` の雑務スレッドが行い、
ホットパスはキャッシュを読むだけにする。

### 同じ話題を二度出さない

`FillerPlanner` は配信中に出した話題を `_used` に貯め、**一度出た話題は二度選ばない**。
キーは固有名詞を並べた `(種別, 語のシグネチャ)`。語が拾えないときだけ、
`(種別, 空白を潰した本文の先頭60文字)` に落ちる。頭の60文字で見ていると、
同じネタでも言い回しが違うだけで別物として通ってしまう。

さらに `_used_terms`（出したお題に含まれていた固有名詞）を持ち、`_overlaps()` で
**中身が同じネタを種別をまたいで弾く**。RAG は同じ話が別の document_id で何件も
入っており、`excludeDocumentIds` は ID 単位なので原理的に防げない。受け取ってから
内容で捨てる。ただし `hobby` / `ask` の固定文には掛けない（人手で書き分けた
レパートリーなので、重なりで潰すと在庫が痩せる）。

在庫は RAG を除くと最大38件（hobby 11 / ask 6 / mood・nagi・bsky・previous_live が
各5まで / short 1）。`FILLER_IDLE_SEC` が25秒なので1時間の配信では**枯れうる**。
枯れたら `_used` を畳んで2周目に入る（`[filler] 話題を一巡したので…`）。
同じ話が二度出るのはよくないが、黙るよりはよい。

畳む前に、抽選で拾えなかっただけなのか本当に尽きたのかを**種別を順に当たって
確かめる**こと。`_KINDS` には重複があり、袋のどこから引き始めるかで一巡しても
触らない種別が出るので、抽選の空振りだけで畳むと在庫を残したまま2周目に入る。

**Nagi と Bluesky は同格に扱う。** botたんのホームは Nagi、Bluesky は毎日通う出張先で、
ペルソナにも「Blueskyだけがあなたの居場所であるかのように話さないこと」と書いてある。
`nagi` にだけ固定枠があると逆の偏りが出るので、`bsky`（`affirmative_bot.posts`）も
同じ形で置いてある。

### mood に引きずられない（`live/topics.py`）

上の `_used` は**お題にしか効かない**。2026-08-24 の配信では、お題が別物でも
「FLASHBULB」の話が61発話中14回出た。犯人はプロンプトの共通ブロックで、

- `_bot_state_block` が `mood`（さっきまでしてたこと）を全プロンプトに載せ、
  しかも「返事をこれに寄せてください」と指示していた
- `mood` は biorhythm 由来で 20〜90分ごとにしか変わらない。1時間の配信では
  ほぼ全発話に同じ文が乗る
- `_memory_block` の「今日やってたこと」も同じ `biorhythm_history` を引いており、
  先頭行が `mood` と同じ文だった（1プロンプトに同じ文が2回）
- `prefetch_rag` のクエリ先頭も `mood` なので、その話題の記憶ばかり返ってくる

対処は3つ。

1. **`mood` は1回載せたら落とす**（`LiveSession._bot_for_prompt`）。
   `BOT_MOOD_SERVE_LIMIT`（既定1）まで載せたら、以降のプロンプトから `mood` 行を
   外す。`mood` が更新されれば数え直すので、配信中に2〜3回は近況を話せる。
   落としたあとも記憶ブロックからは同じ話を外したいので、元の文は `mood_raw` に残す。
   - **「実際にその話をしたか」は見ない。** 載せれば高い確率でその話をする
     （それが上の症状そのもの）ので結果は変わらず、発話から固有名詞を拾う必要が
     なくなる。mood の3件に1件はカギ括弧が無く、語の抽出に頼ると取りこぼす
   - `_bot_context()` 側ではやらないこと。あちらは雑務スレッドからも呼ばれるので、
     そこで予算を使い切ってしまう。覗くだけなら `consume=False`
2. `_bot_state_block` の見出しから「寄せてください」を消し、`_memory_block` から
   `mood` と同じ行・`FillerPlanner.used_terms()` に載っている語を含む行を落とす。
3. `mood` が枯れたら `prefetch_rag` のクエリからも外す。ここを直さないと
   「その話題で検索 → 候補が返る → 全部弾かれる」という空回りが30秒ごとに続く。

`live/topics.py` は語を取るだけの小さなモジュール。**依存ゼロにしてある**
（テストがスタブ無しで読める）。形態素解析器は使わず、カギ括弧の中身と
ラテン文字語の2系統だけ。

- **取りこぼしても壊れない。** 語が拾えなければ `_topic_key` は従来の先頭60文字に
  落ち、`_overlaps` は False を返す。だから「拾えないかもしれない」を理由に
  判定を足さないこと。逆に、**拾ってはいけないものは必ず落とす**
  （抑制が効きすぎるほうが配信では目立つ）
- カギ括弧はセリフの引用にも使う。実ログには「大丈夫。全部、いいんだよ。」
  「おかしいな」が入っていたので、文の断片とひらがなだけの語は名前として数えない
- **Nagi / Bluesky はストップワード。** 固有名詞だが「話題」ではなく居場所の名前で、
  数えると「Nagiの投稿A」と「Nagiの投稿B」が同じネタ扱いになる

**ネタ元の偏りは配信側では直せない。** FLASHBULB の出どころは
`bot_state['seasonal_works_v1']`（Google 検索で取る「いま話題のもの」。music 枠は
数件しかなく7日キャッシュ）→ 日次予定表 → `biorhythm_history.mood` という経路で、
bsky-affirmative-bot 側の `seasonalWorks.ts` / `dailyPlan.ts` が持っている。
配信側は「上流が何を出しても配信が壊れない」ところまでを引き受ける。

### 他人の投稿を自分の体験にしない

2026-08-25 の夜版Shortsで、botたんが Nagi の他人の投稿（「資格取得の為に警察署に
行ってきました」）を**自分の一日の出来事として**喋った。原因はプロンプトの構造で、
自分の行動と他人の投稿が**同じ箇条書きに並んでいた**こと（詳細は `HANDOFF.md` の23）。
配信側にも同じ穴があったので一緒に塞いである。

**自分の体験と他人の話は、プロンプトの上で必ずブロックを分ける。**
`persona._memory_block` は2つの見出しを出す。

```
## 今日のあなたの出来事（あなた自身が体験したこと。自分の話として話してよい）
- 今日やってたこと：…
- 昨日出した動画：…

## 見かけた他の人の投稿・発言（**他人の話**。あなたの体験ではない。…）
- SNSのNagiで見かけた投稿：…
- Blueskyで見かけた投稿：…
- 前回の配信で視聴者が言っていたこと：…
```

- **行頭ラベルだけでは足りない。** 投稿本文は一人称で書かれているので、
  同じ箇条書きに混ぜると本文の一人称のほうが強く効く
- RAG も同じ。`_RAG_SOURCE_LABELS` は「自分の体験か他人の話か」が読み取れる
  文言にすること。**未知 `source` のフォールバックを「思い出」にしないこと**
  （以前の既定は「みんなとの思い出」で、他人の投稿が自分の思い出として読めた）
- **出どころは発話のあとも持ち回る。** `persona.topic_origin(topic)` が返した札を
  `conversation.add_solo_turn(..., origin=...)` と
  `LiveSession._begin_thread(..., origin=...)` に渡す。渡さないと、
  フリートークの発話は履歴で `（フリートーク）` ＋ `role=assistant` になり、
  次のターンからは「自分が言ったこと」としてしか残らない。
  掘り下げ（`build_followup_prompt`）には記憶ブロックが付かないので、
  ここが最後の砦になる

Shorts 側（`shorts/prompts.py`）は**そもそも選ばせない**という形にしてある。
締めで使う botたん自身のエピソードは `pipeline.pick_closing_mood()` が
Python 側で1件に確定し、プロンプトにはそれだけを載せる。直近2日で使った status の
除外も Python 側で適用する（除外しきったら除外を無視して1件返す。無人実行なので
候補ゼロで落とさない）。

### 話題を掘り下げる

コメントに答えて終わりにせず、出たテーマに別の角度をもう一言足す。
`reply_to_comment` と `speak_filler` の末尾で `_begin_thread()` がテーマを覚え、
`FOLLOWUP_IDLE_SEC`（既定8秒）空いたら `speak_followup()` が続きを喋る。
`FOLLOWUP_MAX_DEPTH`（既定2）まで掘ったら次の話題へ移る。

フリートークの後もテーマを立てるので、
`話題 → 掘り下げ → 掘り下げ → 次の話題` と自然に多段になる。

- **テーマの登録でネットワークを触らないこと。** `_begin_thread` はフラグを立てるだけで、
  RAG を引くのは雑務スレッドの `prefetch_followup()`。返事を喋っている15〜25秒の
  あいだに引き終わるので、掘り下げる時点では候補が揃っている
- **資料が無くても喋る。** RAG が引けるまで黙るのは本末転倒
- 掘り下げも `interruptible=True`。コメントが来たら文の切れ目で切り上がる
- クロージングの `FILLER_STOP_LEAD_SEC`（既定120秒）前からは、フリートークも
  掘り下げも出さずコメントの消化に専念する。`run_loop` は `LIVE_CLOSING_HHMM` で
  抜けてしまい、**それ以降に届いたコメントには一切反応できない**

### コメントを捨てるとき

`CommentQueue` は返事せずに捨てた件数を `dropped` に積み、捨てるたびにログを出す。
数えていないと「コメント欄には出たのに返事が来ない」に気づけない。

- **溢れ**（`maxlen`=200 超）: 優先度が低く、かつ古いものから捨てる。
  並べ替えは `(priority, -received_at)` で、残すのは先頭 maxlen 件。
  **受信時刻を昇順にしてはいけない** — 末尾＝「一般視聴者のいちばん新しいコメント」が
  捨てられ、意図と正反対になる（実際そうなっていた）
- **滞留**（`COMMENT_MAX_AGE_SEC`、既定180秒）: それ以上待たせた一般コメントは捨てる。
  取り出しは同じ優先度なら古い順なので、放っておくと「5分前のコメントにいま返事する」
  状態になり、視聴者から見た遅れが配信の後半ほど伸びていく。
  **スパチャ・メンバー・オーナーは古くても捨てない**

待ち行列の様子は30秒ごとに出る:

```
[live] キュー: 待ち12件 / 最古 84秒 / 受信 137件 / 破棄 4件
```

返事が遅れているのが滞留のせいなのか溢れて捨てているのかは、これでしか切り分けられない。

### 読みの直しは DB の1本管理

読み間違いは `affirmative_bot.bot_memory_pronunciations` に登録して直す。
`common/pronunciation.py` が**VOICEVOX へ送るテキストそのものを置換**する
（`audio_query()` が `/audio_query` に投げる直前）。字幕に出る日本語は元のまま。

**VOICEVOX エンジンのユーザー辞書は使わない。** エンジン側の辞書
（`~/voicevox_user_dict/user_dict.json`）とは独立していて、DB へ入れても
そちらには反映されない。二重に管理すると、どちらが効いているのか分からなくなる。

置換は区切り（半角/全角スペース・中黒・読点）をまたいで当たる。登録どおりの
一字一句でしか当たらないと実際にはまず外れる。2026-08-23 の配信では
`ファイアーエムブレム万紫千紅` と登録してあったのに「ファイアーエムブレム 万紫千紅」と
喋って素読みした。区切りを許すのは**文字種が変わる位置と、登録側に区切りがあった位置だけ**
（どこでも許すと `アニメ` が「アニ、メートル」に当たる）。長音 `ー` は読みの一部なので
境界にしない。ASCII は大小を無視するが、`Halo`(ヘイロー) と `halo`(ハロー) のように
大小で別語として登録されているものは、大小を保ったキーで先に引くので混ざらない。

**複合語は最小単位でも登録すること。** 長い surface だけに頼ると、
その一部だけを喋ったときに必ず外れる（`万紫千紅` 単体の行が無いと救えない）。

登録・無効化は bsky-affirmative-bot 側の CLI から行う（SQL を直接叩かない）:

```bash
pnpm tts-pronunciation -- set <surface> <spoken-form> [work|proper_noun]
pnpm tts-pronunciation -- disable <surface>
pnpm tts-pronunciation -- list
```

表の原典は `bsky-affirmative-bot/packages/database/src/botMemoryPronunciation.ts`。
自動学習（`origin='auto'`）は `manual` と `disabled` の行を書き換えないので、
手で入れた読みが後から流されることはない。surface は NFKC で正規化されて入る。

### energy

配信側は energy を**読むだけ**で、加算はしない。`memory.save_comment()` が
`bottan_live.comments` に残した行を、`bsky-affirmative-bot` の biorhythm_server
（`apps/biorhythm_server/src/liveCommentEnergySync.ts`）が15秒ごとに拾い、
1件につき内部energy +10 を足して `bot_state.biorhythm.liveCommentEnergyCursor`
を進める。

配信側から `POST /energy` を投げると同じコメントで二重に加算されるので投げない。
読みは `memory.get_biorhythm()` で `bot_state` を直接引く（内部スケール
0〜10000 を 0〜100 に直して返す）。
