#!/usr/bin/env python3
"""配信の背景に使うフリー素材を Unity に入れる。

Unity のアセットとして取り込むのではなく、`Assets/background_live.jpg` という
決め打ちのファイルを LiveStage.cs が実行時に読む。差し替えはこのコマンドだけで
済み、Unity を開く必要はない。

  ./venv/bin/python tools/install_background.py ~/ダウンロード/room_night.jpg \
      --credit "作者名 / 配布元URL"

クレジットは data/background_credit.txt に残す。要クレジットの素材を使うときは
配信の概要欄に転記すること。
"""

import argparse
import shutil
import sys
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # common/
sys.path.insert(0, str(_ROOT / "live"))  # config, motion, unity_client ...

TARGET = Path("/home/suibari/bottan-video-dev/Assets/background_live.jpg")
CREDIT = _ROOT / "data" / "background_credit.txt"
W, H = 1920, 1080


def fit(src: Path) -> Image.Image:
    """16:9 に合わせる。歪ませず、はみ出すぶんは中央基準で切り落とす。"""
    im = Image.open(src).convert("RGB")
    sw, sh = im.size
    scale = max(W / sw, H / sh)
    nw, nh = round(sw * scale), round(sh * scale)
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - W) // 2, (nh - H) // 2
    return im.crop((left, top, left + W, top + H))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--credit", default="", help="概要欄に載せるクレジット")
    ap.add_argument("--no-crop", action="store_true",
                    help="切り抜かずそのまま入れる（すでに 1920x1080 の場合）")
    a = ap.parse_args()

    if not a.image.exists():
        print(f"ありません: {a.image}")
        return 1

    if a.no_crop:
        shutil.copy(a.image, TARGET)
        size = Image.open(TARGET).size
    else:
        im = fit(a.image)
        im.save(TARGET, quality=92)
        size = im.size
    print(f"[背景] {TARGET}（{size[0]}x{size[1]}）")

    CREDIT.parent.mkdir(parents=True, exist_ok=True)
    if a.credit:
        CREDIT.write_text(f"{a.credit}\n", encoding="utf-8")
        print(f"[背景] クレジットを控えました: {CREDIT}")
    elif CREDIT.exists():
        print(f"[背景] 前のクレジットが残っています。要らなければ消してください: {CREDIT}")

    print("[背景] Unity を再起動すると反映されます（LiveStage が実行時に読みます）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
