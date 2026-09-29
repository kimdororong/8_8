@echo off
cd /d "%~dp0"
python cross_soccer.py
if errorlevel 1 (
  py cross_soccer.py
  pause
)
