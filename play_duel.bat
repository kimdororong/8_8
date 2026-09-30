@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (python penalty_duel.py) else (py penalty_duel.py)
if errorlevel 1 pause
