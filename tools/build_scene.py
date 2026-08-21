#!/usr/bin/env python3
"""OBS の配信シーンを obs-websocket 経由で組み立てる。

GUI で手作業するのが本来だが、OBS は :99（モニタの繋がっていない仮想
ディスプレイ）で動かすので画面を直接触れない。ここで組んでおけば再現もできる。

前提:
  - Xorg :99 と openbox が上がっていること（bottan-live-xorg / bottan-live-wm）
  - OBS が :99 で起動していること（`python -c "import obs; obs.launch()"`）
  - Unity が -liveMode で起動していること（ウィンドウキャプチャの対象）

使い方:
  ./venv/bin/python tools/build_scene.py
  ./venv/bin/python tools/build_scene.py --scene 別の名前
"""

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))          # common/
sys.path.insert(0, str(_ROOT / "live"))  # config, motion, unity_client ...

import obsws_python as obsws

import audio
from config import (OBS_HOST, OBS_PORT, OBS_PASSWORD, SUBTITLE_JA, SUBTITLE_EN,
                    COMMENTS_TXT, CLOCK_TXT, LIVE_DISPLAY, OBS_COLLECTION, OBS_PROFILE)

SCENE = "botたんライブ"
CANVAS_W, CANVAS_H = 1920, 1080

# 日本語と欧文が同じ字面で並ぶので CJK 対応フォントを使う。
# IPAGothic だと欧文が間延びする
FONT = "Noto Sans CJK JP"

# text_ft2_source_v2 の色は ABGR の整数（RGB ではない）
WHITE = 0xFFFFFFFF
# 英字幕の色。背景が青系だと水色は埋もれるので、少し暖かい生成り色にする
CREAM = 0xFFD8F0FF   # ABGR なので B=D8 G=F0 R=FF

# OBS のアラインメント定数
ALIGN_CENTER, ALIGN_LEFT, ALIGN_RIGHT, ALIGN_TOP, ALIGN_BOTTOM = 0, 1, 2, 4, 8

# BGM。data/bgm に置いたものを使う（クレジット要否は各素材の規約に従うこと）
BGM = _ROOT / "data" / "bgm" / "ohirusugi.mp3"

# botたんの声を拾う音声ソースの名前。BGM のダッキングもこれを参照する
VOICE_SOURCE = "botたんの声"

# Game View を最大化しても残る Unity Editor の枠のおおよその値。
# 実測に失敗したときの保険にだけ使う
CHROME_TOP, CHROME_BOTTOM = 118, 23


def _is_letterbox(px) -> bool:
    """Unity が Game View の余白に敷く無地のグレーか。"""
    return max(px) < 90 and abs(px[0] - px[1]) < 6 and abs(px[1] - px[2]) < 6


def _run_above(flags, end):
    """flags[end] から上（前）へ False が続く区間の (開始, 長さ)。"""
    i = end
    while i >= 0 and not flags[i]:
        i -= 1
    return i + 1, end - i


def _run_left_of(flags, end):
    return _run_above(flags, end)


def detect_render_rect(win_w: int, win_h: int):
    """:99 を1枚撮って、Game View が実際に描いている矩形を実測する。

    Editor の枠の高さを定数で持つとレイアウトやバージョンでずれる（実測で
    9px ずれ、画面の下端にグレーの帯が出た）。毎回測る。

    Game View は固定解像度の描画を表示領域の中央に置き、余った上下左右を
    無地のグレーで埋める。無地の行は端から端まで同じ色なので、絵の行とは
    区別できる。ただし Editor のツールバーも一様なグレーに見えることが
    あるので、「画面の下端から続く無地の帯」を起点にして、その直上に続く
    絵の区間を描画領域とみなす（上から探すとツールバーを拾ってしまう）。

    戻り値は (left, top, width, height)。測れなければ None。
    """
    shot = Path(tempfile.gettempdir()) / "bottan_live_probe.png"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab",
             "-video_size", f"{win_w}x{win_h}", "-i", LIVE_DISPLAY,
             "-frames:v", "1", str(shot)],
            check=True, capture_output=True, timeout=30)
        from PIL import Image
        im = Image.open(shot).convert("RGB")
    except Exception as e:
        print(f"[シーン] 画面を測れませんでした（推定値で代用します）: {e}")
        return None
    if im.size != (win_w, win_h):
        print(f"[シーン] 画面の大きさが合いません {im.size}（推定値で代用します）")
        return None

    # 画の内側を通る列だけを見る。端まで見るとツールバーの文字を拾う
    xs = [int(win_w * f) for f in (0.4, 0.5, 0.6)]
    rows = []
    for y in range(win_h):
        first = im.getpixel((xs[0], y))
        rows.append(_is_letterbox(first)
                    and all(im.getpixel((x, y)) == first for x in xs[1:]))

    bottom = win_h - 1
    while bottom >= 0 and rows[bottom]:
        bottom -= 1
    if bottom < 0 or bottom == win_h - 1:
        print("[シーン] 下端の余白が見つかりません（推定値で代用します）")
        return None
    top, height = _run_above(rows, bottom)

    mid = top + height // 2
    cols = [_is_letterbox(im.getpixel((x, mid))) for x in range(win_w)]
    right = win_w - 1
    while right >= 0 and cols[right]:
        right -= 1
    left, width = _run_left_of(cols, right)

    shot.unlink(missing_ok=True)
    if height < 100 or width < 100:
        print(f"[シーン] 測った矩形が小さすぎます {width}x{height}（推定値で代用します）")
        return None
    return left, top, width, height


def compute_crop(win_w: int, win_h: int) -> dict:
    """Game View の描画領域だけを切り出す cut_* を求める。

    仮想画面が 2560x1440 なら縮小は起きず、ぴったり 1920x1080 を無変換で
    切り出せる。
    """
    rect = detect_render_rect(win_w, win_h)
    if rect is not None:
        left, top, render_w, render_h = rect
        how = "実測"
    else:
        area_h = win_h - CHROME_TOP - CHROME_BOTTOM
        scale = min(1.0, win_w / CANVAS_W, area_h / CANVAS_H)
        render_w = round(CANVAS_W * scale)
        render_h = round(CANVAS_H * scale)
        left = (win_w - render_w) // 2
        top = CHROME_TOP + (area_h - render_h) // 2
        how = "推定"

    crop = {
        "cut_left": left,
        "cut_right": win_w - render_w - left,
        "cut_top": top,
        "cut_bot": win_h - render_h - top,
    }
    note = ("等倍" if render_h >= CANVAS_H
            else f"{render_h / CANVAS_H:.2f}倍に縮小されたものを拡大し直します")
    print(f"[シーン] Unity のウィンドウ {win_w}x{win_h} → 描画領域 "
          f"{render_w}x{render_h} @ ({left},{top})（{note} / {how}）")
    if render_h < CANVAS_H:
        print("[シーン] 仮想画面が狭いです。setup/xorg-bottan-live.conf の "
              "Virtual を 2560x1440 にして bottan-live-xorg を再起動すると等倍になります")
    return crop


def text_settings(path: Path, size: int, color: int = WHITE,
                  width: int = 0, wrap: bool = True) -> dict:
    return {
        "font": {"face": FONT, "flags": 0, "size": size, "style": "Bold"},
        "text_file": str(path),
        "from_file": True,          # Python 側が os.replace でアトミックに差し替える
        "word_wrap": wrap,
        "custom_width": width,
        "color1": color,
        "color2": color,
        "outline": True,
        "drop_shadow": True,
        "log_mode": False,
    }


def x_unity_window() -> tuple:
    """X から Unity のクライアントウィンドウを直接探す。

    OBS に空の xcomposite ソースを作らせて一覧を取る手もあるが、OBS 32.2.2 は
    capture_window が空のまま作られると worker thread で例外を投げて落ちる
    （ログに "Qt Concurrent has caught an exception"）。作る前から正しい窓を
    指定できるよう、こちらで探しておく。

    openbox が窓を frame に入れ子にするので `xwininfo -root -children` では
    クライアント窓が見えない。EWMH の _NET_CLIENT_LIST を使う。
    """
    ids = subprocess.run(["xprop", "-display", LIVE_DISPLAY, "-root", "_NET_CLIENT_LIST"],
                         capture_output=True, text=True, check=True).stdout
    for hex_id in re.findall(r"0x[0-9a-f]+", ids):
        props = subprocess.run(["xprop", "-display", LIVE_DISPLAY, "-id", hex_id,
                                "WM_CLASS", "_NET_WM_NAME"],
                               capture_output=True, text=True).stdout
        cls = re.search(r'WM_CLASS\(STRING\) = "([^"]*)", "([^"]*)"', props)
        name = re.search(r'_NET_WM_NAME\(UTF8_STRING\) = "(.*)"', props)
        if not cls or cls.group(2) != "Unity":
            continue
        geo = subprocess.run(["xwininfo", "-display", LIVE_DISPLAY, "-id", hex_id],
                             capture_output=True, text=True, check=True).stdout
        w = int(re.search(r"Width:\s+(\d+)", geo).group(1))
        h = int(re.search(r"Height:\s+(\d+)", geo).group(1))
        if w < 640 or h < 480:
            continue   # Unity は 1x1 や 10x10 のダミー窓もぶら下げている
        title = name.group(1) if name else ""
        value = f"{int(hex_id, 16)}\r\n{title}\r\n{cls.group(2)}"
        print(f"[シーン] Unity のウィンドウ: {hex_id} {w}x{h} {title[:60]}")
        return value, w, h

    raise SystemExit(
        "Unity のウィンドウが " + LIVE_DISPLAY + " に見つかりません。\n"
        "  -liveMode の Unity を起動してから実行してください"
    )


def reconcile_window(client, source: str, want_title: str) -> None:
    """OBS が自分で列挙した capture_window の値に合わせ直す。

    窓IDの表し方が OBS の実装依存なので、こちらで組み立てた文字列とずれることが
    ある。ソースを作ったあとなら一覧が引けるので、同じ窓を指す OBS 側の
    表記に置き換えておく。
    """
    try:
        items = client.get_input_properties_list_property_items(
            source, "capture_window").property_items
    except Exception as e:
        print(f"[シーン] ウィンドウ一覧を取れませんでした（そのまま使います）: {e}")
        return
    for it in items:
        parts = it["itemValue"].split("\r\n")
        if len(parts) == 3 and parts[2] == "Unity" and parts[1] == want_title:
            settings = client.get_input_settings(source).input_settings
            if settings.get("capture_window") != it["itemValue"]:
                client.set_input_settings(source, {"capture_window": it["itemValue"]}, True)
                print("[シーン] capture_window を OBS 側の表記に合わせました")
            return
    print("[シーン] OBS のウィンドウ一覧に Unity が見当たりません（設定はそのまま）")


def ensure_collection(client) -> None:
    """配信用のシーンコレクションとプロファイルへ切り替える（無ければ作る）。

    デスクトップで普段使いする OBS と設定を共有していると、:0 側で開いた
    コレクションのまま配信が始まったり、:0 の OBS を終了した時点で配信用の
    シーンが上書きされたりする。名前で分けておけば干渉しない。
    """
    profiles = client.get_profile_list()
    if OBS_PROFILE not in profiles.profiles:
        client.create_profile(OBS_PROFILE)
        print(f"[シーン] プロファイル「{OBS_PROFILE}」を作りました")
    elif profiles.current_profile_name != OBS_PROFILE:
        client.set_current_profile(OBS_PROFILE)
    time.sleep(1.0)

    collections = client.get_scene_collection_list()
    if OBS_COLLECTION not in collections.scene_collections:
        client.create_scene_collection(OBS_COLLECTION)
        print(f"[シーン] シーンコレクション「{OBS_COLLECTION}」を作りました")
    elif collections.current_scene_collection_name != OBS_COLLECTION:
        client.set_current_scene_collection(OBS_COLLECTION)
    # 切り替えは OBS 側で非同期に走る。直後に作りにいくと取りこぼす
    time.sleep(2.0)
    print(f"[シーン] コレクション: {OBS_COLLECTION} / プロファイル: {OBS_PROFILE}")


def place(client, scene: str, name: str, x: int, y: int,
          align: int = ALIGN_CENTER, bounds: tuple | None = None) -> None:
    item_id = client.get_scene_item_id(scene, name).scene_item_id
    tf = {"positionX": float(x), "positionY": float(y), "alignment": align}
    if bounds:
        tf["boundsType"] = "OBS_BOUNDS_SCALE_INNER"
        tf["boundsWidth"] = float(bounds[0])
        tf["boundsHeight"] = float(bounds[1])
        tf["boundsAlignment"] = align
    client.set_scene_item_transform(scene, item_id, tf)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default=SCENE)
    args = ap.parse_args()
    scene = args.scene

    c = obsws.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=15)
    print(f"[シーン] OBS {c.get_version().obs_version} に接続しました")

    ensure_collection(c)

    # キャンバスを配信解像度に合わせる
    c.set_video_settings(30, 1, CANVAS_W, CANVAS_H, CANVAS_W, CANVAS_H)
    print(f"[シーン] キャンバス {CANVAS_W}x{CANVAS_H} @30fps")

    window, win_w, win_h = x_unity_window()

    # 作り直せるように、同名のシーンがあれば消す
    existing = [s["sceneName"] for s in c.get_scene_list().scenes]
    if scene in existing:
        if len(existing) == 1:
            c.create_scene("__tmp")
            c.set_current_program_scene("__tmp")
        c.remove_scene(scene)
    c.create_scene(scene)
    c.set_current_program_scene(scene)

    # ── 最背面: Unity の Game View ──
    crop = compute_crop(win_w, win_h)
    c.create_input(scene, "botたん", "xcomposite_input",
                   {"capture_window": window, "show_cursor": False, **crop}, True)
    reconcile_window(c, "botたん", window.split("\r\n")[1])
    place(c, scene, "botたん", CANVAS_W // 2, CANVAS_H // 2,
          ALIGN_CENTER, bounds=(CANVAS_W, CANVAS_H))

    # ── コメント欄（右上）──
    c.create_input(scene, "コメント欄", "text_ft2_source_v2",
                   text_settings(COMMENTS_TXT, 26, WHITE, width=460), True)
    place(c, scene, "コメント欄", 1880, 120, ALIGN_TOP | ALIGN_RIGHT)

    # ── 字幕（下・日本語が上、英語が下）──
    # 下端を基準に置く。上基準だと2行になったときに英語が画面外へ出る
    c.create_input(scene, "字幕(日)", "text_ft2_source_v2",
                   text_settings(SUBTITLE_JA, 52, WHITE, width=1500), True)
    place(c, scene, "字幕(日)", CANVAS_W // 2, 940, ALIGN_BOTTOM | ALIGN_CENTER)

    c.create_input(scene, "字幕(英)", "text_ft2_source_v2",
                   text_settings(SUBTITLE_EN, 34, CREAM, width=1500), True)
    place(c, scene, "字幕(英)", CANVAS_W // 2, 1055, ALIGN_BOTTOM | ALIGN_CENTER)

    # ── 時計（右下）──
    c.create_input(scene, "時計", "text_ft2_source_v2",
                   text_settings(CLOCK_TXT, 36, WHITE, wrap=False), True)
    place(c, scene, "時計", 1880, 1040, ALIGN_BOTTOM | ALIGN_RIGHT)

    # ── energy メーター（左下）──
    # biorhythm_server が落ちていても空で出るだけ。配信は止めない
    c.create_input(scene, "energy", "browser_source",
                   {"url": "http://localhost:3002/image.png",
                    "width": 320, "height": 80,
                    "reroute_audio": False, "restart_when_active": True,
                    "shutdown": False, "fps_custom": True, "fps": 1}, True)
    place(c, scene, "energy", 40, 1040, ALIGN_BOTTOM | ALIGN_LEFT)

    # ── botたんの声 ──
    # Unity の音は OS のサウンドサーバを経由しないと OBS に届かない。
    # BGM は OBS の中で完結しているので、この音声ソースが無いと
    # 「BGM は鳴るのに声だけ出ない」状態になる（初回の配信で発生した）
    monitor = audio.monitor_name()
    try:
        audio.ensure_sink()
    except Exception as e:
        print(f"[シーン] 音声シンクを用意できません: {e}")
    c.create_input(scene, VOICE_SOURCE, "pulse_input_capture",
                   {"device_id": monitor}, True)
    print(f"[シーン] 声の取り込み: {monitor}")

    # ── BGM ──
    if BGM.exists():
        c.create_input(scene, "BGM", "ffmpeg_source",
                       {"local_file": str(BGM), "looping": True,
                        "is_local_file": True, "restart_on_activate": False,
                        "close_when_inactive": False}, True)
        # 0dB のままだと botたんの声を食う。まずは控えめから
        client_volume = -20.0
        c.set_input_volume("BGM", vol_db=client_volume)
        # ダッキング。botたんが喋っている間だけ BGM を沈める。
        # サイドチェインの入力に声のソースを指定したコンプレッサーで作る
        try:
            c.create_source_filter("BGM", "ダッキング", "compressor_filter", {
                "ratio": 12.0,
                "threshold": -32.0,
                "attack_time": 6,
                "release_time": 300,
                "output_gain": 0.0,
                "sidechain_source": VOICE_SOURCE,
            })
            print("[シーン] BGM にダッキングをかけました（サイドチェイン: "
                  f"{VOICE_SOURCE}）")
        except Exception as e:
            print(f"[シーン] ダッキングを設定できません（音量だけで運用します）: {e}")
        print(f"[シーン] BGM: {BGM.name}（{client_volume:.0f}dB）")
    else:
        print(f"[シーン] BGM が見つかりません: {BGM}")

    print(f"[シーン] 「{scene}」を組みました:")
    for it in c.get_scene_item_list(scene).scene_items:
        print(f"  - {it['sourceName']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
