#!/usr/bin/env bash
# 配信用の GPU 仮想ディスプレイ (:99) を作り直し、**実際に描けること**を確かめる。
# bottan-live-display.service が root で呼ぶ。単体でも動く: sudo bash setup/reset_display.sh
#
# **なぜ毎晩作り直すのか。ここがこのスクリプトの存在理由。**
#   この PC の GPU は1枚きりで、デスクトップの Xorg (:0) と :99 が同居している。
#   そして :99 は **gdm より後に起動していないと描画を締め出される**。GPU が
#   空いていても 1.0fps に張り付く。2026-09-11 の実測:
#
#     gdm 停止                          56,142fps
#     gdm 起動・:99 は gdm より前に起動      1.0fps  ← GPU 使用率 0% でもこうなる
#     gdm 起動・:99 を gdm の後に作り直す   59,485fps
#
#   起動順は保証できない（bottan-live-xorg も gdm も multi-user.target）。
#   デスクトップのログイン・ログアウトでも並びは変わる。だから配信の直前に
#   必ず作り直す。9/10 の配信は 0.1〜1.9fps の止まった絵を14分流し、9/11 は
#   1.9fps でゲートに止められた。どちらも作り直せば直っていた。
#
#   詳細は docs/investigations/2026-09-11-display-starvation.md
#
# なぜ :0（デスクトップ）の fps を見ないのか。
#   **ロックされた X11 セッションの GL は正常時も進まない。** 9/11 の実測では
#   `loginctl` が `Active=no LockedHint=yes` を返す状態で :0 の glxgears が
#   1フレームも進まず、同時刻の :99 は 60fps で健全だった。無人運用の 21:00 に
#   画面はロックされているので、:0 は常に遅く出る。判定に使えない。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$HERE/.." && pwd)"

DISPLAY_NUM="${LIVE_DISPLAY:-:99}"
XSOCKET="/tmp/.X11-unix/X${DISPLAY_NUM#:}"
# live/config.py の LIVE_MIN_FPS と同じ既定値。あちらは Unity の実測、こちらは
# 配信を始める前のディスプレイ自体の実測で、見ている対象が違う
MIN_FPS="${LIVE_MIN_FPS:-20}"
PROBE_SEC="${DISPLAY_PROBE_SEC:-12}"
# X が上がるまでの待ち。NVIDIA のモード設定に数秒かかる
WAIT_SEC="${DISPLAY_WAIT_SEC:-30}"

# glxinfo のレンダラ名にこれが出たら GPU ではない（live/unity_live.py と同じ表）
SOFTWARE_RENDERERS="llvmpipe softpipe swrast zink"

# 点検は配信と同じユーザーで走らせる。root で X に繋げても、Unity が繋げるとは
# 限らない（逆も同じ）。本番と同じ条件で確かめる意味がない点検はしない
RUN_USER="${BOTTAN_USER:-${SUDO_USER:-$(stat -c %U "$INSTALL_DIR")}}"

log() { echo "[display] $*"; }

# Restart=always なので、起動できない状態のまま放置すると systemd が5秒おきに
# 永久に叩き続ける（2026-09-10 には restart counter が 560 まで行った）。
# 諦めるときは自分で止める
stop_retry_loop() {
    systemctl stop bottan-live-xorg.service 2>/dev/null || true
}

fail() {
    local detail="$1"
    log "$detail" >&2
    # **ここで黙って終わらないこと。** このユニットが失敗すると run_live.sh は
    # 一度も走らないので、run_live.sh 側の notify.error は鳴らない。
    # 2026-08-30 に「返事したコメント0件」を誰も気付けなかったのと同じ穴になる
    "$INSTALL_DIR/venv/bin/python" -c "
import sys
sys.path.insert(0, '$INSTALL_DIR')
from common import notify
notify.error('配信ディスプレイの準備', '''$detail''')
" 2>/dev/null || log "通知の送信に失敗しました（無視します）"
    exit 1
}

# 点検は配信と同じ条件で回す。**__GL_SYNC_TO_VBLANK=0 を落とさないこと。**
# :99 は NoScanout で表示クロックを持たないので、vblank を待つ GL クライアントは
# 描けていても 1fps を返す。Unity には live/unity_live.py が同じ値を渡している。
as_user() {
    runuser -u "$RUN_USER" -- \
        env DISPLAY="$DISPLAY_NUM" __GL_SYNC_TO_VBLANK=0 "$@"
}

if [ "$(id -u)" -ne 0 ]; then
    echo "root で実行してください: sudo bash $0" >&2
    exit 1
fi

# 配信中に叩くと映像ごと消える。手動実行の事故を止める。
# Unity も OBS も :99 に繋いでいるので、Xorg を入れ直すと道連れで落ちる
# （9/11 に残っていた OBS は実際に Xorg の再起動で消えた）。
#
# 正規の経路ではここは素通りする。bottan-live-display.service は
# bottan-live.service より前に走り、Unity も OBS もまだ居ない
if [ -z "${FORCE:-}" ] && pgrep -f "Unity -projectPath|obs --.*bottan-live" >/dev/null 2>&1; then
    echo "Unity か OBS が動いています。配信中ならディスプレイを作り直すと映像が消えます。" >&2
    echo "承知のうえで実行するなら: sudo FORCE=1 bash $0" >&2
    exit 1
fi

log "Xorg を入れ直します（現在の稼働: $(systemctl show bottan-live-xorg -p ActiveEnterTimestamp --value)）"
systemctl restart bottan-live-xorg.service || fail "bottan-live-xorg.service を再起動できませんでした"

# ソケットができてから xdpyinfo が通るまでに間がある。両方待つ
deadline=$((SECONDS + WAIT_SEC))
until [ -S "$XSOCKET" ] && as_user xdpyinfo >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        # **GPU が1枚しかないので、DRM master は X サーバ1つしか持てない。**
        # デスクトップ側（GDM の :0）が握っていると、:99 はスキャンアウトを
        # 作れずここで落ちる。Blackwell 対応で NoScanout をやめ、ダミーEDID の
        # 実スキャンアウトに切り替えたときから、:99 は modesetting を要求する
        # クライアントになっていて、:0 と真正面から競合する。
        # 2026-09-11 21:40 はこれで起動不能になった（21:28 の再起動は通っている。
        # その間にデスクトップ側が起きて master を取ったのだと見ている）。
        if grep -q "Failed to acquire modesetting permission" /var/log/Xorg.99.log 2>/dev/null; then
            stop_retry_loop
            fail "$DISPLAY_NUM が GPU の modesetting 権限を取れません（Failed to acquire modesetting permission）。GPU は1枚しかなく、デスクトップ側の Xorg (:0) が DRM master を握っています。デスクトップのセッションを止めてから (\`sudo systemctl stop gdm\`) もう一度実行してください"
        fi
        stop_retry_loop
        fail "$DISPLAY_NUM が ${WAIT_SEC}秒 経っても応答しません。\`journalctl -u bottan-live-xorg -n 50\` を見てください"
    fi
    sleep 1
done
log "$DISPLAY_NUM が応答しました"

# openbox は Xorg の再起動に巻き込まれて落ちる（Requires= のため）。
# Restart=always で戻ってはくるが、待たずに進むと OBS のウィンドウキャプチャが
# 「XComposite capture disabled.」で無効化される。明示的に上げ直して確実にする
systemctl restart bottan-live-wm.service || fail "bottan-live-wm.service を再起動できませんでした"

# **ソケットがあるだけでは GPU の Xorg とは限らない。** :99 に予約の仕組みは無く、
# 2026-09-10 22:39 には Orca IDE の Xvfb が先に取っていた。その状態の glxgears は
# llvmpipe（CPU 描画）で 5694fps を返す。速い値が出るので fps だけ見ると素通りする
renderer="$(as_user glxinfo -B 2>/dev/null | sed -n 's/^OpenGL renderer string: //p')"
if [ -z "$renderer" ]; then
    fail "$DISPLAY_NUM の GL レンダラを取得できません。glxinfo が通りませんでした"
fi
for sw in $SOFTWARE_RENDERERS; do
    case "$renderer" in
        *"$sw"*)
            fail "$DISPLAY_NUM が CPU 描画です（$renderer）。GPU の Xorg ではなく Xvfb などが先に $DISPLAY_NUM を取っています。\`fuser -v $XSOCKET\` で掴んでいるプロセスを止めてから、もう一度実行してください"
            ;;
    esac
done
log "レンダラ: $renderer"

# 実測。glxgears は5秒ごとに1行出すので、最後の行を採る。
#
# 書き方に2つ罠がある。どちらも実地で踏んだ。
#   1. stdbuf が要る。glxgears の stdout はファイルやパイプへ向けると全バッファに
#      なり、timeout の SIGTERM で未出力のまま捨てられる（空のログになる）。
#   2. パイプで awk へ渡さない。timeout の signal がプロセスグループごと届いて
#      awk も一緒に落ち、END ブロックが走らないまま終了コード 143 で返る。
#      いったんファイルに落としてから読む。
probe_log="$(mktemp)"
trap 'rm -f "$probe_log"' EXIT
as_user timeout "$PROBE_SEC" stdbuf -o0 glxgears > "$probe_log" 2>/dev/null || true
fps="$(awk '/FPS/ { v = $(NF-1) } END { printf "%.1f", v + 0 }' "$probe_log")"
if awk "BEGIN { exit !($fps < $MIN_FPS) }"; then
    fail "$DISPLAY_NUM の描画が ${fps}fps しか出ていません（目安 ${MIN_FPS}fps）。Xorg を入れ直しても戻らないので、ドライバの再読み込みかホストの再起動が要ります"
fi

log "描画の実測: ${fps}fps（目安 ${MIN_FPS}fps）。配信に進みます"
