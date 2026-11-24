@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or later is required: https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
python -c "import pymupdf" >nul 2>nul
if errorlevel 1 (
  python -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
python service.py --open
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. See details above.
pause
exit /b 1
