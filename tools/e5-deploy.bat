@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title E5 patch deploy

rem ============================================================
rem  This .bat is pure ASCII on purpose.
rem  A UTF-8 .bat with Chinese comments gets mis-parsed by the GBK
rem  console: comment bytes leak into command lines. All Chinese
rem  output comes from the Python side (PYTHONIOENCODING=utf-8).
rem ============================================================

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY="
set "PYFALL="

rem ---- candidates: prefer one that ALREADY has paramiko ----
rem Order matters: bundled/3.9+ first. Python 3.8 on the PATH works but
rem emits a stray byte per line under chcp 65001 in a pipe, so avoid it
rem when a newer interpreter exists.
if not defined PY (
    for /d %%D in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do call :probe "%%~fD\python.exe"
)
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :probe "%%~fD\python.exe"
)

python -V >nul 2>&1
if not errorlevel 1 (
    if not defined PYFALL set "PYFALL=python"
    if not defined PY (
        python -c "import paramiko" >nul 2>&1
        if not errorlevel 1 set "PY=python"
    )
)

if not defined PY (
    py -3 -V >nul 2>&1
    if not errorlevel 1 (
        if not defined PYFALL set "PYFALL=py -3"
        py -3 -c "import paramiko" >nul 2>&1
        if not errorlevel 1 set "PY=py -3"
    )
)

if not defined PY (
    python3 -V >nul 2>&1
    if not errorlevel 1 (
        if not defined PYFALL set "PYFALL=python3"
        python3 -c "import paramiko" >nul 2>&1
        if not errorlevel 1 set "PY=python3"
    )
)

if not defined PY (
    for /d %%D in ("C:\Python3*") do call :probe "%%~fD\python.exe"
)

if not defined PY (
    if not defined PYFALL (
        echo [ERROR] No Python 3 found.
        echo         Install it: https://www.python.org/downloads/
        echo         Check "Add python.exe to PATH" during setup.
        goto :end
    )
    echo [SETUP] %PYFALL% lacks paramiko, installing...
    %PYFALL% -m pip install --quiet paramiko
    %PYFALL% -c "import paramiko" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] paramiko install failed. Run manually:
        echo         %PYFALL% -m pip install paramiko
        goto :end
    )
    set "PY=%PYFALL%"
)

rem no % formatting here: a bare %s would be eaten by cmd as a variable
%PY% -c "import sys; print('[ENV] Python ' + sys.version.split()[0])"

rem ============================================================
rem  No arguments = local one-click:
rem  pick the highest local source (dist/ and source tree included),
rem  no network access.
rem ============================================================
%PY% e5-deploy.py %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] exit code 0
) else (
    echo [FAIL] exit code %RC%   1=precheck 2=upload 3=install 4=verify
)

:end
echo.
if not defined E5_NOPAUSE pause
exit /b

rem ---- %1 = quoted exe path ----
:probe
if defined PY goto :eof
%1 -V >nul 2>&1
if errorlevel 1 goto :eof
if not defined PYFALL set "PYFALL=%1"
%1 -c "import paramiko" >nul 2>&1
if errorlevel 1 goto :eof
set "PY=%1"
goto :eof

