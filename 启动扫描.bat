@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist venv (
  echo 还没安装依赖，请先双击“安装.bat”。
  pause
  exit /b 1
)
call venv\Scripts\activate.bat
echo Global Trend Radar 启动：每 15 分钟扫描一次，先立即跑一轮。
echo 不要关这个窗口；停止请按 Ctrl+C。
python run_scheduler.py
pause
