# 引き継ぎ: 生成モーション改善（2026-08-11〜12）

作業用の一時ファイル。読み終わったら消してよい。

## 発端

`/tmp/bottan_20260811_180008.mp4`（夜版・変更前）に対する指摘:

1. 30秒前後の Mixamo の手を振るモーションが不要。ARDY 生成モーションに置き換えたい
2. ARDY モーションのつなぎが不自然（スッと別のモーションに切り替わる）
3. カメラを引いている割に動きが小さい。もっと身振り手振りを大きく

追加で出た指摘:

4. VTuber の配信のようにずっと動いていてほしい（立ったままに見える）
5. ジャンプでパンツが見えている。BAN リスクがあるので危険
6. 72秒で手が震えていて不安。ポーズはペルソナ（LLM）が選ぶことが何より大事だが、
   自由に書かせると話している内容と動きが合わない

---

## 確定した実装

### 1. Mixamo の挨拶モーション撤去（夜版のみ）

`pipeline.py` から `greetingTime1` の書き出しを廃止。

**理由**: `greeting_time1 = corners[0]["end"] - 2.0` だが、NagiCorner は毎回キーワード
検出に失敗してフォールバックするため `corners[0].end == closing_start` になり、
**構造的に必ず「締めの2秒前」に Standing Greeting（5.10秒）が鳴っていた**。
朝版（`quiz_pipeline.py`）は以前から `greetingTime1` を書き出しておらず、前例がある。
Unity 側はキーが無ければ `DoGreeting` を撃たない no-op なので C# の改修は不要。

生成モーションの区間も `4.6秒 〜 thankfulTime直前` の単一連続区間に統合した。
`thankful_time` は字幕の「高評価」キーワード検出依存で失敗時 0 になるため、
**0 のときは `closing_start` の手前で止める**こと（`DoThankful`/`DoWave` と衝突する）。

### 2. つなぎのスムーズ化（朝夜共通）

- `text-to-vrma/tools/ardy-engine/server.py`（他者リポジトリへのローカルパッチ、
  コミット済み `64b14b5`）: `_generate_stitched` の混合を線形から **smoothstep** に変更し、
  窓の長さを `blendSec` リクエストパラメータで可変にした。未指定なら従来の6フレーム
  なので後方互換。
- `core.ARDY_BLEND_SEC = 0.7`（従来 0.3秒相当）を `/generate` に送る。
- Unity `VrmaMotionPlayer.FadeDuration` を 0.25 → **0.5秒**、重みを `Mathf.SmoothStep` に。

**理由**: 混合が完全な線形だったため、位置は連続でも**速度が窓の両端で不連続**になり、
「スッと切り替わった」ように見えていた。窓を伸ばすことより smoothstep 化のほうが効く。

**評価**: ユーザーから「動きのつなぎは改善しました」と確認済み。

### 3. クリップ間クロスフェード（朝夜共通・Unity）

`VrmaMotionPlayer` の再生スロットを1本 → **2本**に拡張。Python 側は分割した `.vrma` を
`VRMA_CHUNK_OVERLAP = 0.5秒` だけ重ねて配置し、Unity は重なり区間で
**2クリップを先に混ぜてから Idle に乗せる**。

**理由**: 従来は逐次再生だったため、継ぎ目で前クリップのフェードアウトと次のフェードインが
重ならず、約1秒間 Idle（棒立ち）に引き戻されていた。順番に Lerp すると継ぎ目で Idle が
25%ほど残るので、先に2本を混ぜる必要がある。重みは SmoothStep なので
`f(x) + f(1-x) = 1` が厳密に成立し、重なり区間の合計はちょうど 1 になる。

**注意**: `VrmaMotionPlayer.FadeDuration`（Unity）と `core.VRMA_CHUNK_OVERLAP`（Python）は
**一致していないと継ぎ目で棒立ちが挟まる**。片方だけ変えないこと。

### 4. セグメントを短く・多く（朝夜共通）

`ARDY_MAX_SEGMENTS`（=12、サーバー側の上限）に合わせて1本を引き伸ばす方式をやめ、
**尺 ÷ `VRMA_SEG_TARGET_SEC`（2.6秒）** で本数を決める方式に変更。
12本を超えたぶんは `build_vrma_motions` が `.vrma` を分割し、上記3のクロスフェードで繋ぐ。

**理由**: ARDY の生成モーションは**尺の後半で必ず動きが止まる**。実測（5秒生成・
全ボーンの角速度[度/フレーム]、前半→後半）:

```
raises one hand to their chin        28.3 → 8.0
repeatedly sways their upper body    20.3 → 10.4
rocks from one foot to the other     19.7 → 11.4
```

「反復する動作」と書いても後半は持続しなかったので、**1本を短くして高エネルギーな
前半だけを使う**のが正しい対処。

**効果（実測・動画のフレーム間差分）**:

| 構成 | 動き量 平均 | ほぼ静止のフレーム割合 |
|---|---|---|
| 12本 / 平均5.0秒 | 1.63 | 28.9% |
| 24本 / 平均3.4秒 | 2.28 | 13.8% |

ユーザーから「動きは頻繁になり、改善しています」と確認済み。

### 5. 下半身の動作を禁止（安全・最優先）

- `prompts.py` / `quiz_prompts.py`: しゃがむ・膝を曲げる・跳ぶ・座る動作を明示的に禁止
- `core.VRMA_BANNED_RE`: `jump / hop / leap / squat / crouch / kneel / sit / lunge /
  spring / knees` を含む動作を**コード側で必ず落とす**
- `VRMA_HIPS_Y` を既定 0 に戻した

**理由**: 夜版19.3秒で下着が映っていた。原因は**ジャンプの高さではなく脚のポーズ**で、
`bends their knees deeply and springs straight up` が「膝を深く曲げて脚を大きく開く
しゃがみ」を生み、カメラが正面・腰の高さにあるため真下から覗く画になっていた。
ARDY はジャンプの予備動作として必ず深くしゃがむので、**跳ぶ指示が残る限り再発する**。
`VRMA_HIPS_Y` を下げても脚のポーズは変わらないので解決しない。

プロンプトだけだと LLM が破ったときに事故る（損害が BAN）ので、**コード側を最後の砦**に
している。`plan_vrma_from_sentences` の入口で必ず通る。

### 6. 拍手を禁止

`VRMA_BANNED_RE` に `clap / claps / clapping / applaud` を追加。

**理由**: 夜版72秒で「手を胸の前に上げたまま小刻みに震える」画になっていた（ユーザー指摘）。
ARDY が拍手を描けず、手が中途半端に往復するだけになる。
**シードを変えた独立2サンプル**（本番シード・単発生成シード777）とも再現。
`ARDY_ARM_SPREAD` を 12 → 6 → 0 に下げても変わらなかったので、
腕を開いている設定のせいではなく ARDY 側の限界。

**禁止はこの2項目（下半身・拍手）だけ**。他に禁止しているものはない。

### 7. モーションを文に紐づける（本命）★

台本スキーマを変更し、**各 sentence が自分の `motion` を持つ**ようにした。

```json
{"text": "文章", "valence": 0.8, "arousal": 0.4,
 "motion": "A person stands in place facing forward and ..."}
```

- `core.generate_voice` が**各文の実測尺**を返すようになった（VOICEVOX で文ごとに
  個別合成してから連結しているので正確に取れる。文字数比だと漢字とかなで
  読み上げ速度が違うぶんズレる）
- `core.plan_vrma_from_sentences` が、文のモーションを**その文が読まれる時刻**に置く
- 1文が長いとき（実測で平均8秒）は**同じ指示文を2〜3本に分けて連続生成**する。
  ARDY はセグメントごとに独立生成するので、同じ指示でも毎回新しい動きが出て、
  意図を保ったまま尺の後半で動きが止まるのを防げる
- 短すぎる文は直前のセグメントに吸収（1本が短いと Idle と見分けがつかない）
- `pipeline.build_vrma_blocks` を全面書き換え。旧実装（コーナー単位の `motions` を
  尺で按分）は削除

**理由**: 旧実装は**どの文に対応するかを一切見ておらず**、セクションのモーション列を尺で
按分して順に流すだけだった。しかも埋め草はリストの末尾に足されるので、
**ペルソナが最後の文のために書いた動きがセクションの中盤で再生されていた**。
ユーザーの「話している内容と動きが一致していることは絶対要件。ただ動けばいいわけではない」
という要求に対し、選択の質ではなく時刻の割り当てが原因だった。

あわせてプロンプトから12個の定番ポーズメニューを撤廃し、「よく使う形」の例示に格下げ。
**ペルソナが選ぶことを最優先**という方針に合わせ、禁止事項（上記2項目）だけを硬く残した。

### 8. 体の向き・傾きを開放（2026-08-12）★

**ARDY は体のワールド回転（向きも傾きも）を出していて `.vrma` にも入っているのに、
Unity が既定で全部捨てていた。** 動きが小さい原因の本体はここだった。

| チャンネル | ARDY | .vrma | 従来のUnity |
|---|---|---|---|
| hips ワールドヨー（体の向き） | あり（360°可） | 入っている | `_bodyRotationWeight=0` で全捨て |
| hips ロール/ピッチ（傾き） | あり | 入っている | 同上・巻き添えで全捨て |
| Spine/Chest/UpperChest Twist | あり | 入っている | `YawMuscleNames` で Idle 値に書き戻し |
| Neck/Head Turn | あり | 入っている | 同上 |
| Spine/Chest の左右曲げ | あり | 入っている | 通っていた |

根拠: `text-to-vrma/tools/ardy-engine/retarget.py:94-100`（hips に全ワールド回転を書く）、
`tools/spec2vrma.mjs`（素通し。傾きを戻す `appendNeutralEnding` はアプリ側 `src/main.js:411`
だけで、このパイプラインは通らない）。

`VrmaMotionPlayer` を「ヨーを殺す」から**「上限をかけて通す」**に変更した。

- **ヨーは Idle からの絶対角でクランプ**する。ARDY の連結は前セグメントの終端ヨーに
  合わせて回転を積み上げる（`server.py:456-463`）ので、相対量で制限すると
  **一度横を向いたら残りずっと横向き**になる。絶対角なら構造的に起きない
- **ヨーの総量 = hips のヨー + 上体3本の Twist muscle**。片方だけ塞いでも体は向くので、
  合計に一様な倍率をかけて縮める
- **余ったぶんを首で逆に回して顔をカメラに残す**（`-vrmaHeadCounter`）。
  体は斜め・顔はこちら＝「肩越しに振り返る」画になる。逆補正はオフセットなので
  クリップ自身の首の動きは消えない
- **hips の傾きは通す**（`-vrmaBodyTilt`）。`-vrmaGain` は腕にしか効かないので、
  上体の傾きの大きさを戻せるのはここだけ

新引数（**すべて既定値のままなら改修前と完全に同じ見た目**）:

```
-vrmaYawLimit    度    上体の向きの上限   (Unity既定0 / パイプライン 35)
-vrmaHeadYaw     度    首の横振りの上限   (Unity既定0 / パイプライン 15)
-vrmaHeadCounter 0..1  首での逆補正       (既定0.8)
-vrmaBodyTilt    倍率  腰の傾き           (Unity既定0 / パイプライン 1.0)
```

`-vrmaKeepFacing` / `-vrmaBodyRotation` は撤去（Python から一度も渡していなかった）。

**注意**: `retarget.py:105` の Euler XYZ 分解は **Y=±90° が特異点**。
`-vrmaYawLimit` を 80° 超に上げるときは、そこから見直すこと。

### 9. プロンプトの実測（2026-08-12）★

ARDY の `/generate` を直接叩き、spec の hips ヨーと上体ロールの振幅を測った
（3秒生成・**独立2シード**・単位は度。対照は `raises one hand to their chin`）:

| 指示 | hipsヨー幅 | 上体ロール幅 |
|---|---|---|
| （対照） | 4.5 / 8.2 | 6.4 / 5.1 |
| `turns their upper body to their right, then back to the front` | **81.1 / 76.3** | 25.3 / 7.9 |
| `leans their upper body to their left, then straightens up` | 10.0 / 9.3 | **25.6 / 27.6** |
| `tilts their upper body far to their left side` | 4.0 / 5.9 | 27.9 / 4.1（再現せず） |
| `slowly sways their upper body from side to side` | 6.4 / 2.7 | 6.2 / 2.5 |
| `shakes their head slowly from side to side` | 1.2 / 1.5 | 首ヨー 4.9 / 1.1 |

分かったこと:

1. **「…して、正面に戻る」という往復の形だけが効く**。強調語（far / bends）では再現しない
2. **`sways` と `shakes their head` は対照と差が無い＝効いていない**。
   `slowly sways their upper body from side to side` は `VRMA_IDLE_MOTIONS` にも
   プロンプトの例にも入っていたが、**ずっと無意味だった**。両方から外した
3. **ひねりは hips のヨーにしか出ない**（`chest` の twist は 0.2° 程度）。
   つまり `-vrmaBodyTilt` が 0 だと体はまったく向かない。両方セットで有効にすること
4. `turns to look over their right shoulder` はヨー 79〜89° に達する。
   クランプで 35° に落ちるので画は同じだが、Euler の特異点に乗るので例からは外した

`prompts.py` / `quiz_prompts.py` から `A person stands in place facing forward` の
**「facing forward」を撤去**した（正面を明示すると ARDY が体を向けなくなる）。

### 10. 朝版を文ベースへ移行 + thankful 撤去（2026-08-12）

- `quiz_prompts.py`: `motions: {think/answer/explanation: [{text, emphasis}]}` を廃止し、
  **各 sentence が `motion` を持つ**夜版と同じ形にした。`emphasis` は消滅。
  発話の無い THINK だけ `motions.think`（英文2本の配列）を残す
- `quiz_pipeline.build_audio` が文ごとの実測尺から `seg["spans"]` を作る
- `quiz_pipeline.build_vrma_blocks` を `core.plan_vrma_from_sentences` 呼び出しに書き換え。
  パート間のパディング（`PAD_AFTER`）は前の文の区間に含めて隙間を無くしている
- **`thankfulTime` を書き出さなくなった**。AFF の頭で Mixamo の一礼を撃つために
  生成モーションを AFF で止めていたが、話に合った動きより優先するものではない。
  窓は `THINK.start 〜 END.start - VRMA_TAIL_PAD` に延びた
- `core.plan_vrma_segments` / `dedupe_vrma_segments` / `merge_vrma_spans` /
  `VRMA_BIG_SEC` / `VRMA_SMALL_SEC` は**呼び出し元ゼロ**になった。
  文ベースが安定するまで戻せるように残してある（コメントで明示済み）

### 11. セグメント上限の切り詰め方を修正

`plan_vrma_from_sentences` は 24本を超えると `out[:24]` で**後ろから落として**いたため、
台本が長い日は終盤が丸ごと Idle（棒立ち）に戻っていた。
**先に「1文を何本に割るか」を 3→2→1 と減らし**、それでも超えるときだけ
短いセグメントを隣に吸収させる方式にした。窓は必ず端まで埋まる。

---

## 現在の設定値

```
core.py
  VRMA_SEG_TARGET_SEC     = 2.6   1セグメントの目標尺。本数はこれで決まる
  VRMA_SEG_MIN_SEC        = 2.0   これ未満は Idle と見分けがつかない
  VRMA_MAX_SEGMENTS_TOTAL = 24    1ブロックの上限（= 2チャンク）
  VRMA_CHUNK_OVERLAP      = 0.5   Unity の FadeDuration と一致必須
  VRMA_TAIL_PAD           = 0.4   次の Mixamo に食い込ませない余白
  VRMA_GAIN               = 1.0   腕の振幅ゲイン。既定無効（下の残件参照）
  VRMA_HIPS_Y             = 0.0   腰の上下移動。既定無効（下半身禁止のため）
  VRMA_BODY_TILT          = 1.0   腰の傾き。ひねりを画に出すのにも必須
  VRMA_YAW_LIMIT          = 35    上体の向きの上限[度]
  VRMA_HEAD_YAW           = 15    首の横振りの上限[度]
  VRMA_HEAD_COUNTER       = 0.8   上体の向きを首で打ち消す割合
  ARDY_BLEND_SEC          = 0.7   セグメント境界のクロスフェード長
  ARDY_ARM_SPREAD         = 12.0  変更していない
  ARDY_MAX_SEGMENTS       = 12    サーバー側の上限。変更不可

VrmaMotionPlayer.cs
  FadeDuration            = 0.5f
```

Unity 側の既定値はすべて「従来どおり」（yawLimit=0 / headYaw=0 / bodyTilt=0）なので、
**引数を渡さなければ改修前と同じ見た目に戻る**。切り分けはこの4つを 0 にすればよい。

## 変更したファイル（すべて未コミット）

```
bottan-pipeline-dev/   core.py / pipeline.py / prompts.py / quiz_pipeline.py / quiz_prompts.py
bottan-video-dev/      Assets/Scripts/VrmaMotionPlayer.cs
text-to-vrma/          コミット済み（64b14b5「スムージング導入」）
```

---

## 残件

### A. 本番相当の実行確認（最優先・未実施）

**段階2（録画による目視比較）は完了。** 4条件を同一 `.vrma`（7セグメント×3.0秒、
ターン/傾き/腕のジェスチャを混在、seed_base `probe20260812`）＋無音WAVで録画し、
`ffmpeg` で同時刻フレームを並べて比較した（`scratchpad/rec/cmp_*.png`, `C_tile.png`）。

| 条件 | 引数 | 結果 |
|---|---|---|
| A | `-vrmaYawLimit 0 -vrmaBodyTilt 0` | 現行。ほぼ正面固定 |
| B | `-vrmaYawLimit 0 -vrmaBodyTilt 1.0` | 傾きのみ。A より明確に動くが向きは変わらない |
| C | `-vrmaYawLimit 35 -vrmaBodyTilt 1.0 -vrmaHeadYaw 15 -vrmaHeadCounter 0.8` | **採用**。体は明確にひねる／傾くのに顔は常にカメラ側 |
| D | C から `-vrmaHeadCounter 0` | 顔もいっしょに流れる。C との差は小さいが C のほうが安全 |

C を毎秒サンプリング（0〜23秒）して全フレーム確認した結果:
- **顔は全フレームで正面〜斜め前を向いており、口が隠れる角度は一度も出ない**
- 髪・スカートのスプリングボーンの破綻なし
- 背景 Quad の端は一度も画面に入らない
- 体の向きの変化は 5秒・11秒・16〜17秒付近ではっきり視認できる

残るは**段階3（本番相当の通し実行）**のみ。

**提案7（文への紐づけ）・8（向きと傾き）・10（朝版の移行）を入れてから、
夜版も朝版も一度も通しで実行していない。** ドライランと、無音WAV + `.vrma` だけの
録画（残件G の手順）では確認済み。

確認すべき点（夜版 → 朝版の順）:
- `[モーション] 4.6〜XX秒 → N セグメント（モーション指定のある文 n/m）` のログ
- 話している内容と動きが対応しているか（これが主目的）
- **顔がカメラに残っているか**（提案8。口パクが見えなくなったら `-vrmaHeadCounter` を上げる）
- 拍手・下半身の除外ログが出た場合、差し替えが自然か
- 朝版は `thankfulTime` が消えて END 直前までモーションが続いているか

### B. 朝版の文ベース移行 → 完了（提案10）

`core.plan_vrma_segments` / `dedupe_vrma_segments` / `merge_vrma_spans` /
`VRMA_BIG_SEC` / `VRMA_SMALL_SEC` は**呼び出し元ゼロ**になった。
文ベースが本番で確認できたら消してよい。

### C. 動画の尺が常態的に超過

`prompts.py` に「合計尺は必ず60秒以内。65秒を超える台本は生成しない」とあるが、
実測は 66〜102秒。**変更前のプロンプトでも平均430文字＝約61秒**で、もともと境界すれすれ
だった（4回サンプリングで確認）。私の加筆で +16% 程度押し上げている可能性は残るが、
n=4 では有意差と言えない。**以前からの性質であり、今回の変更が持ち込んだ回帰ではない。**
生成時間も尺に比例して伸びるので、別途対処の余地がある。

### D. 振幅ゲイン（`-vrmaGain`）は実装済みだが既定無効

Unity 側に腕の muscle だけを増幅する仕組みが入っているが、既定 1.0（無効）。
1.35 でも 1.2 でも、ARDY が出す「手を頭の近くに上げる」動きを引き伸ばして
**腕（袖）が顔を覆う**ため使えなかった。倍率を下げても改善しない。
衣装やカメラが変わったら `VRMA_GAIN=1.2` などで再検討できる。

### E. ジャンプは実装済みだが封印

`-vrmaHipsY`（腰の上下移動、上方向のみ・クリップ先頭からの差分）は動く状態で残っているが、
下半身の動作を禁止したので既定 0。**衣装が変わって跳べるようになったら
`VRMA_HIPS_Y=1.0` で復活できる。**

### F. 検証方法についての反省（重要）

**ARDY は拡散モデルでシードによって出力が大きく変わるのに、候補ごとにシード1つでしか
測っていなかった。** 角度変化の二階差分（カクつき）が大きい候補を避けたつもりだったが、
実際に録画して見比べたところ画面上の差は確認できず、対照群を含む全条件が
「腕を下ろして立っているだけ」だった。**この指標は判断に使えない。**

以後、モーションの採否は**実際に録画して目で見て**決めること。数値は補助でしかない。
拍手のように独立2サンプルで再現したものだけが確かな根拠になる。

### G. 検証中はフルパイプラインを回さないこと

`./run.sh` は毎回 Gemini API と DB を消費する。モーションの調整に台本生成は不要なので、
LLM を使わないハーネスで検証すること。

- **モーション単体の比較**: ARDY の `/generate` を直接叩いて spec JSON を取る
  （サーバー起動4〜5分、1本あたり数十秒）
- **見た目の確認**: `core.make_silence_wav` の無音WAVと `vrmaMotions` だけを入れた
  emotions.json を作り、`core.record_with_unity` で録画（約5分）。台本も音声も不要
- **既存の `.vrma` の使い回し**: ARDY 生成もスキップして録画だけ（約5分）
- Unity は確率的に Mono ランタイムクラッシュを起こす。`core._retry` で包むこと

### H. 参考: 検証用スクリプトの置き場

セッションのスクラッチパッドに置いたので**次のセッションでは消えている**。
必要なら作り直すこと（上記 G の手順で書ける）。今回作ったのは3つ:

- `probe_yaw.py` / `probe2.py` … ARDY を直接叩いて spec の hips ヨー・上体ロールを測る。
  プロンプトの候補比較に使う。**独立2シードで回すこと**（残件F）
- `probe_record.py` … 無音WAV + `.vrma` だけで録画し、`-vrmaYawLimit` などの
  引数だけを変えた4条件を並べて比べる

### I. カメラワーク（未着手・効果は大きいはず）

「いきいき」への効きは体の動きより大きい可能性がある。現状カメラは
`VideoRecorder.cs:91-110` の 4.6秒 0.7m 一回きりで、以降まったく動かない。

- **手持ち風の微細な揺れ**（±0.5cm / ±0.2°、Perlin）… 破綻リスクほぼゼロで棒立ち感が消える。
  次の候補として最有力
- コーナー境界でのゆっくりした寄り/引き（既存の `-cameraPullbackAt` を複数回打てるようにする）
- 斜めアングルへのカット … 背景 Quad（30×18・位置固定）の端が映らないか要検証

### J. 視線・Idle 区間

- `VRM1FaceAnimation.cs:331-343` は LookAt にパーリンノイズを乗せているだけ。
  考えるときに視線を外して戻す、を感情タイムラインに紐づけられる
- 0〜4.6秒（Blow A Kiss）と締め以降は素の `Idle.fbx` のまま

---

# 追記: 女の子らしい所作 + 朝版フックの声色（2026-08-15）

## 発端

`7cd814c` 時点の動画に対する指摘:

1. **モーションが男っぽい**。大ぶりなこと自体は良いので、振幅は落とさずに直したい
2. **朝版のフックにも夜版と同じ声色の変化がほしい**

ユーザーに「男っぽい」の中身を確認したところ、**腕の開き・肘 / 動きの質・速さ /
手・首の細かさ**の3点だった。**立ち姿勢と脚は対象外**（内股などの脚の補正は不要）。

## 12. 指と親指が全編ずっと固定ポーズだった（手の問題の原因）★

`.vrma` の骨格には **親指とつま先のボーンが無い**。
`text-to-vrma/src/vrmaBuilder.js:6-42` の `SKELETON` が持つのは
20本の体幹・手足＋**人差し指〜小指×3関節だけ**で、親指は含まれていない。
さらに人差し指〜小指は ARDY が出力しないので、
`vrmaBuilder.js:119-129` が**クリップ全編に固定値のカール（14/17/10度）を焼き込んでいる**。

実測（本番で生成された `Assets/Motions/body.vrma` を直接パース）:

```
ノード数 44 = 体幹・手足20 + 指24（Index/Middle/Ring/Little × 3関節 × 2手）
親指のノード: なし
つま先のノード: なし
アニメーションチャンネル 45（指24本はすべて定数）
```

一方 Unity 側 `VrmaMotionPlayer.LateUpdate` は **95 muscle 全部を無条件に Lerp** していた。
結果、生成モーション区間（＝ほぼ全編）で:

| muscle | クリップ側の値 | 画に出ていたもの |
|---|---|---|
| 人差し指〜小指 32本 | 固定カール | 全編ずっと同じ手の形 |
| **親指 8本** | **ボーンが無い → 0** | **Unity 既定＝親指が伸びたまま** |
| つま先 2本 | ボーンが無い → 0 | Idle のつま先が潰れる |

つまり **VRM モデルが持っている手のポーズが、汎用の固定ポーズに上書きされていた**。

対処: `-vrmaKeepIdleHands`（0/1・既定0）。1 のとき指・親指・つま先の muscle を
Lerp から外して Idle の値を残す。Unity ログで **42 muscle**（指40＋つま先2）が
除外されることを確認済み。

## 13. 姿勢バイアスと平滑化（Unity）

`.vrma` 側に相当するチャンネルが無い／ARDY がほとんど動かさないため、
プロンプトでは作れない。再生側で足す。**すべて既定0＝従来と同じ見た目**。

```
-vrmaKeepIdleHands 0/1  指・親指・つま先を Idle のまま残す      (パイプライン 1)
-vrmaElbowBend     度   肘を常時わずかに曲げる                  (パイプライン 8)
-vrmaWristBend     度   手首を常時わずかに曲げる                (パイプライン 6)
-vrmaHeadTilt      度   首を常時わずかに傾ける                  (パイプライン 4)
-vrmaSmooth        秒   ポーズを時間方向に平滑化する            (パイプライン 0.10)
```

- バイアスは**度で受けて muscle 値に換算**する（`ResolveMuscles`。可動域がボーンごとに
  違うので、生の muscle 値で足すと左右・部位で効き方が変わる）。`weight` を掛けるので
  Idle 区間には効かない。**符号付き**なので、向きが逆なら負値を渡せば再ビルド不要
- 首の傾きは Neck:Head = 4:6 に配る（頭だけ傾けると首が折れて見える）
- 平滑化は**2本のクロスフェード合成が済んだ後**の信号にかける一次ローパス。
  角ばった動きの角が丸まる。**時定数を上げると速い動きの振幅まで落ちる**ので
  0.15秒を超えないこと（引数側で 0.5秒 に制限）
- `ResolveYawMuscles` はヨー以外にも使うので `ResolveMuscles` に改名した

## 14. ARDY 側

- `ARDY_ARM_SPREAD` を **12 → 8**。脇が開いて男性的に見えるという指摘への対処。
  **0 にはしないこと**。リアル体型のモーキャプをアニメ体型に当てる都合で、
  開きが足りないと腕（袖）が胴にめり込む（`retarget.py` の `ARM_SPREAD_SIGN`）
- プロンプトの主語を **`A person` / `their` → `A woman` / `her`** に変更。
  `prompts.py` / `quiz_prompts.py`（ルール文・例示・フォールバック台本）と
  `core.VRMA_IDLE_MOTIONS` の全部。
  **提案9の実測値は `A person / their` 版のもの**なので、振幅が落ちていないか測り直すこと
- `VRMA_IDLE_MOTIONS` の index 1 と 7 が同一文字列で重複していた（実効7種）のを直した

## 15. 朝版のフックの声色

夜版のパラメータは `core.generate_voice` にハードコードされていたので、
`core.HOOK_VOICE_PARAMS` に切り出して朝夜で共有するようにした（夜版の挙動は不変）。

```
speedScale 0.85 / intonationScale 1.4 / volumeScale 1.3 / pitchScale 0.05
```

朝版は `question_intro`（パートID `Q`）が「掛け声／問題文／どっちだと思う？」の
2〜3文なので、**掛け声の1文目だけ**に当てる（`quiz_pipeline.HOOK_PART` /
`HOOK_SENTENCES`）。問題文まで `speedScale 0.85` にすると間延びするうえ尺が伸びる。
実測: 「今日の勘違いクイズ！」1.589秒 → 1.877秒。

**字幕もあわせて直した。** `build_subtitles` はパート内の全文を1本に連結し、
既定の速さで測ったモーラ時刻を `actual_duration` に線形スケールしている。
パート内に速さの違う文が混ざると配分がズレるので、`seg["spans"]`（文ごとの実測区間）を
使って **Q だけ境界で切って2回計算する**。切らないパートは spans が
`seg["start"]/["duration"]` と一致するので従来と完全に同じ結果になる。

## 16. Unity 引数の組み立てを1か所に

夜版 `pipeline.py` と朝版 `quiz_pipeline.py` に同じ引数リストが2つ書いてあり、
片方だけ直すと食い違う状態だった。`core.vrma_unity_args()` にまとめた。

## 17. 実測（2026-08-15）

検証は録画ハーネスのみ。フルパイプラインは回していない（残件G）。
同一の `.vrma`（`Assets/Motions/body.vrma`・28.55秒）＋無音WAVで、
**引数だけを変えて**録画した。カメラは2種類撮ってある。

| 条件 | 引数 |
|---|---|
| A / A2 | 現行（新引数はすべて既定0） |
| B / B2 | `-vrmaKeepIdleHands 1` |
| C / C2 | B + `-vrmaElbowBend 8 -vrmaWristBend 6 -vrmaHeadTilt 4` |
| D / D2 | C + `-vrmaSmooth 0.10` |

（A〜D はアップ＝手の確認用、A2〜D2 は `-cameraPullbackZ 0.7` の本番framing）

### 手（A vs B）— 明確に改善

23.97秒地点。**A は指が1枚の板に融合し、親指が見えない**（ボーンが無く Unity 既定の0で
伸びきったまま）。**B は指が分離し、親指も形になっている。**
差分の大きいフレームを `blend=all_mode=difference` で機械的に拾って比較した。

### 首の傾き・肘（B2 vs C2）— 効いている

2.57秒地点で首が明確に傾き（小首をかしげる）、腕がわずかに内側に畳まれる。
**`ElbowBendSign = -1`（負の向きが「曲げる」）で正しかった**ことが画で確認できた。

### 平滑化（A2 vs D2）— 振幅は落ちず、カクつきだけ減る

`-vrmaSmooth 0.10` は当初「大ぶりが失われたのでは」と見えたが、**位相の遅れであって
振幅の低下ではなかった**。A2 の 23.97秒 と D2 の **24.10秒** がほぼ同一のポーズ
（遅れ約0.13秒）で、D2 は 24.25〜24.45秒でより大きく腕を上げきっている。

動画のフレーム間差分による実測:

| 条件 | 動き量 平均 | ほぼ静止の割合 | 動き量の二階差分（カクつき） |
|---|---|---|---|
| A2 | 1.659 | 38.7% | 0.733 |
| B2 | 1.654 | 39.1% | — |
| C2 | 1.648 | 38.8% | 0.727 |
| D2 | 1.626 | 38.8% | **0.265** |

**動き量は 2% しか落ちないのに、カクつきは 64% 減る。** 一次ローパスなので
高周波だけが落ちるという理屈どおりの結果。代償は 0.13秒 の遅れだけ。
気になるなら時定数を下げる（遅れは時定数に比例する）。

### プロンプトの主語（`A person / their` → `A woman / her`）— 振幅は落ちない

ARDY の `/generate` を直接叩いて実測（3秒生成・**独立2シード**・5種類の動作。
`speed` は全ボーン平均の角速度[度/フレーム]で「動きの大きさ」の代理指標）:

| 動作 | person yaw / speed | woman yaw / speed |
|---|---|---|
| opens both arms out to the sides | 3.7 / 0.63 | 3.1 / **0.24** |
| raises both arms straight up | 2.5 / 0.40 | 6.1 / 0.63 |
| turns upper body to the right, then back | 75.1 / 0.47 | 53.5 / 0.57 |
| leans upper body to the left, then straightens | 14.0 / 0.23 | 16.8 / 0.32 |
| brings one hand up to the chin | 3.4 / 0.39 | 4.6 / 0.49 |
| **全体平均 speed** | **0.425** | **0.449** |

**全体では +5.6% で、動きは小さくなっていない。** 5種のうち4種で woman のほうが大きい。

例外は `opens both arms out to the sides`（0.63 → 0.24）で、これは独立2シードとも
woman が低かったので再現している。ただし person 側のシード間分散が 0.35 vs 0.91 と
非常に大きく、n=2 では確定的なことは言えない。全体傾向を覆すものではないと判断した。

`turns their upper body` の hipsYaw が 75.1 → 53.5 に下がっているが、
`VRMA_YAW_LIMIT=35` でどのみちクランプされるので画は変わらない。
むしろ Euler の特異点（Y=±90°）から遠ざかるので安全側。

**「女の子らしく見えるか」自体は数値では測れない。** 実際の動画で判断すること。

### 腕の開き（`ARDY_ARM_SPREAD` 12 vs 8）— 狙いどおり、めり込みも無い

`armSpread` はリターゲット時の静的オフセットなので、**シードを固定すれば動きは完全に同じで
腕の開きだけが変わる**。同一 seed（1824691967）・同一プロンプト（"A woman / her" 版）で
7セグメント×3.0秒を2回生成し、Unity 引数も揃えて録画した。

- 8 のほうが**腕が体に近く、シルエットが細い**。12 は脇が開いて肘が外を向いている
- **袖が胴にめり込む破綻は 8 でも出ていない**（0.9秒・14.1秒の2点で確認）。
  サーバー既定は 6 なので、8 はまだ余裕がある側

## 18. 現在の設定値（更新）

```
core.py
  ARDY_ARM_SPREAD         = 8     ← 12 から変更
  VRMA_KEEP_IDLE_HANDS    = 1     新規
  VRMA_ELBOW_BEND         = 8     新規（度・負の向きが「曲げる」は C# 側で持つ）
  VRMA_WRIST_BEND         = 6     新規（度）
  VRMA_HEAD_TILT          = 4     新規（度）
  VRMA_SMOOTH             = 0.10  新規（秒）
  （他は変更なし）

VrmaMotionPlayer.cs
  既定値はすべて従来どおり（keepIdleHands=false / 各バイアス0 / smooth=0）
```

## 19. 残件（更新）

### A'. 本番相当の通し実行（最優先・未実施）

**提案7・8・10・12〜16 を入れてから、夜版も朝版も一度も通しで実行していない。**
検証はすべて録画ハーネス（無音WAV + `.vrma`）とVOICEVOX単体で行っている。

確認すべき点:
- 朝版のフックで**掛け声だけ声色が変わっている**か。字幕がズレていないか
- 手が全編を通して自然か（今回いちばん効いた変更）
- `-vrmaSmooth` の 0.13秒 の遅れが、話している内容とのズレとして気になるか
  （気になるなら `VRMA_SMOOTH=0.06` などに下げる。遅れは時定数に比例する）
- `A woman / her` 版のプロンプトで LLM が書いた motion が、
  ちゃんと `A woman stands in place` で始まっているか

### 検証スクリプト（残件H の更新）

今回スクラッチパッドに置いたのは4つ。**次のセッションでは消えている**ので、
必要なら作り直すこと。

- `probe_voice.py` … 固定のダミー台本で `build_audio` + `build_subtitles` だけを回す。
  LLM・DB・Unity 不要。VOICEVOX のみ
- `probe_record.py` … 無音WAV + 既存 `.vrma` で条件別に録画してタイル比較。
  `A`〜`D` がアップ、`A2`〜`D2` が `-cameraPullbackZ 0.7` の本番framing
- `probe_ardy.py` … `/generate` を直接叩いてプロンプトごとの振幅を測る（**独立2シード**）
- `probe_spread.py` … 同一シードで `armSpread` だけ変えた `.vrma` を2本作って録画

**差分の大きいフレームの見つけ方**（今回いちばん役に立った手口）:

```
ffmpeg -i A.webm -i B.webm -filter_complex \
  "[0:v][1:v]blend=all_mode=difference,signalstats,\
   metadata=print:key=lavfi.signalstats.YAVG:file=-" -f null - 2>/dev/null \
 | paste - - | sed 's/.*pts:\([0-9]*\).*YAVG=\([0-9.]*\)/\1 \2/' | sort -k2 -gr | head
```

2条件が**どの瞬間にいちばん違うか**を機械的に出せるので、目で探さなくて済む。
動き量・カクつきは `tblend=all_mode=difference` の YAVG の平均と二階差分で測れる。

### D'. 振幅ゲイン（`-vrmaGain`）は据え置き

依然 1.0（無効）。今回は触っていない。

---

# 追記2: 2026-08-15 の本番実行で出た2件

`logs/quiz_20260815_125959.log`。**モーションはユーザー評価「とてもよくなっています」。**
ただし2つ問題が出た。

## 20. `SKIP_YOUTUBE=TRUE` が効かず動画が公開された ★

`quiz_pipeline.py:628` は `os.getenv("SKIP_YOUTUBE") == "true"` と**完全一致**で見ていた。
`TRUE` を渡しても一致しないのでスキップされず、`YOUTUBE_PRIVACY` の既定が `public` の
ため**公開でアップロードされた**（videoId `7q7mXlTSbxg`）。夜版 `pipeline.py:500` も
同じ書き方だった。同じファイル内の `KEEP_TEMP` / `QUIZ_NO_CONSUME` は
`.lower() == "true"` を使っていて、揃っていなかった。

対処: `core.env_flag(name, default=False)` を追加し、`true / TRUE / 1 / yes / on` を
すべて真として扱う。真偽値の環境変数はすべてこれを通すようにした
（`SKIP_YOUTUBE` / `KEEP_TEMP` / `QUIZ_NO_CONSUME` / `USE_LOCAL_LLM` /
`ARDY_REUSE` / `ARDY_FREE_OLLAMA`）。

## 21. サムネイル設定の404で、公開済みなのに記録が残らなかった ★

アップロード自体は成功して videoId が返っているのに、**直後の
`thumbnails().set()` が `videoNotFound` の404**を返した。動画がまだ処理中で
サムネイルAPIから見えていない、YouTube 側の既知のラグ。

問題は例外が `upload_to_youtube` を突き抜けたこと。`yt_url` が返らないので
**`save_youtube_upload_to_db` も `notify_discord` も `mark_used` も走らない**。
結果、**動画は公開されたのに DB に記録が無く、クイズも消費済みにならない**という
最悪の不整合になった。

対処: `youtube._set_thumbnail()` に切り出し、

- `THUMBNAIL_RETRY_DELAYS = [5, 10, 20, 30, 60]`（合計125秒）で伸ばしながら再試行
- **失敗しても例外を投げない**。動画は既に公開されているので、URLを返さないほうが害が大きい。
  サムネイルは後から手で設定できる

## 22. LLM が指示文の主語を落としていた（コード側で強制）★

ログの ARDY 入力を見ると、LLM が書いた motion が
`raises one hand up to her chin, looking thoughtful` のように
**`A woman stands in place` の主語ごと落ちていた**。2026-08-12 の実行でも同じ。
プロンプトには「必ずこの形で始める」と書いてあるが守られていない。

つまり**提案14の「主語を A woman にする」がARDYまで届いていなかった**（届くのは
`her` の代名詞だけ）。禁止語と同じくコード側を最後の砦にする。

`core.normalize_motion_text()` を追加し、`plan_vrma_from_sentences` の入口で
必ず通す（禁止語チェックの直前）。`A person stands in place facing forward.` のような
古い形も含めて先頭を剥がし、`A woman stands in place and ...` に付け替える。

```
'raises one hand up to her chin, looking thoughtful'
  → 'A woman stands in place and raises one hand up to her chin, looking thoughtful'
'A person stands in place and opens both arms out to the sides at chest height.'
  → 'A woman stands in place and opens both arms out to the sides at chest height.'
```

**未対処**: 同じログで `looking thoughtful` / `with a bright smile` /
`shakes her head slightly` も出ている。プロンプトが禁止している表情の描写と、
実測で効かないと分かっている `shakes their head` にあたる。
今回は主語だけをコード側で強制した。表情語の除去は文意を壊しうるので手を付けていない。
