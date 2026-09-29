@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (python cross_soccer.py) else (py cross_soccer.py)
if errorlevel 1 pause
