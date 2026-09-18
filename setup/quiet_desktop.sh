#!/usr/bin/env bash
# 無人配信ホストから、配信に要らないデスクトップの常駐と自動更新を退かせる。
#   bash setup/quiet_desktop.sh          # 実行
#   bash setup/quiet_desktop.sh --undo   # 元に戻す
#
# **sudo で実行しないこと。** `systemctl --user` は呼び出したユーザのセッションを
# 見るので、root で走らせると root のセッション（存在しない）を触ってしまう。
# apt のタイマーを直すところだけ、スクリプトの中から sudo を呼ぶ。
#
# ── なぜ必要か ───────────────────────────────────
#
# この機械は GUI ログインしたまま無人で配信する。GNOME の常駐は誰も見ていない
# のに RAM を掴み、タイマーでときどき目を覚ましてディスクを触る。配信中に
# 効くのは後者のほう。**掴んでいる RAM より、寝ているものが起きることが問題**で、
# 起きた拍子にページインと I/O が走り、OBS の送出と同じディスクを取り合う。
#
# 2026-09-19 の実測（uptime 3日22時間）:
#
#   gnome-software          swap 645MB / rss  35MB   ほぼ全部スワップの上で寝ている
#   nautilus                swap 154MB / rss  50MB
#   update-manager          swap  72MB / rss  32MB
#   evolution-alarm-notify  swap  15MB / rss  21MB
#   tracker-miner-fs-3      swap   4MB / rss  15MB
#
# 回収できるのは合計 900MB 前後で、これ自体は配信を左右する量ではない。
# 狙いは常駐を減らすことより、**配信枠で目を覚ますものを無くすこと**。
#
# apt-daily.timer が現にそれをやっていた。既定は
#
#   OnCalendar=*-*-* 6,18:00
#   RandomizedDelaySec=12h
#
# で、18:00 の回は 18:00〜翌06:00 のどこかへ散る。**21:00〜22:00 の配信枠は
# この範囲のど真ん中**で、2026-09-18 は 21:14:21 に起動している（その回は
# 1秒で終わっており、あの晩の詰まりの原因ではない。ただ毎日抽選しているので、
# いつか重い回を引く）。6時と13時に寄せて枠から外す。
#
# apt-daily-upgrade.timer（実際にパッケージを展開する重いほう、実測 CPU 30秒）は
# OnCalendar=*-*-* 6:00 / RandomizedDelaySec=60m なので 6〜7時に収まる。触らない。
#
# ── デスクトップを使うときに何が変わるか ──────────
#
# **GUI で座って使うぶんには、止めたものは全部そのまま起動できる。** 消すのは
# 「ログイン時に勝手に常駐して、裏で更新を確認しに行く」経路だけで、アプリ
# そのものではない。具体的には:
#
#   gnome-software          「ソフトウェア」アプリはメニューから普通に開ける。
#                           無くなるのは**更新の自動確認と通知**だけ
#   update-notifier         「更新があります」のポップアップが出なくなる。
#                           update-manager は自分で開けば今までどおり動く
#   evolution-alarm-notify  Evolution の予定のアラーム通知が出なくなる。
#                           Evolution 自体を使っていないなら失うものは無い
#
# 更新が当たらなくなるわけではない。apt-daily-upgrade.timer（6〜7時）が
# unattended-upgrade を走らせ続けるので、**自動更新はこれまでどおり効く**。
# 変わるのは人間への通知だけ。気になるなら `sudo apt update && sudo apt upgrade`。
#
# 戻したくなったら `bash setup/quiet_desktop.sh --undo` で全部戻る。
#
# ── 触らないもの ────────────────────────────────
#
# nautilus・gnome-shell・gnome-system-monitor は残す。人が GUI で操作しに
# 来るときに要るものを機械的に殺すと、次に座ったときに困るだけで、配信中に
# 勝手に目を覚ます類ではない。gnome-shell は :0 のセッションそのもの。
#
# **tracker-miner-fs-3 も既定では触らない。** 最初は止める側に入れていたが、
# 実測が swap 4MB / rss 15MB しかないのに、止めると GNOME の検索（Activities
# でファイル名や中身から探すやつ）と nautilus の全文検索が効かなくなる。
# 割に合わない。配信ホストとして本当に要らないと判断したときだけ、下の
# QUIET_TRACKER=1 を付けて実行する:
#
#   QUIET_TRACKER=1 bash setup/quiet_desktop.sh
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "sudo を付けずに実行してください: bash $0" >&2
    exit 1
fi

# 配信枠で目を覚ます、人が見ていないと意味の無い常駐。
# アプリ本体ではなく autostart の経路だけを止めるので、GUI からは今までどおり開ける
USER_UNITS=(
    "app-org.gnome.Software@autostart.service"
    "app-update\\x2dnotifier@autostart.service"
    "app-org.gnome.Evolution\\x2dalarm\\x2dnotify@autostart.service"
)
# デスクトップ検索を捨ててでも静かにしたいとき（上の「触らないもの」を参照）。
# --undo は環境変数の有無によらず、両方を戻す
if [ "${QUIET_TRACKER:-0}" = "1" ] || [ "${1:-}" = "--undo" ]; then
    USER_UNITS+=("tracker-miner-fs-3.service")
fi
APT_DROPIN="/etc/systemd/system/apt-daily.timer.d/bottan-live.conf"

undo=false
[ "${1:-}" = "--undo" ] && undo=true

echo "== いまスワップの上に居る常駐 =="
for name in gnome-software update-manager evolution-alarm-notify tracker-miner-fs-3; do
    for pid in $(pgrep -x "$name" 2>/dev/null || true); do
        awk -v n="$name" -v p="$pid" \
            '/^VmSwap:/{s=$2} /^VmRSS:/{r=$2} END{if (s!="") printf "  %-24s pid %-8s swap %5dMB  rss %5dMB\n", n, p, s/1024, r/1024}' \
            "/proc/$pid/status" 2>/dev/null || true
    done
done
echo

if $undo; then
    echo "== 常駐を戻します =="
    for u in "${USER_UNITS[@]}"; do
        systemctl --user unmask "$u" 2>/dev/null && echo "  unmask $u" || true
    done
    echo "== apt-daily.timer を既定へ戻します =="
    sudo rm -f "$APT_DROPIN"
    sudo systemctl daemon-reload
    sudo systemctl restart apt-daily.timer
    echo
    echo "次回ログインから元どおりになります。"
    exit 0
fi

echo "== 常駐を止めます =="
for u in "${USER_UNITS[@]}"; do
    # mask してから stop する。逆にすると、止めた直後に D-Bus の
    # activation で起き直すものがある（gnome-software がそれ）
    systemctl --user mask "$u" 2>/dev/null && echo "  mask $u" || echo "  （$u は見当たりません）"
    systemctl --user stop "$u" 2>/dev/null || true
done
# autostart は .desktop 経由でも起きるので、いま動いているものは名指しで落とす。
# -x（完全一致）で、たまたま名前を含むだけのプロセスを巻き込まない
for name in gnome-software update-manager evolution-alarm-notify; do
    pkill -x "$name" 2>/dev/null && echo "  終了させました: $name" || true
done

echo
echo "== apt-daily.timer を配信枠の外へ =="
# 既定の OnCalendar=*-*-* 6,18:00 / RandomizedDelaySec=12h だと 18:00 の回が
# 21:00〜22:00 に落ちてくる。空の OnCalendar= で既定を消してから書き直す
# （drop-in のリスト型は代入ではなく追記になるため、この1行が要る）
sudo install -d /etc/systemd/system/apt-daily.timer.d
sudo tee "$APT_DROPIN" >/dev/null <<'EOF'
# botたんの配信ホスト向け。setup/quiet_desktop.sh が置く。
#
# 既定は OnCalendar=*-*-* 6,18:00 に RandomizedDelaySec=12h で、18:00 の回が
# 18:00〜翌06:00 へ散る。21:00〜22:00 の配信枠がこの中に入っていて、
# 2026-09-18 は 21:14:21 に起動した。6時と13時に寄せて枠から外す。
[Timer]
OnCalendar=
OnCalendar=*-*-* 6:00
OnCalendar=*-*-* 13:00
RandomizedDelaySec=2h
EOF
sudo systemctl daemon-reload
sudo systemctl restart apt-daily.timer

echo
echo "== 結果 =="
systemctl list-timers apt-daily.timer --no-pager | head -3
echo
free -h | head -3
echo
echo "スワップに退避済みのページは、プロセスを止めても SwapFree には"
echo "すぐには戻りません（カーネルが解放を見つけたときに戻る）。"
echo "効くのは次の配信から: 配信枠で目を覚ますものが減ります。"
