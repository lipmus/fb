@echo off
setlocal
cd /d "%~dp0"
title Facebook Auto Watch Manager

where py >nul 2>&1
if %errorlevel%==0 (set PY=py -3.12) else (set PY=python)

%PY% -c "import webview, playwright, psutil" >nul 2>&1
if not %errorlevel%==0 (
  echo [setup] memasang dependensi ^(sekali saja^)...
  %PY% -m pip install --upgrade pip >nul
  %PY% -m pip install -r requirements.txt || goto :err
)

rem Buka GUI lewat app.gui:run (bukan python -m app.cli gui) supaya paket ter-import benar
%PY% -c "from app.gui import run; run()"
if errorlevel 1 goto :err
exit /b 0

:err
echo.
echo Gagal menjalankan aplikasi. Cek pesan di atas.
pause
exit /b 1
