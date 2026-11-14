@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/3] 检查 Python...
python --version
if errorlevel 1 (
  echo 找不到 python，请先安装 Python 3.12（安装时勾选 Add Python to PATH），再双击本文件。
  pause
  exit /b 1
)
echo [2/3] 创建虚拟环境（已有则跳过）...
if not exist venv (
  python -m venv venv
)
call venv\Scripts\activate.bat
echo [3/3] 安装依赖...
pip install -r requirements.txt
if not exist .env (
  copy .env.example .env
  echo 已生成 .env，请用记事本打开填写 Reddit / YouTube 凭证（不填也能跑 RSS + GDELT）。
)
echo.
echo 安装完成。下一步：双击“启动扫描.bat”开始每 15 分钟扫描，或双击“启动面板.bat”看 Dashboard。
pause
