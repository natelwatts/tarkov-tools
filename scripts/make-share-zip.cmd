@echo off
REM Build a shareable archive containing ONLY files tracked by git.
REM Zipping the folder by hand would include config.local.json (your
REM TarkovTracker token) and data\ (your database) - this cannot.
cd /d "%~dp0\.."
git archive --format=zip -o "%TEMP%\tarkov-tools.zip" HEAD
echo.
echo Wrote %TEMP%\tarkov-tools.zip
echo Contains tracked files only - no token, no personal database.
pause
