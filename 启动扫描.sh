#!/bin/bash
# macOS 启动定时扫描：每 15 分钟一轮，先立即跑一轮。不要关这个终端；停止请按 Ctrl+C。
set -e
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "还没安装依赖，请先运行 ./安装.sh"
  exit 1
fi
source venv/bin/activate
echo "Global Trend Radar 启动：每 15 分钟扫描一次，先立即跑一轮。"
python3 run_scheduler.py
