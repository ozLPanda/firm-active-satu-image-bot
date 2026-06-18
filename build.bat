@echo off
setlocal

cd /d "%~dp0"

tasklist /FI "IMAGENAME eq satu-image-bot.exe" 2>nul | find /I "satu-image-bot.exe" >nul
if not errorlevel 1 goto :bot_running

set "PYTHON=python"
where python >nul 2>nul
if errorlevel 1 (
  set "PYTHON=py -3"
  where py >nul 2>nul
  if errorlevel 1 goto :no_python
)

set PLAYWRIGHT_BROWSERS_PATH=0
for /f "usebackq delims=" %%I in (`%PYTHON% -c "import pathlib, sysconfig; print(pathlib.Path(sysconfig.get_config_var('prefix')) / 'tcl')"`) do set "PY_TCL_ROOT=%%I"
if exist "%PY_TCL_ROOT%\tcl8.6" set "TCL_LIBRARY=%PY_TCL_ROOT%\tcl8.6"
if exist "%PY_TCL_ROOT%\tk8.6" set "TK_LIBRARY=%PY_TCL_ROOT%\tk8.6"

%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 goto :error

%PYTHON% -m playwright install chromium
if errorlevel 1 goto :error

%PYTHON% -m PyInstaller --noconfirm --clean satu-image-bot.spec
if errorlevel 1 goto :error

echo.
echo Build completed:
echo %~dp0dist\satu-image-bot\satu-image-bot.exe
echo.
pause

endlocal
exit /b 0

:no_python
echo.
echo Build failed: Python was not found. Install Python 3 or the Python launcher and try again.
echo.
pause
endlocal
exit /b 1

:bot_running
echo.
echo Build stopped: satu-image-bot.exe is currently running.
echo Close the bot window and all related browser windows, then run build.bat again.
echo.
pause
endlocal
exit /b 1

:error
set "EXIT_CODE=%errorlevel%"
echo.
echo Build failed with error code %EXIT_CODE%.
echo Check the messages above for details.
echo.
pause
endlocal
exit /b %EXIT_CODE%
