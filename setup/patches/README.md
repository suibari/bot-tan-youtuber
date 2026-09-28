# 音声優先のGPU制御に必要な外部変更

- `irodori-oom.patch`: `~/work/bot-tan-tts` の `server.py` に適用する。
  loading / insufficient_vram / cuda_oom をHTTPエラーのdetail.codeで返し、
  OOM後のモデル状態を破棄する。バックグラウンド読み込みの失敗も次の要求へ伝える。
- `ardy-peft-cpu.patch`: `ARDY_ENGINE_ROOT/ardy` のソースへ適用する。
  PEFTアダプタ読み込みにも `TEXT_ENCODER_DEVICE` を渡す。

適用先で `git apply --check <patch>` の後、`git apply <patch>` を実行する。
適用済みの場合は再適用しない。Irodoriは配信・合成中を避けてサービスを再起動する。
ARDYは次のプロセス起動から有効になる。

検証用 `verify_sidecars.py` は Irodori の venv で実行する。
`TTS_SERVER_SOURCE` に server.py、`ARDY_ENCODER_SOURCE` に llm2vec.py の絶対パスを指定する。
`PYTHONPATH` には Irodori の vendor/Irodori-TTS を指定する。
実モデルを読み込まず、エラー応答・OOM後の破棄・PEFTへのCPU指定を確認する。
