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

# 録画パイプラインは配信と違って待てるので、LLM の上限を伸ばす。
# 配信側（live/）は .env の LLM_TIMEOUT_SEC のまま短く縛る
export LLM_TIMEOUT_SEC="${LLM_TIMEOUT_SEC:-180}"

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

# 失敗を無人で握り潰さない。
# 2026-08-29 の GPU 交換で VOICEVOX と Unity が同時に壊れたとき、
# systemd の failed 状態は残るのに誰も見ておらず、丸1日ぶん取りこぼした。
if [ "$STATUS" -ne 0 ]; then
    "$SCRIPT_DIR/venv/bin/python" -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR')
from common import notify
notify.error('朝のクイズ', open('$LOG_FILE', errors='replace').read()[-1500:])
" 2>/dev/null || true
fi

# 端末から実行された場合のみtail -f
if [ -t 1 ]; then
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    wait
    kill $TAIL_PID 2>/dev/null
fi

exit "$STATUS"
