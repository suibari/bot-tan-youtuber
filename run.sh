#!/bin/bash
source /home/suibari/work/bottan-pipeline/venv/bin/activate
mkdir -p /home/suibari/work/bottan-pipeline/logs

# Unityスクリプトを同期
echo "[Setup] Unityスクリプトを同期中..."
cp /home/suibari/work/bottan-pipeline/unity-scripts/VRM1LipSync.cs /home/suibari/bottan-video/Assets/Scripts/VRM1LipSync.cs
cp /home/suibari/work/bottan-pipeline/unity-scripts/VideoRecorder.cs /home/suibari/bottan-video/Assets/Scripts/VideoRecorder.cs
echo "[Setup] 同期完了"

LOG_FILE="/home/suibari/work/bottan-pipeline/logs/pipeline_$(date +%Y%m%d_%H%M%S).log"
python3 -u /home/suibari/work/bottan-pipeline/pipeline.py 2>&1 | tee "$LOG_FILE"

# 端末から実行された場合のみtail -f
if [ -t 1 ]; then
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    wait
    kill $TAIL_PID 2>/dev/null
fi
