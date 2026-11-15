#!/bin/bash
# macOS 启动 Dashboard：请在浏览器打开终端里显示的本地地址（一般是 http://localhost:8501）。
set -e
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "还没安装依赖，请先运行 ./安装.sh"
  exit 1
fi
source venv/bin/activate
streamlit run app.py
