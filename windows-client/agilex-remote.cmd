@echo off
rem AgileX remote controller - Windows launcher.
rem All arguments are forwarded to agilex-remote-win.ps1.
rem Examples:
rem   agilex-remote.cmd
rem   agilex-remote.cmd start --host 192.168.1.50
rem   agilex-remote.cmd status
rem   agilex-remote.cmd check
rem   agilex-remote.cmd stop
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0agilex-remote-win.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo AgileX client exited with code %RC%.
  pause
)
exit /b %RC%
