@echo off
cd /d "%~dp0"
echo ================================================
echo   存档当前版本（给脚本拍快照）
echo ================================================
echo.
set /p msg=请输入存档说明（直接回车=自动命名）: 
if "%msg%"=="" set msg=手动存档
git add -A
git commit -m "%msg%"
echo.
echo 已存档。最近 5 次存档：
git log --oneline -5
echo.
pause
