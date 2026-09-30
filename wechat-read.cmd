@echo off
rem wechat-read launcher.
rem Finds Python and cli.py, then forwards all arguments.
rem install.ps1 writes the interpreter path to python-path.txt.
rem
rem ASCII only: Chinese in a .cmd file breaks under the console code page.

setlocal
set "HERE=%~dp0"
set "PY="

if exist "%HERE%python-path.txt" set /p PY=<"%HERE%python-path.txt"
if defined PY if not exist "%PY%" set "PY="

if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        echo %%i | findstr /i "WindowsApps" >nul || set "PY=%%i"
    )
)

if not defined PY (
    echo wechat-read: Python not found. Run install.ps1 first.
    exit /b 1
)

%PY% "%HERE%cli.py" %*
exit /b %ERRORLEVEL%
