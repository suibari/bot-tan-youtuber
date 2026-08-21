#!/bin/bash
# 朝の勘違いクイズ Shorts 生成パイプラインの起動スクリプト
#
# パスはこのスクリプトの位置から解決する。
#
# 引数は KEY=VALUE 形式で環境変数として渡せる:
#   ./run_quiz.sh SKIP_YOUTUBE=true QUIZ_NO_CONSUME=true
#
# 依存を更新したときは手動で:
#   ./venv/bin/pip install -r requirements.txt

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# common/ を import できるようにリポジトリのルートを通す
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$SCRIPT_DIR/logs"

# KEY=VALUE 形式の引数は環境変数にし、それ以外は quiz_pipeline.py へ渡す
PY_ARGS=()
for arg in "$@"; do
    if [[ "$arg" == [A-Za-z_]*=* ]]; then
        export "$arg"
    else
        PY_ARGS+=("$arg")
    fi
done

LOG_FILE="$SCRIPT_DIR/logs/quiz_$(date +%Y%m%d_%H%M%S).log"

# 夜版と共通のロックで直列化する。
# record_with_unity が `pkill -9 -f "Unity -projectPath"` で全Unityを無差別に殺し、
# /tmp/.X*-lock も全削除するため、夜版と同時に走ると互いを壊す。
# systemd timer の Persistent=true により、停止後の再起動で両方が同時発火しうる。
flock -w 3600 /tmp/bottan-render.lock \
  "$SCRIPT_DIR/venv/bin/python" -u "$SCRIPT_DIR/shorts/quiz_pipeline.py" "${PY_ARGS[@]}" \
  2>&1 | tee "$LOG_FILE"
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
