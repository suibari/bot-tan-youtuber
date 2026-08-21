#!/usr/bin/env bash
# botたんライブ配信の起動スクリプト。systemd timer から呼ぶ。
#   ./run_live.sh                     本番
#   ./run_live.sh DRY_RUN=true        YouTube/OBS に触らずローカルだけで回す
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# common/ を import できるようにリポジトリのルートを通す
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 引数の KEY=VALUE を環境変数として渡す
for arg in "$@"; do
    if [[ "$arg" == *=* ]]; then
        export "${arg?}"
    fi
done

mkdir -p logs
LOG="logs/live_$(date +%Y%m%d_%H%M%S).log"

# 録画パイプライン（run.sh / run_quiz.sh）と同じロックを取る。
# core.py:record_with_unity が pkill -9 -f "Unity -projectPath" で Unity を
# 無差別に殺すので、直列化しないと配信中の Unity が巻き添えで落ちる。
# 時間帯は重ならない想定だが、手動実行やリトライで重なったときの保険。
exec 200>/tmp/bottan-render.lock
if ! flock -w 3600 200; then
    echo "[run_live] 他のパイプラインがロックを保持しています。中止します" | tee -a "$LOG"
    exit 1
fi

echo "[run_live] 開始: $(date '+%F %T')" | tee -a "$LOG"
./venv/bin/python -u live/live.py 2>&1 | tee -a "$LOG"
# tee の終了コードではなく python のほうを systemd に返す
STATUS=${PIPESTATUS[0]}
echo "[run_live] 終了: $(date '+%F %T') status=$STATUS" | tee -a "$LOG"
exit "$STATUS"
