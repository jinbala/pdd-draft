@echo off
cd /d "%~dp0"
echo ================================================
echo   回退到上一次存档
echo ================================================
echo.
echo 未存档的改动：
git status --short
echo.
echo 警告：下一步会【丢弃所有未存档的改动】，
echo       把 main.py / config.py 等恢复到上次存档。
echo.
pause
git restore .
echo.
echo 已回退到上次存档。
echo.
pause
