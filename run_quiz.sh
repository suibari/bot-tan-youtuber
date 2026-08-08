#!/bin/bash
# 朝の勘違いクイズ Shorts 生成パイプラインの起動スクリプト
#
# run.sh と違い、パスはこのスクリプトの位置から解決する。
# （run.sh は本番パスがハードコードされていて、dev から叩くと本番が走ってしまう）
#
# 引数は KEY=VALUE 形式で環境変数として渡せる:
#   ./run_quiz.sh SKIP_YOUTUBE=true QUIZ_NO_CONSUME=true

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

source "$SCRIPT_DIR/venv/bin/activate"
mkdir -p "$SCRIPT_DIR/logs"

# requirementsに更新があったら、インストールする
pip install -r "$SCRIPT_DIR/requirements.txt" -q --disable-pip-version-check 2>&1 \
  | grep -v "already satisfied"

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
  python3 -u "$SCRIPT_DIR/quiz_pipeline.py" "${PY_ARGS[@]}" 2>&1 | tee "$LOG_FILE"

# 端末から実行された場合のみtail -f
if [ -t 1 ]; then
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    wait
    kill $TAIL_PID 2>/dev/null
fi
