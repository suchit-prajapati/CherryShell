@echo off
rem Builds dist\cherryshell.exe and dist\CherryShell-Setup.exe
setlocal
cd /d "%~dp0"

for /f %%v in ('python -c "import cherryshell; print(cherryshell.__version__)"') do set "VERSION=%%v"
if "%VERSION%"=="" (
    echo Could not read __version__ from cherryshell.py
    exit /b 1
)
echo Building CherryShell %VERSION%

python -m PyInstaller --onefile --console --clean --name cherryshell cherryshell.py || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup not found. Install it with: winget install JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" /DAppVersion=%VERSION% installer.iss || exit /b 1

echo.
echo Done: dist\CherryShell-Setup.exe
