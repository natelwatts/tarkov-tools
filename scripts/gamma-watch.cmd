@echo off
REM Start the Tarkov gamma watcher. Gamma is applied only while the game
REM has focus, and always restored when this window is closed.
cd /d "%~dp0\.."
python -m tarkov_tools.cli gamma watch
