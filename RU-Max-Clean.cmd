@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title RU Max Clean v1.6.0

rem Keep this CMD file ASCII-only. All localized UI is printed by Python.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYCMD="

call :FIND_PYTHON
if defined PYCMD goto :HAVE_PYTHON

echo ============================================================
echo RU Max Clean: Python setup
echo ============================================================
echo 64-bit Python 3.10 or newer was not found.
echo Trying an automatic installation with Windows Package Manager...
echo.

winget --version >nul 2>&1
if errorlevel 1 goto :NO_WINGET

call :INSTALL_PYTHON 3.13
if defined PYCMD goto :HAVE_PYTHON
call :INSTALL_PYTHON 3.14
if defined PYCMD goto :HAVE_PYTHON
call :INSTALL_PYTHON 3.12
if defined PYCMD goto :HAVE_PYTHON
call :INSTALL_PYTHON 3.11
if defined PYCMD goto :HAVE_PYTHON
goto :PY_FAIL

:HAVE_PYTHON
echo [OK] Python command: %PYCMD%
%PYCMD% bootstrap.py
if errorlevel 1 goto :BOOT_FAIL
%PYCMD% ru_max_launcher.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo RU Max Clean exited with code %RC%.
pause
exit /b %RC%

:FIND_PYTHON
set "PYCMD="
rem Prefer CPython 3.13 when it is already installed: indexed_bzip2 currently
rem has broader Windows wheel support there. Fall back to the newest Python.
py -3.13 -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,10) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3.13"
if defined PYCMD exit /b 0

py -3 -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,10) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD exit /b 0

python -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,10) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD exit /b 0

call :TRY_PYTHON_EXE "%LocalAppData%\Programs\Python\Python313\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "%LocalAppData%\Programs\Python\Python314\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "%LocalAppData%\Programs\Python\Python311\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "C:\Python313\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "C:\Python314\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "C:\Python312\python.exe"
if defined PYCMD exit /b 0
call :TRY_PYTHON_EXE "C:\Python311\python.exe"
exit /b 0

:TRY_PYTHON_EXE
if not exist "%~1" exit /b 0
"%~1" -c "import sys,struct; raise SystemExit(0 if sys.version_info >= (3,10) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=\"%~1\""
exit /b 0

:INSTALL_PYTHON
set "PYVER=%~1"
echo [SETUP] Trying Python %PYVER%...
winget install --id Python.Python.%PYVER% -e --scope user --accept-package-agreements --accept-source-agreements --silent
call :FIND_PYTHON
exit /b 0

:NO_WINGET
echo.
echo ERROR: winget is not available, so Python cannot be installed automatically.
echo Install 64-bit Python 3.10 or newer, then run RU-Max-Clean.cmd again.
pause
exit /b 10

:PY_FAIL
echo.
echo ERROR: automatic Python installation did not produce a usable 64-bit Python 3.10+.
echo Install Python manually and run RU-Max-Clean.cmd again.
pause
exit /b 10

:BOOT_FAIL
echo.
echo ERROR: Python environment setup failed.
echo The detailed error is shown above. Nothing was deleted.
pause
exit /b 12

