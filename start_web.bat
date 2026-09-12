@echo off
rem ============================================================
rem   SellerSprite Deep-Dive Web Console launcher
rem   Double-click this file, then open http://127.0.0.1:8765
rem   (kept ASCII-only: cmd parses .bat in the ANSI codepage)
rem ============================================================
cd /d %~dp0
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.pw-browsers"
set "PYTHONIOENCODING=utf-8"

rem open the console page after the server has 3s to start
start "" cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8765"

echo ==============================================================
echo   SellerSprite Deep-Dive Web Console starting...
echo   URL: http://127.0.0.1:8765   (close this window to stop)
echo ==============================================================
".venv\Scripts\python.exe" web_server.py
pause
