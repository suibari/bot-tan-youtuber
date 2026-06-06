#!/bin/bash
source venv/bin/activate

nohup python3 pipeline.py >> logs/pipeline_$(date +%Y%m%d_%H%M%S).log 2>&1 &
echo "PID: $!"
echo "ログ: logs/pipeline_$(date +%Y%m%d_%H%M%S).log"
