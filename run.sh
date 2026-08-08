#!/bin/bash
source /home/suibari/work/bottan-pipeline/venv/bin/activate
mkdir -p /home/suibari/work/bottan-pipeline/logs

# requirementsに更新があったら、インストールする
pip install -r requirements.txt -q --disable-pip-version-check 2>&1 | grep -v "already satisfied"

# 引数から環境変数をセット (KEY=VALUE 形式)
for arg in "$@"; do
    if [[ "$arg" == *=* ]]; then
        export "$arg"
    fi
done

LOG_FILE="/home/suibari/work/bottan-pipeline/logs/pipeline_$(date +%Y%m%d_%H%M%S).log"
# 朝版(run_quiz.sh)と共通のロックで直列化する。
# record_with_unity が全Unityプロセスを pkill -9 するため、同時実行すると互いを壊す。
flock -w 3600 /tmp/bottan-render.lock \
  python3 -u /home/suibari/work/bottan-pipeline/pipeline.py 2>&1 | tee "$LOG_FILE"

# 端末から実行された場合のみtail -f
if [ -t 1 ]; then
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    wait
    kill $TAIL_PID 2>/dev/null
fi
