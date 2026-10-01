@echo off
rem lantern launcher — written by `lantern init`; edits here are overwritten.
rem
rem The Windows counterpart of lantern.launcher.sh: binds this command to the
rem home it lives in and runs the venv's lantern. There are no secrets in this
rem file — lantern reads config\secrets.env itself, and nothing is exported to
rem other processes.
rem
rem Docker's per-user MSI owns sbx.exe outside this home; the Python client
rem resolves it from LOCALAPPDATA. No sbx wrapper is needed beside this one.
setlocal
set "LANTERN_HOME=%~dp0.."
set "PATH=%~dp0;%PATH%"
set "TMPDIR=%LANTERN_HOME%\tmp"
set "TEMP=%LANTERN_HOME%\tmp"
set "TMP=%LANTERN_HOME%\tmp"
set "PIP_CACHE_DIR=%LANTERN_HOME%\cache\pip"
set "UV_CACHE_DIR=%LANTERN_HOME%\cache\uv"
set "UV_PYTHON_INSTALL_DIR=%LANTERN_HOME%\python"
set "COPILOT_CLI_EXTRACT_DIR=%LANTERN_HOME%\cache\copilot-sdk"
if not exist "%LANTERN_HOME%\venv\Scripts\lantern.exe" (
  echo lantern is not installed in %LANTERN_HOME% ^(expected venv\Scripts\lantern.exe^); run: lantern init 1>&2
  exit /b 127
)
"%LANTERN_HOME%\venv\Scripts\lantern.exe" %*
exit /b %ERRORLEVEL%
