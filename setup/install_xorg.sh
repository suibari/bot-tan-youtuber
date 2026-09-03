#!/usr/bin/env bash
# botたんライブ配信用の GPU 仮想ディスプレイをインストールする。
#   sudo bash setup/install_xorg.sh
#
# 既存のデスクトップ（GDM が管理する :0）には一切触らない。
# /etc/X11/xorg.conf.d/ ではなく単独のファイルを置き、Xorg -config で読ませる。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root で実行してください: sudo bash $0" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# GPU の BusID が設定ファイルと合っているか確かめる。合っていないと
# "No devices detected" で起動に失敗する
BUS=$(lspci | grep -i 'vga.*nvidia' | cut -d' ' -f1 | head -1)
if [ -z "$BUS" ]; then
    echo "NVIDIA GPU が見つかりません" >&2
    exit 1
fi
EXPECT="PCI:$(echo "$BUS" | awk -F'[:.]' '{printf "%d:%d:%d", "0x"$1, "0x"$2, $3}')"
if ! grep -q "\"$EXPECT\"" "$HERE/xorg-bottan-live.conf"; then
    echo "警告: 検出した BusID ($EXPECT) が設定ファイルと一致しません。" >&2
    echo "      xorg-bottan-live.conf の BusID を書き換えてください。" >&2
    exit 1
fi

if [ ! -f /usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so ]; then
    echo "NVIDIA の X ドライバが見つかりません。" >&2
    echo "  sudo apt install nvidia-driver-595  などで導入してください。" >&2
    exit 1
fi

# NoScanout (UseDisplayDevice=None) では NVIDIA 595.84 + RTX 5070 Ti の
# Unity Editor が約1fpsになり、OpenGL present 中に abort した。接続中の実モニタから
# EDIDを保存し、xorg-bottan-live.conf が未使用端子 DFP-1 のダミー表示として読む。
# /sys の card番号は起動ごとに変わり得るので決め打ちしない。
EDID_SOURCE=""
for STATUS_FILE in /sys/class/drm/card*-*/status; do
    [ -r "$STATUS_FILE" ] || continue
    [ "$(tr -d '\n' < "$STATUS_FILE")" = "connected" ] || continue
    CANDIDATE="$(dirname "$STATUS_FILE")/edid"
    [ -r "$CANDIDATE" ] || continue
    EDID_SIZE="$(wc -c < "$CANDIDATE")"
    if [ "$EDID_SIZE" -ge 128 ]; then
        EDID_SOURCE="$CANDIDATE"
        break
    fi
done
if [ -z "$EDID_SOURCE" ]; then
    echo "接続中モニターのEDIDを取得できません。モニターを接続して再実行してください。" >&2
    exit 1
fi
install -m 0644 "$EDID_SOURCE" /etc/X11/bottan-live.edid
echo "EDIDを保存しました: $EDID_SOURCE -> /etc/X11/bottan-live.edid"

install -m 0644 "$HERE/xorg-bottan-live.conf" /etc/X11/xorg-bottan-live.conf
install -m 0644 "$HERE/bottan-live-xorg.service" /etc/systemd/system/bottan-live-xorg.service
systemctl daemon-reload
systemctl enable bottan-live-xorg.service
# enable --now は「すでに動いていれば何もしない」ので、設定を書き換えても
# 反映されない（Virtual を 2560x1440 にしたのに 1920x1080 のままになった）。
# 明示的に再起動する。:99 上の Unity と OBS は道連れになるので上げ直すこと
systemctl restart bottan-live-xorg.service

echo "起動を待っています..."
for i in $(seq 1 20); do
    if [ -e /tmp/.X11-unix/X99 ]; then
        echo "OK: DISPLAY=:99 が使えます"
        DISPLAY=:99 glxinfo -B 2>/dev/null | grep -i "renderer" || true

        # ソケットがあっても CurrentMetaMode=NULL の NoScanout では表示クロックがなく、
        # Unity Editor は約1fpsになる。実際に現在モードへ * が付いていることを確認する。
        if ! DISPLAY=:99 xrandr --current | grep -Eq '[0-9]+\.[0-9]+\*'; then
            echo "DISPLAY=:99 に有効なリフレッシュレートがありません。" >&2
            echo "Xorgログで CustomEDID / MetaModes を確認してください。" >&2
            exit 1
        fi
        DISPLAY=:99 xrandr --current | grep -E '[0-9]+x[0-9]+.*\*' | head -1

        # ウィンドウマネージャ。OBS の XComposite ウィンドウキャプチャは
        # EWMH 準拠の WM が居ないとプラグインごと無効化される（ソース追加の
        # プルダウンに「ウィンドウキャプチャ」が出てこなくなる）ので必須。
        if ! command -v openbox >/dev/null 2>&1; then
            echo "openbox がありません。apt install openbox を実行してください" >&2
            exit 1
        fi
        install -m 0644 "$HERE/openbox-rc.xml" /etc/X11/openbox-bottan-live.xml
        sed "s|__USER__|${SUDO_USER:-$USER}|" "$HERE/bottan-live-wm.service" \
            > /etc/systemd/system/bottan-live-wm.service
        chmod 0644 /etc/systemd/system/bottan-live-wm.service
        systemctl daemon-reload
        systemctl enable bottan-live-wm.service
        # X を上げ直したので、古いウィンドウマネージャが残っていれば道連れに
        # なっている。残っていると "ウィンドウマネージャが既に起動しています" で
        # 起動できないため、先に確実に止める
        systemctl stop bottan-live-wm.service 2>/dev/null || true
        pkill -f "openbox .*openbox-bottan-live" 2>/dev/null || true
        sleep 1
        systemctl start bottan-live-wm.service
        sleep 2
        systemctl is-active --quiet bottan-live-wm.service \
            && echo "OK: :99 にウィンドウマネージャが載りました" \
            || { echo "openbox の起動に失敗しました: journalctl -u bottan-live-wm -n 30" >&2; exit 1; }
        exit 0
    fi
    sleep 1
done

echo "起動できませんでした。ログを確認してください:" >&2
echo "  journalctl -u bottan-live-xorg -n 50" >&2
echo "  /var/log/Xorg.99.log" >&2
exit 1
