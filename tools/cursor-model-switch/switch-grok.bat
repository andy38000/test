@echo off
setlocal
cd /d "%~dp0"
py -3 "%~dp0cursor_mode_switch.py" grok %*
if not errorlevel 9009 exit /b %errorlevel%
python "%~dp0cursor_mode_switch.py" grok %*
exit /b %errorlevel%
