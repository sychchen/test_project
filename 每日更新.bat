@echo off
chcp 65001 >nul
title stock-system 每日更新
cd /d %~dp0
set PY=C:\Users\yingchun.song\.workbuddy\binaries\python\envs\default\Scripts\python.exe
set PYTHONIOENCODING=utf-8

echo ============================================
echo   stock-system 每日数据更新 + 全策略选股
echo   建议每个交易日收盘后(19:00 以后)运行
echo   日志落盘: logs\daily_update_*.log
echo ============================================
echo.

"%PY%" run_manual.py %*

pause
