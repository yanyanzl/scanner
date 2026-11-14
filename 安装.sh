#!/bin/bash
# macOS 一键安装：创建虚拟环境、安装依赖、生成 .env
set -e
cd "$(dirname "$0")"
echo "[1/3] 检查 Python..."
python3 --version
echo "[2/3] 创建虚拟环境（已有则跳过）..."
if [ ! -d venv ]; then
  python3 -m venv venv
fi
source venv/bin/activate
echo "[3/3] 安装依赖..."
pip install -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env，请打开填写 Reddit / YouTube 凭证（不填也能跑 RSS + GDELT）。"
fi
echo ""
echo "安装完成。下一步：./启动扫描.sh 开始每 15 分钟扫描，或 ./启动面板.sh 看 Dashboard。"
