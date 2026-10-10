#!/usr/bin/env bash
# 本番チェックアウトから実行。root不要のユーザーtimerを導入する。
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
    echo '本番実行ユーザーで実行してください（sudo不要）' >&2
    exit 1
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$HERE/.." && pwd)"
if [[ "$INSTALL_DIR" == */orca/workspaces/* || "$INSTALL_DIR" == *-dev ]]; then
    echo '本番チェックアウトから実行してください' >&2
    exit 1
fi
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
for unit in bottan-unity-license.service bottan-unity-license.timer; do
    sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$HERE/$unit" > "$UNIT_DIR/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now bottan-unity-license.timer
systemctl --user list-timers bottan-unity-license.timer --no-pager
# 無人運用にはlingerが必要。このホストは有効化済み。
loginctl show-user "$(id -un)" -p Linger
