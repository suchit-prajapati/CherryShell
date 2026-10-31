@echo off
rem Builds dist\cherryshell.exe and dist\CherryShell-Setup.exe
setlocal EnableDelayedExpansion
cd /d "%~dp0"

for /f %%v in ('python -c "import cherryshell; print(cherryshell.__version__)"') do set "VERSION=%%v"
if "%VERSION%"=="" (
    echo Could not read __version__ from cherryshell.py
    exit /b 1
)
echo Building CherryShell %VERSION%

rem Standard-library modules that `py` code may use, so they are bundled into the exe
set "HIDDEN="
for %%m in (math statistics random decimal fractions collections itertools functools operator string textwrap hashlib base64 uuid pathlib zipfile gzip tarfile sqlite3 html xml.etree.ElementTree urllib.parse calendar pprint) do set "HIDDEN=!HIDDEN! --hidden-import %%m"
python -m PyInstaller --onefile --console --clean --name cherryshell !HIDDEN! cherryshell.py || exit /b 1

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup not found. Install it with: winget install JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" /DAppVersion=%VERSION% installer.iss || exit /b 1

echo.
echo Done: dist\CherryShell-Setup.exe
