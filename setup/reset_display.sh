#!/bin/bash
# 配信用の GPU 仮想ディスプレイ (:99) を作り直し、**実際に描けること**を確かめる。
# bottan-live-display.service が /usr/local/libexec/bottan の配置済みコピーを呼ぶ。
#
# **なぜ配信前に作り直すのか。ここがこのスクリプトの存在理由。**
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
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
cd /

if [[ "$EUID" != 0 || "$#" != 1 ]]; then
    echo "bottan-live-display.service から実行してください" >&2
    exit 1
fi
RUN_USER="$1"
RUN_UID="$(id -u -- "$RUN_USER")"
if [[ "$RUN_UID" == 0 ]]; then
    echo "ディスプレイ点検には非特権の配信ユーザーが必要です" >&2
    exit 1
fi

log() { printf '[display] %s\n' "$*"; }
fail() { log "$*" >&2; exit 1; }

probe() {
    /usr/sbin/runuser -u "$RUN_USER" -- /bin/bash /usr/local/libexec/bottan/probe_display.sh "$@"
}

# 配信中に叩くと映像ごと消える。手動実行の事故を止める。
# Unity も OBS も :99 に繋いでいるので、Xorg を入れ直すと道連れで落ちる
# （9/11 に残っていた OBS は実際に Xorg の再起動で消えた）。
#
# 正規の経路ではここは素通りする。bottan-live-display.service は
# bottan-live.service より前に走り、Unity も OBS もまだ居ない
if [ -z "${FORCE:-}" ] && pgrep -f "Unity -projectPath|obs --.*bottan-live" >/dev/null 2>&1; then
    echo "Unity か OBS が動いています。配信中ならディスプレイを作り直すと映像が消えます。" >&2
    exit 1
fi

log "Xorg を入れ直します"
systemctl restart bottan-live-xorg.service || fail "bottan-live-xorg.service を再起動できませんでした"

if ! probe wait; then
    # Xorg は Restart=always。応答がなければリトライを止める。
    systemctl stop bottan-live-xorg.service 2>/dev/null || true
    if grep -q "Failed to acquire modesetting permission" /var/log/Xorg.99.log 2>/dev/null; then
        fail "GPU の modesetting 権限を取れません。デスクトップ側の Xorg が DRM master を握っていないか確認してください"
    fi
    fail "Xorg が応答しません。journalctl -u bottan-live-xorg -n 50 を見てください"
fi

# OBS のウィンドウキャプチャに openbox が必要。Xorg の再起動に巻き込まれるので上げ直す。
systemctl restart bottan-live-wm.service || fail "bottan-live-wm.service を再起動できませんでした"

probe check
