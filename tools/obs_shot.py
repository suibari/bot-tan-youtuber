#!/usr/bin/env python3
"""OBS の出力を PNG に落とす。:99 は誰も見られないので目視確認用。

  ./venv/bin/python tools/obs_shot.py out.png
  ./venv/bin/python tools/obs_shot.py out.png --source "botたん"
"""
import argparse
import base64
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # common/
sys.path.insert(0, str(_ROOT / "live"))  # config, motion, unity_client ...

import obsws_python as obsws

from config import OBS_HOST, OBS_PORT, OBS_PASSWORD

ap = argparse.ArgumentParser()
ap.add_argument("out", type=Path)
ap.add_argument("--source", default=None, help="既定は現在のシーン全体")
a = ap.parse_args()

c = obsws.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=20)
name = a.source or c.get_scene_list().current_program_scene_name
r = c.get_source_screenshot(name, "png", 1920, 1080, -1)
a.out.write_bytes(base64.b64decode(r.image_data.split(",", 1)[1]))
print(f"{name} → {a.out}（{a.out.stat().st_size} bytes）")
