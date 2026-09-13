#!/usr/bin/env bash
# 配信で立てた OBS と Unity を確実に落とす。bottan-live.service の ExecStopPost から呼ぶ。
#
# **なぜ systemd の cgroup 停止だけでは足りないか。**
# bottan-live.service は DBUS_SESSION_BUS_ADDRESS を渡して flatpak の OBS を起動する
# （PulseAudio に繋ぐのに要る）。すると OBS はセッションバス越しに立ち上がり、
# ユニットの cgroup の外へ出る。ユニットが終わっても道連れにならず、そのまま残る。
#
# **残すと翌晩の配信が止まる。** setup/reset_display.sh は配信の直前に走り、
# Unity か OBS が居たら「配信中かもしれない」と判断して exit 1 する。残留を掴むと
# bottan-live-display.service が失敗し、Requires= で引いている bottan-live.service が
# Dependency failed で起動しない。
#
# 2026-09-12 の配信で残った OBS が、9/13 20:40 のタイマーでこれを踏んだ。配信が出な
# かっただけでなく、当時 Conflicts= で止められていた画像生成サイドカーが ExecStopPost の
# 届かない経路（サービスが一度も active にならない）で止まったままになり、23:05 の
# おやすみポストが ECONNREFUSED で絵なしになった。
set -uo pipefail

# **reset_display.sh のガードと同じパターンを見ること。** 片方だけ直すと、
# 掃除したつもりで翌晩また引っかかる。
PATTERN='Unity -projectPath|obs --.*bottan-live'

# **パターンに一致しただけで殺してはいけない。** `pgrep -f` はコマンドラインを見るので、
# このパターンを引数に持つ呼び出し元（`bash -c "... stop_live_processes.sh ..."` や
# 確認のために pgrep を並べたシェル）自身にも一致する。実際、これを書いた日に手元の
# シェルごと SIGTERM で消えた。実体が OBS / Unity のものだけを残す。
# flatpak の OBS は bwrap が包んでいるので、その3つを許す。
ALLOWED_COMM='obs|bwrap|Unity'

# シーン設定の保存を待つ時間。OBS は終了時に collection を書き出す。
GRACE_SEC=30

running() {
    local pid comm
    for pid in $(pgrep -f "$PATTERN" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        comm="$(cat "/proc/$pid/comm" 2>/dev/null)" || continue
        [[ "$comm" =~ ^($ALLOWED_COMM)$ ]] && echo "$pid"
    done
}

signal_all() {
    local sig="$1" pid
    for pid in $(running); do
        kill "-$sig" "$pid" 2>/dev/null || true
    done
}

pids="$(running)"
if [ -z "$pids" ]; then
    echo "[stop_live] 残っている配信プロセスはありません"
    exit 0
fi
echo "[stop_live] 終了させます: $(echo "$pids" | tr '\n' ' ')"

# まず行儀よく頼む。いきなり KILL するとシーンの変更が飛ぶ。
signal_all TERM

deadline=$((SECONDS + GRACE_SEC))
while [ "$SECONDS" -lt "$deadline" ]; do
    if [ -z "$(running)" ]; then
        echo "[stop_live] 終了しました"
        exit 0
    fi
    sleep 1
done

echo "[stop_live] ${GRACE_SEC}秒で消えないので強制終了します" >&2
signal_all KILL
sleep 2
remain="$(running)"
if [ -n "$remain" ]; then
    # ここまで来たら翌晩のガードに引っかかる。journal に残して気付けるようにする。
    echo "[stop_live] まだ残っています: $(echo "$remain" | tr '\n' ' ')" >&2
fi

# **常に成功で返す。** 掃除に失敗しても配信そのものは終わっている。Type=oneshot だと
# ExecStopPost の失敗がユニット全体の failed になり、「配信が落ちた」ことになってしまう。
exit 0
