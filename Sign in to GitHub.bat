@echo off
title Sign in to GitHub
echo.
echo   You'll see a one-time code below.
echo   1. Copy the code
echo   2. Press Enter - your browser will open
echo   3. Paste the code and click Authorize
echo.
"C:\Program Files\GitHub CLI\gh.exe" auth login --hostname github.com --git-protocol https --web
echo.
echo   Done - you can close this window.
pause
