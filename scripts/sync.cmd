@echo off
REM Refresh the local item database from tarkov.dev.
cd /d "%~dp0\.."
python -m tarkov_tools.cli sync
pause
