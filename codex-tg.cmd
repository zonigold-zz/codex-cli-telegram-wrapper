@echo off
setlocal
REM Preserve the current project working directory and its Python environment.
if defined CODEX_TG_PYTHON (
  "%CODEX_TG_PYTHON%" "%~dp0codex_tg_bridge.py" %*
  exit /b
)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python "%~dp0codex_tg_bridge.py" %*
  exit /b
)
py -3 "%~dp0codex_tg_bridge.py" %*
exit /b
