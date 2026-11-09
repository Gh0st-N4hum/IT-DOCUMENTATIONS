echo off
cd /d "%~dp0"
title Scan Sorter
python scan_sorter_ui.py
if errorlevel 1 (
  echo.
  echo Scan Sorter stopped with an error. Read the message above.
  pause
)
 