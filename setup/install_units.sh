#!/bin/bash
# botたんの systemd ユニット（Shorts 2本 + ライブ配信）をインストールする。
#   sudo /bin/bash /root/bottan-deploy/source/setup/install_units.sh APP_CHECKOUT APP_USER
# ライブ用 timer は既定で無効。LIVE_ENABLED=1 で有効にする。
#
# ユニットの中の __INSTALL_DIR__ / __USER__ を実際の値に置き換えてから配置する。
# インストール元には管理者が確認した /root 配下のスナップショットを使う。
# APP_CHECKOUT は非特権サービスの実行先。README.md の導入手順を参照。
#
# 映像まわり（Xorg :99 / openbox）は install_xorg.sh が別に入れる。
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ "$EUID" != 0 || "$HERE" != /root/* || "$#" != 2 || "${1:-}" != /* ]]; then
    echo "README.md の手順で /root 配下の確認済みコピーから実行してください（引数: APP_CHECKOUT APP_USER）" >&2
    exit 1
fi
cd /

INSTALL_DIR="$(realpath -e -- "$1")"
RUN_USER="$2"
# テンプレートへ埋め込んでも systemd や sed の構文として解釈されない値に限る。
if [[ ! "$INSTALL_DIR" =~ ^/[a-zA-Z0-9_./-]+$ || ! "$RUN_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]]; then
    echo "APP_CHECKOUT または APP_USER の形式が無効です" >&2
    exit 1
fi
RUN_UID="$(id -u -- "$RUN_USER")"
if [[ "$RUN_UID" == 0 ]]; then
    echo "APP_USER は非特権の配信ユーザーを指定してください" >&2
    exit 1
fi

for f in run.sh run_quiz.sh run_live.sh; do
    if [ ! -f "$INSTALL_DIR/$f" ]; then
        echo "$INSTALL_DIR/$f がありません。統合リポジトリのルートで実行してください" >&2
        exit 1
    fi
done

# dev の worktree を指定すると本番のタイマーが dev を向いてしまい、
# 配信が dev の Unity・限定公開で走る。意図してやるとき以外は止める
if [[ "$(basename "$INSTALL_DIR")" == *-dev ]] && [ -z "${ALLOW_DEV:-}" ]; then
    PROD_DIR="$(dirname "$INSTALL_DIR")/$(basename "$INSTALL_DIR" -dev)"
    echo "$INSTALL_DIR は dev の worktree です。" >&2
    echo "本番へ入れるなら本番のディレクトリで実行してください:" >&2
    echo "    sudo /bin/bash $0 $PROD_DIR $RUN_USER" >&2
    echo "dev を承知で入れるなら: sudo ALLOW_DEV=1 /bin/bash $0 $INSTALL_DIR $RUN_USER" >&2
    exit 1
fi

echo "インストール先: $INSTALL_DIR (User=$RUN_USER, UID=$RUN_UID)"

install_unit() {
    local name="$1" staged
    staged="$(mktemp /etc/systemd/system/.bottan-unit.XXXXXXXX)"
    sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__USER__|$RUN_USER|g" \
        -e "s|__UID__|$RUN_UID|g" \
        "$HERE/$name" > "$staged"
    chmod 0644 "$staged"
    mv -fT -- "$staged" "/etc/systemd/system/$name"
    echo "  配置: $name"
}

install -d -o root -g root -m 0755 /usr/local/libexec/bottan
install -o root -g root -m 0755 "$HERE/reset_display.sh" "$HERE/probe_display.sh" /usr/local/libexec/bottan/

for u in bottan-pipeline.service bottan-pipeline.timer \
         bottan-quiz.service bottan-quiz.timer \
         bottan-live-prepare.service bottan-live-prepare.timer \
         bottan-live-display-notify.service bottan-live-display.service \
         bottan-live.service bottan-live.timer; do
    install_unit "$u"
done

systemctl daemon-reload

# ollama は別ホストの bsky-affirmative-bot と共用しているので、勝手に上書きしない。
# ずれていたら知らせるだけにする。ここがずれると 11GB のモデルが読み直され続け、
# ARDY の生成がタイムアウトして Shorts からモーションが消える（2026-09-02）
OLLAMA_DROPIN=/etc/systemd/system/ollama.service.d/override.conf
if [ ! -f "$OLLAMA_DROPIN" ]; then
    echo
    echo "警告: $OLLAMA_DROPIN がありません。" >&2
    echo "      setup/ollama-override.conf を参照して入れてください。" >&2
elif ! diff -q "$HERE/ollama-override.conf" "$OLLAMA_DROPIN" >/dev/null; then
    echo
    echo "警告: $OLLAMA_DROPIN が setup/ollama-override.conf と違います:" >&2
    diff -u "$OLLAMA_DROPIN" "$HERE/ollama-override.conf" | sed 's/^/      /' >&2 || true
    echo "      OLLAMA_CONTEXT_LENGTH が common/llm.py の OLLAMA_NUM_CTX と同値か特に確認すること。" >&2
fi

# スワップの出しやすさ。OBS がスワップアウトしていると、配信開始でエンコーダを
# 作るときにページインを待たされ、描画スレッドも 33ms に間に合わなくなる。
# 2026-09-07 の配信事故はこれ（詳細は 99-bottan-live.conf のコメント）
SYSCTL_DROPIN=/etc/sysctl.d/99-bottan-live.conf
if [ ! -f "$SYSCTL_DROPIN" ] || ! diff -q "$HERE/99-bottan-live.conf" "$SYSCTL_DROPIN" >/dev/null; then
    install -m 0644 "$HERE/99-bottan-live.conf" "$SYSCTL_DROPIN"
    sysctl -p "$SYSCTL_DROPIN"
    echo "  配置: $(basename "$SYSCTL_DROPIN")"
fi

# ライブ配信は GPU 仮想ディスプレイが要る。入っていなければ警告だけ出す。
# bottan-live.service は bottan-live-display.service を Requires= しており、
# あちらが Xorg を作り直すので、Xorg のユニットが無いと配信は始まらない
if [ ! -f /etc/systemd/system/bottan-live-xorg.service ]; then
    echo
    echo "警告: bottan-live-xorg.service がありません。" >&2
    echo "      新規導入は README.md の install_xorg.sh の手順を参照してください。" >&2
fi

systemctl enable bottan-pipeline.timer bottan-quiz.timer

# ライブ配信は 2026-11-27 から休止中。再インストールで勝手に再開しないよう、
# 明示しない限り配信と枠準備の timer は止めておく。
# 止めたあとに残った YouTube の枠と DB の告知対象は live/retire_live.py で片付ける。
if [[ "${LIVE_ENABLED:-}" == 1 ]]; then
    systemctl enable bottan-live-prepare.timer bottan-live.timer
else
    systemctl disable --now bottan-live-prepare.timer bottan-live.timer
    echo "  ライブ配信は休止中: bottan-live-prepare.timer / bottan-live.timer を無効化しました"
fi

echo
echo "次回の起動予定:"
systemctl list-timers bottan-pipeline.timer bottan-quiz.timer \
    bottan-live-prepare.timer bottan-live.timer --no-pager

echo
echo "時間帯が重なっていないか確認してください（録画と配信は flock で直列化されますが、"
echo "配信中に録画のロック待ちが入ると配信の終了が遅れます）"
