@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist venv (
  echo 还没安装依赖，请先双击“安装.bat”。
  pause
  exit /b 1
)
call venv\Scripts\activate.bat
echo 正在启动 Dashboard，请在浏览器打开下面显示的本地地址（一般是 http://localhost:8501）。
streamlit run app.py
pause
