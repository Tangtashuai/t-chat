@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>nul
cd /d "%~dp0"

rem t-chat1.3 launcher (Python 3.6 - 3.13)
rem Author: Tang at Shuai
rem Bilibili: https://space.bilibili.com/3546392266017069
rem This batch file is ASCII-only on purpose, so it works with any cmd codepage.

echo ==========================================
echo   t-chat1.3 launcher
echo   Author: Tang at Shuai
echo   Bilibili: https://space.bilibili.com/3546392266017069
echo ==========================================
echo.

set "PY="
where python >nul 2>nul
if errorlevel 1 goto try_py
python -c "import sys" >nul 2>nul
if errorlevel 1 goto try_py
set "PY=python"
goto python_found

:try_py
where py >nul 2>nul
if errorlevel 1 goto try_python3
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto try_python3
set "PY=py -3"
goto python_found

:try_python3
where python3 >nul 2>nul
if errorlevel 1 goto no_python
python3 -c "import sys" >nul 2>nul
if errorlevel 1 goto no_python
set "PY=python3"
goto python_found

:no_python
echo [ERROR] No working Python 3 was found.
echo.
echo Please install Python 3.6 or newer from:
echo   https://www.python.org/downloads/windows/
echo.
echo Important:
echo   1. During setup check "Add python.exe to PATH".
echo   2. If "python" opens Microsoft Store, disable the Store
echo      python.exe alias in Windows Settings, or install from python.org.
echo   3. You can also try the Python launcher: py -3 --version
echo.
pause
exit /b 1

:python_found
echo [OK] Python: %PY%
%PY% --version
%PY% -c "import sys;raise SystemExit(0 if sys.version_info >= (3,6) else 1)"
if errorlevel 1 goto python_old

rem Pick the requirements file that matches this Python version.
set "REQ=requirements-py36.txt"
%PY% -c "import sys;raise SystemExit(0 if sys.version_info >= (3,7) else 1)"
if not errorlevel 1 set "REQ=requirements-legacy.txt"
%PY% -c "import sys;raise SystemExit(0 if sys.version_info >= (3,8) else 1)"
if not errorlevel 1 set "REQ=requirements.txt"
echo [INFO] Requirements: %REQ%

rem If the system Python already has compatible dependencies, use it directly.
%PY% check_deps.py >nul 2>nul
if not errorlevel 1 goto use_system

if exist ".venv\Scripts\python.exe" goto venv_ready

echo [1/3] Creating virtual environment (.venv) ...
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv
if errorlevel 1 goto venv_failed

:venv_ready
set "PYEXE=.venv\Scripts\python.exe"
set "PIPFLAGS="
%PYEXE% -c "import sys" >nul 2>nul
if errorlevel 1 goto venv_failed
goto deps

:use_system
set "PYEXE=%PY%"
echo [1/3] System Python already has compatible dependencies.
goto run_server

:venv_failed
echo [WARN] Could not create or use .venv.
echo        Falling back to system Python: %PY%
echo        If the next step fails, reinstall Python from python.org
echo        with the "venv" and "pip" components enabled.
set "PYEXE=%PY%"
set "PIPFLAGS=--user"
goto deps

:deps
echo [2/3] Checking dependencies ...
%PYEXE% check_deps.py >nul 2>nul
if not errorlevel 1 goto run_server

echo       Installing dependencies from %REQ% ...
%PYEXE% -m pip install --disable-pip-version-check %PIPFLAGS% -r %REQ%
if not errorlevel 1 goto verify_deps
if /I "%REQ%"=="requirements.txt" goto try_legacy
if /I "%REQ%"=="requirements-legacy.txt" goto try_py36
goto dep_failed

:try_legacy
echo [WARN] requirements.txt failed, trying requirements-legacy.txt ...
set "REQ=requirements-legacy.txt"
%PYEXE% -m pip install --disable-pip-version-check %PIPFLAGS% -r %REQ%
if not errorlevel 1 goto verify_deps

:try_py36
echo [WARN] Trying requirements-py36.txt ...
set "REQ=requirements-py36.txt"
%PYEXE% -m pip install --disable-pip-version-check %PIPFLAGS% -r %REQ%
if not errorlevel 1 goto verify_deps
goto dep_failed

:verify_deps
%PYEXE% check_deps.py >nul 2>nul
if errorlevel 1 goto dep_failed
goto run_server

:dep_failed
echo.
echo [ERROR] Dependency installation failed.
echo Try manually:
echo   %PYEXE% -m pip install %PIPFLAGS% -r %REQ%
echo   %PYEXE% -m pip install %PIPFLAGS% -r %REQ% -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.
pause
exit /b 1

:run_server
echo [3/3] Starting server ...
echo.
echo Local:  http://127.0.0.1:5000
echo Admin:  http://127.0.0.1:5000/admin
echo Press Ctrl+C to stop.
echo.
set "PYTHONIOENCODING=utf-8"
%PYEXE% server.py
echo.
echo Server stopped.
pause
exit /b 0

:python_old
echo [ERROR] Python 3.6 or newer is required.
echo Current version:
%PY% --version
echo Please install a newer Python from https://www.python.org/downloads/windows/
pause
exit /b 1
