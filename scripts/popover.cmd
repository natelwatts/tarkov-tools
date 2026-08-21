@echo off
REM Hotkey-summoned Tarkov search popover (default: Ctrl+Alt+T).
REM Requires Tarkov in BORDERLESS WINDOWED mode to appear over the game.
cd /d "%~dp0\.."
python -m tarkov_tools.cli popover
