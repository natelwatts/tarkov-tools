@echo off
REM Start the gamma watcher AND the search popover in one process.
REM Closing this window (or Ctrl-C) stops both and restores gamma.
cd /d "%~dp0\.."
uv run tarkov-tools start %*
