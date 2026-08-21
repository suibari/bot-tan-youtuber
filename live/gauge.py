"""energy ゲージの書き出し。

OBS の「energy」ブラウザソース（320x80・画面左下）に file:// で読ませる HTML を書く。
数値の出どころは energy.get_energy()（biorhythm_server → 落ちていれば DB）。

ブラウザソースはローカルファイルの変更を自前では監視しないので、HTML の側に
「一定時間後に自分を読み直す」スクリプトを埋めてある。energy は分単位でしか
動かないため、この粗さで十分。

字幕と同じく、OBS が書き込み途中の中途半端な HTML を読むことがあるので
subtitle._write_atomic で差し替える。
"""

from config import ENERGY_HTML, ENERGY_REFRESH_SEC
from subtitle import _write_atomic

# ゲージの色。energy が低いほど冷たい色にして、上がると暖かくなる。
# (しきい値, 明るい側, 暗い側) を大きい順に並べる
_COLORS = [
    (70.0, "#ffd45e", "#ff9f43"),   # 元気
    (35.0, "#7ee787", "#39c46e"),   # ふつう
    (0.0,  "#79c0ff", "#3b82f6"),   # おねむ
]


def _colors_for(energy: float) -> tuple:
    for threshold, light, dark in _COLORS:
        if energy >= threshold:
            return light, dark
    return _COLORS[-1][1], _COLORS[-1][2]


def render(energy: float) -> str:
    """ゲージの HTML を作る。テストしやすいよう書き出しとは分けてある。"""
    energy = max(0.0, min(100.0, float(energy)))
    light, dark = _colors_for(energy)
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>energy</title>
<style>
  html, body {{
    margin: 0; padding: 0; background: transparent; overflow: hidden;
    width: 320px; height: 80px;
    font-family: "Noto Sans JP", "Yu Gothic UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  /* 背景の明暗に関わらず読めるように、半透明のパネルを敷く。
     Unity の背景は日替わりで、明るい空の写真が来ることもある */
  .wrap {{
    margin: 4px; padding: 7px 12px 9px;
    border-radius: 12px;
    background: rgba(12, 16, 26, .42);
    backdrop-filter: blur(3px);
    box-shadow: 0 2px 10px rgba(0,0,0,.28);
  }}
  .head {{
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 6px;
  }}
  .label {{
    font-size: 17px; font-weight: 700; letter-spacing: .16em; color: #fff;
    text-shadow: 0 2px 4px rgba(0,0,0,.75), 0 0 2px rgba(0,0,0,.9);
  }}
  .value {{
    font-size: 24px; font-weight: 800; color: #fff; line-height: 1;
    text-shadow: 0 2px 4px rgba(0,0,0,.75), 0 0 2px rgba(0,0,0,.9);
  }}
  .value small {{ font-size: 13px; font-weight: 700; opacity: .85; margin-left: 1px; }}
  .track {{
    position: relative; height: 22px; border-radius: 11px;
    background: rgba(0,0,0,.45);
    box-shadow: inset 0 1px 3px rgba(0,0,0,.6), 0 1px 0 rgba(255,255,255,.12);
    overflow: hidden;
  }}
  .fill {{
    height: 100%; width: {energy:.1f}%;
    border-radius: 11px;
    background: linear-gradient(180deg, {light} 0%, {dark} 100%);
    box-shadow: 0 0 10px {dark}88;
  }}
  /* 目盛り。25% ごとの薄い線 */
  .ticks {{ position: absolute; inset: 0; }}
  .ticks i {{
    position: absolute; top: 0; bottom: 0; width: 1px;
    background: rgba(255,255,255,.22);
  }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <span class="label">ENERGY</span>
      <span class="value">{energy:.0f}<small>%</small></span>
    </div>
    <div class="track">
      <div class="fill"></div>
      <div class="ticks"><i style="left:25%"></i><i style="left:50%"></i><i style="left:75%"></i></div>
    </div>
  </div>
<script>
  // live 側が書き換えた HTML を読み直す。ブラウザソースはファイル監視をしない。
  setTimeout(function () {{ location.reload(); }}, {ENERGY_REFRESH_SEC * 1000});
</script>
</body>
</html>
"""


def write(energy: float) -> None:
    """ゲージを更新する。落ちても配信に影響させない（呼び出し側で握る）。"""
    _write_atomic(ENERGY_HTML, render(energy))
