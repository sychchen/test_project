@echo off
chcp 65001 >nul
echo ==========================================
echo   选股系统 - 本地一键启动
echo   API + Web: http://127.0.0.1:8600
echo ==========================================
cd /d %~dp0
set PY=C:\Users\yingchun.song\.workbuddy\binaries\python\envs\default\Scripts\python.exe
start "" http://127.0.0.1:8600
"%PY%" -m uvicorn app.api.main:app --host 127.0.0.1 --port 8600
pause
