#!/bin/bash
# botたん 夜のYouTube自動投稿パイプラインの起動スクリプト
#
# パスはこのスクリプトの位置から解決する。
# （以前は本番パスがハードコードされていて、dev から叩くと本番が走っていた）
#
# 引数は KEY=VALUE 形式で環境変数として渡せる:
#   ./run.sh SKIP_YOUTUBE=true
#
# 依存を更新したときは手動で:
#   ./venv/bin/pip install -r requirements.txt

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# common/ を import できるようにリポジトリのルートを通す
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$SCRIPT_DIR/logs"

# 引数から環境変数をセット (KEY=VALUE 形式)
for arg in "$@"; do
    if [[ "$arg" == *=* ]]; then
        export "$arg"
    fi
done

LOG_FILE="$SCRIPT_DIR/logs/pipeline_$(date +%Y%m%d_%H%M%S).log"

# 朝版(run_quiz.sh)と共通のロックで直列化する。
# record_with_unity が全Unityプロセスを pkill -9 するため、同時実行すると互いを壊す。
flock -w 3600 /tmp/bottan-render.lock \
  "$SCRIPT_DIR/venv/bin/python" -u "$SCRIPT_DIR/shorts/pipeline.py" 2>&1 | tee "$LOG_FILE"
# tee ではなく python の終了コードを systemd に返す。
# 保存しないと下の if 文の値がスクリプトの終了コードになり、
# 非対話実行（systemd）では常に 0 になって失敗が握り潰される
STATUS=${PIPESTATUS[0]}

# 端末から実行された場合のみtail -f
if [ -t 1 ]; then
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    wait
    kill $TAIL_PID 2>/dev/null
fi

exit "$STATUS"
