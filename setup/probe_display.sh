#!/bin/bash
# 配信ユーザーで X11 の応答を待ち、GPU レンダラと実測 FPS を確認する。
set -uo pipefail
if [[ "$EUID" == 0 ]]; then
    echo "ディスプレイ点検は配信ユーザーで実行してください" >&2
    exit 1
fi

export DISPLAY="${LIVE_DISPLAY:-:99}"
# NoScanout の Xorg では vblank を待つと描けていても 1fps になる。
export __GL_SYNC_TO_VBLANK=0
DISPLAY_NUM="$DISPLAY"
XSOCKET="/tmp/.X11-unix/X${DISPLAY#:}"
MIN_FPS="${LIVE_MIN_FPS:-20}"
PROBE_SEC="${DISPLAY_PROBE_SEC:-12}"
WAIT_SEC="${DISPLAY_WAIT_SEC:-30}"
SOFTWARE_RENDERERS="llvmpipe softpipe swrast zink"

log() { printf '[display] %s\n' "$*"; }
fail() { log "$*" >&2; exit 1; }

case "${1:-}" in
    wait)
        deadline=$((SECONDS + WAIT_SEC))
        until [ -S "$XSOCKET" ] && timeout 2 xdpyinfo >/dev/null 2>&1; do
            if [ "$SECONDS" -ge "$deadline" ]; then
                fail "$DISPLAY_NUM が ${WAIT_SEC}秒 経っても応答しません"
            fi
            sleep 1
        done
        log "$DISPLAY_NUM が応答しました"
        exit 0
        ;;
    check) ;;
    *) fail "使用法: $0 wait|check" ;;
esac

# **ソケットがあるだけでは GPU の Xorg とは限らない。** :99 に予約の仕組みは無く、
# 2026-09-10 22:39 には Orca IDE の Xvfb が先に取っていた。その状態の glxgears は
# llvmpipe（CPU 描画）で 5694fps を返す。速い値が出るので fps だけ見ると素通りする
renderer="$(glxinfo -B 2>/dev/null | sed -n 's/^OpenGL renderer string: //p')"
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
probe_log="$(mktemp)" || fail "描画計測の一時ファイルを作成できません"
trap 'rm -f "$probe_log"' EXIT
timeout "$PROBE_SEC" stdbuf -o0 glxgears > "$probe_log" 2>/dev/null || true
fps="$(awk '/FPS/ { v = $(NF-1) } END { printf "%.1f", v + 0 }' "$probe_log")"
if awk -v fps="$fps" -v min="$MIN_FPS" 'BEGIN { exit !(fps + 0 < min + 0) }'; then
    fail "$DISPLAY_NUM の描画が ${fps}fps しか出ていません（目安 ${MIN_FPS}fps）。Xorg を入れ直しても戻らないので、ドライバの再読み込みかホストの再起動が要ります"
fi

log "描画の実測: ${fps}fps（目安 ${MIN_FPS}fps）。配信に進みます"
