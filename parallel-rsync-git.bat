@echo off
rem parallel-rsync-git —— 给 parallel-rsync 接上 git（Windows 启动器）
rem 用法：parallel-rsync-git [--report|--dry-run|--builtin] [-c config.yml]
setlocal
set "SCRIPT=%~dp0parallel_rsync_git.py"

if not exist "%SCRIPT%" (
    echo 错误：找不到 "%SCRIPT%" 1>&2
    exit /b 1
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%SCRIPT%" %*
) else (
    py -3 "%SCRIPT%" %*
)
