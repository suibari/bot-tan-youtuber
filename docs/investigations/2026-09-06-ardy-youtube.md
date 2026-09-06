# 2026-09-06 ARDY GPU化・YouTubeクォータ調査

## 実施範囲

- ユーザー指定に従い22:00 JSTまでは読み取り調査のみ。
- 配信ログは21:55:52に正常終了。22:00以降に独立したARDY検証を実施。
- 本番コード・設定・venv・サービスは変更していない。依存の追加は `/tmp/bottan-ardy-investigation-deps` のみ。
- 検証スクリプト: `/tmp/bottan_ardy_probe.py`。既存のARDY実装を読み込み、プロセス内で検証用エンコーダを渡す。HTTPサーバーは立てない。
- GPU使用上限はPyTorchのper-process memory fraction 0.40。Gemmaをアンロードしない。

## 今夜のARDY

本番ログ `/home/suibari/work/bot-tan-youtuber/logs/live_20260906_204002.log` の39〜43行:

- `stage=ardy progress=0.82` まで到達したが600秒でタイムアウト。
- サーバーを停止し、既存モーションプールのみで配信を継続。
- 今夜は「CPUで新規モーション生成が遅い」以前に、新規生成自体が停止していた。
- 同日朝のquizログでは `device=cuda:0` で正常起動し、約16秒の6セグメントを28.2秒で生成している。異なる負荷・入力のため今回の速度比較の基準にはしない。

`common/ardy.py` の `TEXT_ENCODER_DEVICE=cpu` は8BテキストエンコーダだけをCPUへ固定する。ARDYの拡散モデル本体はCUDAが利用可能ならGPUを選ぶ。

## 容量と4bit検証

- RTX 5070 Ti 16,303MiB。
- 21:21のGPU使用量8,892MiB。ただしARDYは停止中。
- 本番のGemmaは `hf.co/unsloth/gemma-4-12B-it-qat-GGUF:UD-Q4_K_XL`、コンテキスト32768。
- 作業用devの `.env` は26Bが残っている。今後ベンチマークや起動処理を使う際は本番と混同しないこと。
- safetensorsヘッダーによる実重量はBF16で13.979GiB（15,009,849,344 bytes）。無量子化でGPUへ移す案は同居不可。
- Linear重みのみ4bit、その他BF16とした最低ペイロードは4.229GiB。8bitでは7.479GiB。アダプタ・量子化メタデータ・作業領域は別。
- `/tmp` にbitsandbytes 0.50.2を追加。既存環境はtorch 2.11.0+cu128 / transformers 5.8.1 / peft 0.20.0 / accelerate 1.14.0。
- NF4 + double quantization + BF16 compute、既存のsupervisedアダプタを維持。エンコーダとARDY本体のGPUロード、3本の生成に成功。
- エンコーダ読み込み195.6秒、本体読み込み136.5秒、計336.6秒（最初のPython import時間は含めない）。
- GPU allocated 5,481MiB / reserved 5,656MiB / peak allocated 5,493MiB。
- テキスト単独の3件は10.509 / 0.608 / 0.324秒。初回はウォームアップ等を含む。
- 同一3プロンプト×各3秒、seed 101 / 202 / 303、CFG 3.0、blend 0.7、arm spread 8.0で生成。出力8.95秒、20ボーン。
- 生成時間10.421 / 0.860 / 0.862秒。初回と定常時を分ける。配信中のGemma推論・Unity描画との同時負荷はまだ再現していない。

GPU化だけで起動停滞を解消できるとは限らない。検証中のスタックは、本体構築中の `pydantic_core` importで待機していた。ホストではI/O PSIが高く、プロセスの待機場所は `folio_wait_bit_common`。他のLLMプロセスも稼働していた。これは今回の検証の観測であり、今夜のタイムアウト原因を断定する証拠ではない。

### CPUとの同条件比較（完了）

| 項目 | 現行CPUエンコーダ | NF4 GPUエンコーダ |
|---|---:|---:|
| seed 101、初回生成 | 9.456秒 | 10.421秒 |
| seed 202 | 8.030秒 | 0.860秒 |
| seed 303 | 9.096秒 | 0.862秒 |
| GPU予約メモリ（ARDYプロセス、PyTorch分） | 908MiB | 5,656MiB |
| GPU peak allocated | 886MiB | 5,493MiB |

後半2本の平均では9.94倍の速度差。ただし同一の短い3プロンプト・3seedという小規模検証で、Gemmaへの新規推論負荷やUnityの描画は検証スクリプトから発生させていない。実配信の応答時間全体が10倍になる意味ではない。

CPUの起動は116.7秒で成功し、600秒タイムアウトは再現しなかった。4bit検証直後でディスクキャッシュなどの条件が異なるので、起動時間の単純比較はしない。CPU版の読み込み中スタックではTransformersの `_initialize_missing_keys` → `normal_` が観測された。詳細な初期化対象とI/Oの調査は未実施。

出力検証:

- CPU/GPUとも3本すべて8.95秒・180フレーム・20ボーンで生成成功。
- GPU出力の全ボーン回転は有限値。
- 同一入力の4096次元テキスト特徴量のコサイン類似度は0.9858 / 0.9833 / 0.9845。
- 同一seedでのボーン姿勢差を、出力仕様のintrinsic XYZから相対回転角に変換して比較。全フレーム・ボーン平均は7.05 / 8.76 / 6.69度、最大86.97 / 110.05 / 125.62度。
- 特徴量が近くても拡散生成の最終動作には差がある。これは品質劣化の証明ではないが、品質同等を保証するものでもない。Unityでの目視確認が必要。
- 数値結果: `/tmp/bottan_ardy_cpu.json`, `/tmp/bottan_ardy_4bit.json`。各seedのspecは `/tmp/bottan_ardy_{cpu,4bit}_{101,202,303}.json`。

結論: **4bit GPU化は実機で動作し、定常時の速度改善も確認できた。導入候補として有望。現時点では本番へ切り替えていない。**

## YouTube

- ログのquotaExceededは22行（チャット21、終了transition 1）。
- 最初のエラー前に39コメント、終了時に51コメントを受信。エラー後も成功した取得があるため、最初の403で翌日まで停止する設計は不適切。
- 現行 `live/chat.py` は `pollingIntervalMillis` を守っているが、最低間隔は1秒、既定5秒。全エラー共通で10〜120秒の指数バックオフ。
- 成功時の要求回数・実際のpollingInterval・時刻が記録されておらず、ログのみで消費量は復元できない。
- 終了の `liveBroadcasts.transition(complete)` も失敗。OBSの送信停止は成功している。配信枠作成コードは `enableAutoStop=True` だが、今夜の枠の実設定・YouTube側の最終状態は未照会。
- Cloud管理用認証は見つからず、ユーザーに対象プロジェクトのクォータ項目名・使用量・上限を確認中。

### 対策候補

1. Cloud Consoleで日次・時間当たり・ユーザー当たりのどの上限か特定し、同一プロジェクトの他クライアントも確認。
2. チャットの取得回数、成功/失敗、待機間隔、エラーreason、受信数のメトリクスを追加。クォータ値や要求コストを推測で固定しない。
3. 公式 `streamList` に移行。インストール済みgoogle-api-python-client 2.197.0のdiscovery定義にstreamListはなく、公式gRPCクライアントを別途組み込む案。
4. 既存OAuth認証を利用し、アクセストークン更新・再接続・nextPageToken継承・停止時キャンセル・重複防止を実装する必要がある。
5. 公式protoのイベント定義と現行の削除イベント処理の対応を確認する。受信方式だけを変えて削除同期を失わないこと。
6. 日次枯渇と一時的制限を分ける。quotaExceeded単独では翌日停止を決めず、Cloudの確認と整合した再試行方針にする。
7. 効率化後も必要なら監査・増枠申請。プロジェクト追加による制限回避は行わない。

### 公式資料

- [streamList仕様](https://developers.google.com/youtube/v3/live/docs/liveChatMessages/streamList)
- [gRPCのPython例とproto](https://developers.google.com/youtube/v3/live/streaming-live-chat)
- [クォータ](https://developers.google.com/youtube/v3/determine_quota_cost): 日次リセットはPT午前0時。9月はJST16時。
- [監査・増枠](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits)
- [bitsandbytes対応環境](https://huggingface.co/docs/bitsandbytes/main/en/installation)

## 導入前に残る確認

- 入力種類を増やしたCPU/GPU比較（今回の3プロンプト・3seedの範囲を超える一般性の確認）。
- 量子化後の動作品質をUnityで目視確認。数値上の差だけで品質同等とは判断しない。
- 配信相当のGemma推論・Unity・OBSと同時に動かし、VRAMピークとLLM応答遅延を確認。
- 4bit用loaderは既存の一律 `.to(device, dtype)` と整合するように分岐する必要がある。本検証は事前構築したエンコーダを渡してこれを避けている。
- 起動段階ごとの所要時間・スタック・I/Oの観測を追加し、600秒の起動失敗原因を再現して特定。
- YouTube側はクォータ実値と、次の実チャットでの受信・再接続・削除同期を確認。
