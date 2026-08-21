@echo off
REM Start both background helpers in their own windows.
cd /d "%~dp0\.."
start "Tarkov gamma"   cmd /c python -m tarkov_tools.cli gamma watch
start "Tarkov popover" cmd /c python -m tarkov_tools.cli popover
