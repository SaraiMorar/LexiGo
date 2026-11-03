@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LexiGo
".venv\Scripts\python.exe" app.py
echo.
echo LexiGo 已停止，按任意键关闭窗口...
pause >nul
