@echo off
title Sign in to Telegram (twice)
cd /d "%~dp0"
echo.
echo   You will sign in TWICE - once for this laptop, once for the cloud.
echo   Telegram sends a code to your Telegram app each time.
echo.
"C:\Users\hp\AppData\Local\Programs\Python\Python312\python.exe" first_time_login.py
echo.
pause
