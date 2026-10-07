@echo off
cd /d "%~dp0"

echo ================================================
echo   测试模式：只处理 5 条（倍数 2.0）
echo ================================================
echo.
python main.py --multiplier 2.0 --limit 5
echo.
pause
