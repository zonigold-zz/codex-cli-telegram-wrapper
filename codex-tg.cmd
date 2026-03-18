@echo off
setlocal

REM codex-tg.cmd
REM -----------------------------------------------------------------------------
REM Windows launcher for codex_tg_bridge.py.
REM
REM Important behavior:
REM   - The current working directory is intentionally preserved.
REM     Codex will work in the folder where you launch this file from.
REM   - Extra arguments are forwarded to the bridge script, which then forwards
REM     unknown arguments to `codex exec`.
REM
REM Common examples:
REM   codex-tg.cmd --tg-discover-ids
REM   codex-tg.cmd --full-auto --model gpt-5-codex --search
REM   codex-tg.cmd --tg-no-topic --full-auto
REM -----------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"

REM Prefer the Windows Python launcher when available.
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 goto run_with_py

REM Fall back to `python` in PATH.
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 goto run_with_python

echo [ERROR] Python was not found in PATH.
echo [ERROR] Install Python 3 and make sure `py` or `python` is available.
endlocal & exit /b 9009

:run_with_py
py -3 "%SCRIPT_DIR%codex_tg_bridge.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%

:run_with_python
python "%SCRIPT_DIR%codex_tg_bridge.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
