@echo off
rem Builds dist\cherryshell.exe and dist\CherryShell-Setup.exe
setlocal
cd /d "%~dp0"

python -m PyInstaller --onefile --console --clean --name cherryshell cherryshell.py || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup not found. Install it with: winget install JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" installer.iss || exit /b 1

echo.
echo Done: dist\CherryShell-Setup.exe
