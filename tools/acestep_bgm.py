#!/usr/bin/env python3
"""ACE-Step でインスト曲を1曲作る。common/bgm.py から呼ばれる。

**このリポジトリの venv では動かない。** ACE-Step 専用の venv の python で、
cwd を ACE-Step のディレクトリ（checkpoints/ がある場所）にして実行する:

    cd ~/work/acestep-poc && CUDA_VISIBLE_DEVICES=<5070 Ti の UUID> \\
      .venv/bin/python ~/work/bot-tan-youtuber/tools/acestep_bgm.py \\
      --caption "A light lo-fi hip hop instrumental ..." --duration 50 --out /tmp/bgm.wav
"""

import argparse
import os
import shutil
import sys
import tempfile


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--caption", required=True)
    p.add_argument("--duration", type=float, default=50)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    root = os.getcwd()
    sys.path.insert(0, root)
    from acestep.handler import AceStepHandler
    from acestep.llm_inference import LLMHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    dit = AceStepHandler()
    # 載らなかったときも例外にならず (メッセージ, False) が返る
    msg, ok = dit.initialize_service(project_root=root, config_path="acestep-v15-turbo",
                                     device="cuda", offload_to_cpu=True)
    if not ok:
        print(f"[acestep] 読み込みに失敗: {msg[:500]}", file=sys.stderr)
        return 1

    params = GenerationParams(
        caption=args.caption, lyrics="[Instrumental]", instrumental=True,
        duration=args.duration, shift=3.0,
        # 5Hz LM は使わない（曲調は caption で決め打ちする）
        thinking=False, use_cot_metas=False, use_cot_caption=False, use_cot_language=False,
    )
    with tempfile.TemporaryDirectory() as tmp:
        res = generate_music(dit, LLMHandler(), params,
                             GenerationConfig(batch_size=1, audio_format="wav"), save_dir=tmp)
        if not res.success or not res.audios:
            print(f"[acestep] 生成に失敗: {res.error}", file=sys.stderr)
            return 1
        shutil.move(res.audios[0]["path"], args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
