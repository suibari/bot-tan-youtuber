# bot-tan-youtuber

全肯定botたんの YouTube 運用一式。Shorts の自動生成・投稿と、AITuber ライブ配信を
1つのリポジトリで扱う。

| | 中身 | 起動 | 時刻 |
|---|---|---|---|
| `shorts/` | 夜の Shorts（Nagi のポスト紹介）| `run.sh` | 毎日 18:00 JST |
| `shorts/` | 朝の勘違いクイズ Shorts | `run_quiz.sh` | 毎日 06:00 JST |
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
| 直前の発話が終わるのを待つ | 5〜15秒 | **ここがいちばん効いていた。** フリートークは `interruptible=True` で、文と文の切れ目でコメントの有無を見て切り上げる |
| LLM | 約2秒 | 実測（gemini-2.5-flash、2〜3文の構造化出力） |
| VOICEVOX + 送信 | 0.1〜0.7秒 | 全文まとめてではなく**1文ずつ**合成して Unity のキューへ流し込む。喋り出しまで実測 0.12秒 |

`_speak` は1文ごとに「合成 → `/speak` → いまの文が終わる 0.7秒 前まで待つ」を
繰り返す。0.7秒 あれば次の文の合成が間に合う（実測 0.05〜0.7秒）ので音は途切れず、
その待ち時間が割り込みの窓になる。字幕は `SubtitleScheduler.push()` で後から
足していく（発話を始める時点では2文目以降の尺がまだ分からないため）。

文と文の間の 0.25秒 の無音は `voice.synthesize_lines(tail_gap=...)` が作る。
1文ずつ別々に送るので、こちらで作らないと詰まって聞こえる。

### 黙っている間も動かす

Unity は生成モーションが乗っていない区間では Animator の Idle を流すだけで、
表情も `/emotion` で最後に指定した値のまま固定される。放っておくと
「同じ立ち姿・同じ顔」で1時間が過ぎる。

`idle.IdleAnimator` が配信ループとは別スレッドで、Unity の `/status` を見ながら

- 9〜20秒おきにプールのモーションを1本投げる（落ち着いたカテゴリを厚めに抽選）
- 6〜14秒おきに valence/arousal を少し振る（直前の発話の感情を素の顔として引き継ぐ）

を行う。発話中は何もしない。`/speak` を送ってから Unity が実際に鳴らし始めるまでは
`/status` が `speaking=false` のままなので、その隙に割り込まないよう `_speak` が
`idle.hold()` で明示的に手を引かせている。

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

### 「botたんRAG」について

pgvector も埋め込みテーブルも実在しない。`bsky-affirmative-bot` の埋め込みは
`packages/bot_brain/src/gemini/embeddingTexts.ts` でその場で計算して捨てており、
永続化されていない。記憶の実体は `affirmative_bot` スキーマの素の RDB なので、
`memory.py` が SQL で引いてプロンプトに載せている。
将来ベクトル検索が要るなら `bottan_live.comments` に `embedding` 列を足す形になる。

### energy

配信側は energy を**読むだけ**で、加算はしない。`memory.save_comment()` が
`bottan_live.comments` に残した行を、`bsky-affirmative-bot` の biorhythm_server
（`apps/biorhythm_server/src/liveCommentEnergySync.ts`）が15秒ごとに拾い、
1件につき内部energy +10 を足して `bot_state.biorhythm.liveCommentEnergyCursor`
を進める。

配信側から `POST /energy` を投げると同じコメントで二重に加算されるので投げない。
読みは `memory.get_biorhythm()` で `bot_state` を直接引く（内部スケール
0〜10000 を 0〜100 に直して返す）。
