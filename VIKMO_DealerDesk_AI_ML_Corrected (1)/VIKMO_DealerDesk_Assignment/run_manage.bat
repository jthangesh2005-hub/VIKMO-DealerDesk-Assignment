@echo off
REM Run Django commands from this project folder on Windows.
set "ROOT=%~dp0"
set "PYTHON="
if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON=%ROOT%.venv\Scripts\python.exe"
if not defined PYTHON if exist "%ROOT%venv\Scripts\python.exe" set "PYTHON=%ROOT%venv\Scripts\python.exe"
if not defined PYTHON set "PYTHON=python"
"%PYTHON%" "%ROOT%manage.py" %*
