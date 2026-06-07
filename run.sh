#!/bin/bash
source venv/bin/activate
mkdir -p logs
LOG_FILE="logs/pipeline_$(date +%Y%m%d_%H%M%S).log"
nohup python3 -u pipeline.py >> "$LOG_FILE" 2>&1 &
PID=$!
echo "PID: $PID"
echo "ログ: $LOG_FILE"

# 端末から実行された場合のみtail -f
if [ -t 1 ]; then
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    wait $PID
    kill $TAIL_PID 2>/dev/null
fi
