@echo off
chcp 65001 >nul
echo ==========================================
echo   选股系统 - 每日数据更新 + 全策略选股
echo   链路: basic -^> quotes -^> valsnap -^> index -^> moneyflow -^> pick
echo   (数据源反爬自动降级, 缺口自动 baostock 补齐)
echo ==========================================
cd /d %~dp0
set PY=C:\Users\yingchun.song\.workbuddy\binaries\python\envs\default\Scripts\python.exe
"%PY%" daily_update.py %*
pause
