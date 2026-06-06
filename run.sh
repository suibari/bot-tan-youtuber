#!/bin/bash
source venv/bin/activate

LOG_FILE="logs/pipeline_$(date +%Y%m%d_%H%M%S).log"
nohup python3 -u pipeline.py >> "$LOG_FILE" 2>&1 &
echo "PID: $!"
echo "ログ: $LOG_FILE"
