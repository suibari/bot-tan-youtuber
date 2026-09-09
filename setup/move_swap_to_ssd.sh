#!/usr/bin/env bash
# スワップを HDD から SSD へ移す。
#   sudo bash setup/move_swap_to_ssd.sh
#
# **なぜ必要か。** このホストのスワップは 2GB が SSD(sda)、16GB が
# /mnt/data すなわち WDC WD20EARX（5400rpm の 2TB HDD）に置かれていた。
# 優先度は SSD が -2、HDD が -3 なので、SSD の 2GB を使い切ると残りは全部
# HDD へ落ちる。
#
# 2026-09-09 の配信でそれが起きた。21時台のスワップ使用量は 9.1GB で、
# 7GB 前後が HDD 側に乗っていた。21:20〜21:30 の実測はスワップイン
# 248ページ/秒・スワップアウト 652ページ/秒・iowait 12.3%。ランダム読みで
# この HDD はほぼ振り切れている。結果として
#
#   - VOICEVOX の /synthesis が 15秒 × 2回で ReadTimeout
#   - Unity の /speak が read timeout=10 を連発
#   - OBS の送出が止まり、21:08 に YouTube が enableAutoStop で配信を終了
#   - デスクトップが GNOME の5分無操作で消灯したあと復帰できず、
#     モニターが「信号なし」のまま。ケーブルを替えても直らず再起動で復活
#
# が同時に起きた。gnome-shell を数百MB 読み戻すのに、VOICEVOX・Unity・OBS と
# 同じ1本のスピンドルを取り合うので何分もかかる。
#
# **容量は変えない。** 2GB + 16GB = 18GB を、SSD 上の 18GB 1本にまとめる。
# スワップそのものを減らすわけではないので、根本の「31GB に対して commit が
# 110%」は別途 RAM を空けて対処すること（README のメモリの節を参照）。
#
# vm.swappiness は setup/99-bottan-live.conf の 10 のまま。ここでは触らない。
set -euo pipefail

SSD_SWAP="/swapfile"
HDD_SWAP="/mnt/data/swapfile"
SIZE_GB=18

if [ "$(id -u)" -ne 0 ]; then
    echo "root で実行してください: sudo bash $0" >&2
    exit 1
fi

echo "== いまの状態 =="
swapon --show || true
free -h | head -3
echo

# ── 1. 安全確認 ────────────────────────────────────
# swapoff は「追い出したページを RAM へ戻す」操作なので、使用中のスワップが
# 空きメモリに収まらないと OOM になる。収まらないなら何もせず降りる
used_kb=$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{print t-f}' /proc/meminfo)
avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
if [ "$used_kb" -gt 0 ]; then
    echo "スワップを $((used_kb / 1024))MB 使用中（空きメモリ $((avail_kb / 1024))MB）"
    if [ "$used_kb" -ge "$((avail_kb - 1024 * 1024))" ]; then
        echo "swapoff で RAM へ戻しきれません。重いプロセスを止めてから実行してください" >&2
        echo "  いま何が使っているか: ps -eo rss,comm --sort=-rss | head" >&2
        exit 1
    fi
fi

# 新しいスワップを置く / の空き
root_avail_gb=$(df -BG --output=avail / | tail -1 | tr -d 'G ')
if [ "$root_avail_gb" -lt "$((SIZE_GB + 20))" ]; then
    echo "/ の空きが ${root_avail_gb}GB しかありません（${SIZE_GB}GB + 余裕20GB が要ります）" >&2
    exit 1
fi

# / が SSD であること。ここを取り違えると何も解決しない
root_src=$(findmnt -no SOURCE /)
root_disk=$(lsblk -no PKNAME "$root_src")
if [ "$(cat "/sys/block/$root_disk/queue/rotational")" != "0" ]; then
    echo "/ ($root_src → $root_disk) が回転ディスクです。移す意味がありません" >&2
    exit 1
fi
echo "移し先: $SSD_SWAP (${SIZE_GB}GB, $root_disk = 非回転)"
echo

# ── 2. fstab を控える ──────────────────────────────
backup="/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
cp -a /etc/fstab "$backup"
echo "fstab を控えました: $backup"

# ── 3. いまのスワップを外す ────────────────────────
for f in "$HDD_SWAP" "$SSD_SWAP"; do
    if swapon --show=NAME --noheadings | grep -qx "$f"; then
        echo "swapoff $f"
        swapoff "$f"
    fi
done

# ── 4. SSD 側に作り直す ────────────────────────────
rm -f "$SSD_SWAP"
# ext4 なので fallocate でよい（Ubuntu の公式手順と同じ）。
# 失敗する環境のために dd も用意しておく
if ! fallocate -l "${SIZE_GB}G" "$SSD_SWAP" 2>/dev/null; then
    echo "fallocate が使えないので dd で作ります（数十秒かかります）"
    dd if=/dev/zero of="$SSD_SWAP" bs=1M count=$((SIZE_GB * 1024)) status=progress
fi
chmod 600 "$SSD_SWAP"
mkswap "$SSD_SWAP"
swapon "$SSD_SWAP"

# ── 5. fstab から HDD のスワップを消す ─────────────
# 消すのは第1フィールドが HDD_SWAP の行だけ。/mnt/data のマウント行は残す
awk -v target="$HDD_SWAP" '
    $1 == target { print "# " $0 " ← setup/move_swap_to_ssd.sh が無効化（HDD 上のため）"; next }
    { print }
' "$backup" > /etc/fstab
echo
echo "== fstab のスワップ行 =="
grep -n "swap" /etc/fstab

# ── 6. systemd に読み直させる ──────────────────────
# .swap ユニットは fstab から生成される。忘れると次の起動まで食い違う
systemctl daemon-reload

# ── 7. HDD 側のファイルを消す ──────────────────────
# スワップは中身に価値が無く、要ればこのスクリプトで作り直せる。
# ここまで来ていれば fstab はもう参照していない
if [ -f "$HDD_SWAP" ]; then
    rm -f "$HDD_SWAP"
    echo "$HDD_SWAP を削除しました（16GB を回収）"
fi

echo
echo "== 移したあとの状態 =="
swapon --show
free -h | head -3
echo
echo "戻したいときは: sudo cp -a $backup /etc/fstab && sudo systemctl daemon-reload && sudo reboot"
