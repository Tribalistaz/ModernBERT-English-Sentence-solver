@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

set "OPEN_BROWSER=1"
set "PAUSE_AT_END=1"
set "VALIDATE_ONLY=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--no-browser" set "OPEN_BROWSER=0"
if /I "%~1"=="--no-pause" set "PAUSE_AT_END=0"
if /I "%~1"=="--validate-only" set "VALIDATE_ONLY=1"
shift
goto parse_args
:args_done

set "HOST=127.0.0.1"
set "PORT=8080"
set "PY_HOST=127.0.0.1"
set "PY_PORT=65432"

set "PY_ROOT=%ROOT%\source\python"
set "WEBROOT=%ROOT%\source\webroot"
set "WEB_ENV=%WEBROOT%\.env"
set "ROOT_ENV=%ROOT%\.env"

echo ========================================
echo  Sentence Rebuilder - Bitbucket Start
echo ========================================
echo.

if not exist "%PY_ROOT%\predictor_server.py" (
    echo Missing Python server file:
    echo   %PY_ROOT%\predictor_server.py
    goto fail
)

if not exist "%WEBROOT%\frontend\index.html" (
    echo Missing frontend file:
    echo   %WEBROOT%\frontend\index.html
    goto fail
)

if not exist "%WEBROOT%\backend\process_socket_w_history.php" (
    echo Missing backend endpoint:
    echo   %WEBROOT%\backend\process_socket_w_history.php
    goto fail
)

if not exist "%WEB_ENV%" (
    if exist "%ROOT_ENV%" (
        copy /Y "%ROOT_ENV%" "%WEB_ENV%" >nul
        echo Created %WEB_ENV% from root .env
    ) else (
        echo Warning: %WEB_ENV% not found and root .env missing.
        echo          Backend may fail DB connection until .env is configured.
    )
)

set "PYTHON_BIN="
set "PHP_EXE="
set "PHP_INI="

call :read_env_key "%WEB_ENV%" "PYTHON_BIN" PYTHON_BIN
if not defined PYTHON_BIN call :read_env_key "%ROOT_ENV%" "PYTHON_BIN" PYTHON_BIN

call :read_env_key "%WEB_ENV%" "PHP_EXE" PHP_EXE
if not defined PHP_EXE call :read_env_key "%ROOT_ENV%" "PHP_EXE" PHP_EXE

call :read_env_key "%WEB_ENV%" "PHP_INI" PHP_INI
if not defined PHP_INI call :read_env_key "%ROOT_ENV%" "PHP_INI" PHP_INI

if not defined PYTHON_BIN set "PYTHON_BIN=python"
if not defined PHP_EXE set "PHP_EXE=php"
if not defined PHP_INI if exist "%ROOT%\php\php.ini" set "PHP_INI=%ROOT%\php\php.ini"

call :resolve_command "%PYTHON_BIN%" PYTHON_CMD
if errorlevel 1 (
    echo Python executable not found: %PYTHON_BIN%
    echo Set PYTHON_BIN in %WEB_ENV% or root .env.
    goto fail
)

call :resolve_command "%PHP_EXE%" PHP_CMD
if errorlevel 1 (
    echo PHP executable not found: %PHP_EXE%
    echo Set PHP_EXE in %WEB_ENV% or root .env.
    goto fail
)

if defined PHP_INI (
    if not exist "%PHP_INI%" (
        echo Warning: PHP_INI not found: %PHP_INI%
        echo          Starting PHP without explicit php.ini.
        set "PHP_INI="
    )
)

if "%VALIDATE_ONLY%"=="1" (
    echo Validation OK.
    echo PY_ROOT   = %PY_ROOT%
    echo WEBROOT   = %WEBROOT%
    echo PYTHON    = %PYTHON_CMD%
    echo PHP       = %PHP_CMD%
    if defined PHP_INI (echo PHP_INI   = %PHP_INI%) else (echo PHP_INI   = ^(default^))
    echo URL       = http://%HOST%:%PORT%/frontend/index.html
    if "%PAUSE_AT_END%"=="1" pause
    exit /b 0
)

echo [1/2] Starting Python Socket Server...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"%PY_HOST%:%PY_PORT% .*LISTENING"') do (
    tasklist /FI "PID eq %%P" | findstr /I "python.exe" >nul
    if not errorlevel 1 (
        echo Restarting old Python listener on %PY_HOST%:%PY_PORT% PID %%P
        taskkill /PID %%P /F >nul 2>&1
    )
)

start "Python Server (Bitbucket)" /D "%PY_ROOT%" "%PYTHON_CMD%" predictor_server.py

echo Waiting for Python server on %PY_HOST%:%PY_PORT% (model load can take time)...
set /a PY_WAIT=0
:wait_python
netstat -ano | findstr /R /C:"%PY_HOST%:%PY_PORT% .*LISTENING" >nul
if not errorlevel 1 goto python_ready
timeout /t 2 /nobreak >nul
set /a PY_WAIT+=2
if %PY_WAIT% GEQ 180 goto python_fail
goto wait_python

:python_ready
echo Python server is ready.
echo.
echo [2/2] Starting PHP Web Server...

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:"%HOST%:%PORT% .*LISTENING"') do (
    tasklist /FI "PID eq %%P" | findstr /I "php.exe" >nul
    if not errorlevel 1 (
        echo Restarting old PHP listener on %HOST%:%PORT% PID %%P
        taskkill /PID %%P /F >nul 2>&1
    )
)

netstat -ano | findstr /R /C:"%HOST%:%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo Port %PORT% is already in use by another process.
    echo Stop that process or change PORT in this script.
    goto fail
)

if defined PHP_INI (
    start "PHP Server (Bitbucket)" /D "%ROOT%" "%PHP_CMD%" -c "%PHP_INI%" -S %HOST%:%PORT% -t "%WEBROOT%"
) else (
    start "PHP Server (Bitbucket)" /D "%ROOT%" "%PHP_CMD%" -S %HOST%:%PORT% -t "%WEBROOT%"
)

set /a PHP_WAIT=0
:wait_php
netstat -ano | findstr /R /C:"%HOST%:%PORT% .*LISTENING" >nul
if not errorlevel 1 goto php_ready
timeout /t 2 /nobreak >nul
set /a PHP_WAIT+=2
if %PHP_WAIT% GEQ 45 goto php_fail
goto wait_php

:php_ready
echo PHP server is ready.
echo.
if "%OPEN_BROWSER%"=="1" (
    echo Opening browser...
    start "" "http://%HOST%:%PORT%/frontend/index.html"
    echo.
)
echo Keep both windows open:
echo 1. Python Server (Bitbucket)
echo 2. PHP Server (Bitbucket)
echo.
if "%PAUSE_AT_END%"=="1" pause
exit /b 0

:python_fail
echo.
echo Python server did not start in time.
echo Manual fallback:
echo   cd /d "%PY_ROOT%"
echo   "%PYTHON_CMD%" predictor_server.py
goto fail

:php_fail
echo.
echo PHP server did not bind to %HOST%:%PORT%.
echo Check the "PHP Server (Bitbucket)" window for the real error.
echo Manual fallback:
if defined PHP_INI (
    echo   "%PHP_CMD%" -c "%PHP_INI%" -S %HOST%:%PORT% -t "%WEBROOT%"
) else (
    echo   "%PHP_CMD%" -S %HOST%:%PORT% -t "%WEBROOT%"
)
goto fail

:fail
echo.
if "%PAUSE_AT_END%"=="1" pause
exit /b 1

:read_env_key
setlocal EnableExtensions DisableDelayedExpansion
set "FILE=%~1"
set "KEY=%~2"
set "VAL="
if exist "%FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /R /I /B "%KEY%=" "%FILE%"`) do (
        if /I "%%~A"=="%KEY%" set "VAL=%%~B"
    )
)
if defined VAL (
    if "%VAL:~0,1%"=="\"" if "%VAL:~-1%"=="\"" set "VAL=%VAL:~1,-1%"
)
endlocal & set "%~3=%VAL%"
exit /b 0

:resolve_command
setlocal EnableExtensions DisableDelayedExpansion
set "CANDIDATE=%~1"
set "RESOLVED="
if exist "%CANDIDATE%" set "RESOLVED=%CANDIDATE%"
if not defined RESOLVED (
    for /f "delims=" %%P in ('where "%CANDIDATE%" 2^>nul') do (
        set "RESOLVED=%%~fP"
        goto :resolve_done
    )
)
:resolve_done
if defined RESOLVED (
    endlocal & set "%~2=%RESOLVED%" & exit /b 0
)
endlocal & exit /b 1
