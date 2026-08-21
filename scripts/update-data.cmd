@echo off
REM Rebuild the item database from the game's raw templates. Run after a patch.
cd /d "%~dp0\.."
uv run tarkov-tools import-templates --download
pause
