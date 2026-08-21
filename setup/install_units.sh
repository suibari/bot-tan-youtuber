#!/usr/bin/env bash
# botたんの systemd ユニット（Shorts 2本 + ライブ配信）をインストールする。
#   sudo bash setup/install_units.sh
#
# ユニットの中の __INSTALL_DIR__ / __USER__ を実際の値に置き換えてから配置する。
# 本番と dev の両方から実行できるので、systemd がどちらを向くかは
# 「どのディレクトリでこれを走らせたか」で決まる。
#
# 映像まわり（Xorg :99 / openbox）は install_xorg.sh が別に入れる。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root で実行してください: sudo bash $0" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$HERE/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
# ライブ配信の OBS が PulseAudio に繋ぐのに要る（bottan-live.service の Environment）
RUN_UID="$(id -u "$RUN_USER")"

for f in run.sh run_quiz.sh run_live.sh; do
    if [ ! -f "$INSTALL_DIR/$f" ]; then
        echo "$INSTALL_DIR/$f がありません。統合リポジトリのルートで実行してください" >&2
        exit 1
    fi
done

echo "インストール先: $INSTALL_DIR (User=$RUN_USER, UID=$RUN_UID)"

install_unit() {
    local name="$1"
    sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        -e "s|__UID__|$RUN_UID|g" \
        "$HERE/$name" > "/etc/systemd/system/$name"
    chmod 0644 "/etc/systemd/system/$name"
    echo "  配置: $name"
}

for u in bottan-pipeline.service bottan-pipeline.timer \
         bottan-quiz.service bottan-quiz.timer \
         bottan-live.service bottan-live.timer; do
    install_unit "$u"
done

systemctl daemon-reload

# ライブ配信は GPU 仮想ディスプレイが要る。入っていなければ警告だけ出す
if [ ! -f /etc/systemd/system/bottan-live-xorg.service ]; then
    echo
    echo "警告: bottan-live-xorg.service がありません。" >&2
    echo "      ライブ配信を使うなら sudo bash setup/install_xorg.sh を先に実行してください。" >&2
fi

systemctl enable bottan-pipeline.timer bottan-quiz.timer bottan-live.timer

echo
echo "次回の起動予定:"
systemctl list-timers bottan-pipeline.timer bottan-quiz.timer bottan-live.timer --no-pager

echo
echo "時間帯が重なっていないか確認してください（録画と配信は flock で直列化されますが、"
echo "配信中に録画のロック待ちが入ると配信の終了が遅れます）"
