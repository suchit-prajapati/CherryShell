# CherryShell

A small PowerShell-style shell written in Python, with an object pipeline.

```
ls | where Length -gt 100 | select Name,Length
ps | sort MemoryKB -Descending | select -First 3
gci | ? { $_.Length -gt 100 } | measure Length
$x = "cherry"; echo "hello $x"
```

## Run

```
python cherryshell.py            # interactive
python cherryshell.py script.cherry
```

Type `Get-Command` to list built-in commands. Press Tab to complete command names and paths; Up/Down arrows browse history (saved in `~/.cherryshell_history`, also `Get-History`). Anything else (git, pip, winget, ...) runs as an external program with live output.

## Build the Windows installer

Needs PyInstaller and prompt_toolkit (`pip install pyinstaller prompt_toolkit`) and Inno Setup (`winget install JRSoftware.InnoSetup`).

```
build.bat
```

Produces `dist\cherryshell.exe` and `dist\CherryShell-Setup.exe` (Start Menu shortcut, optional PATH entry, uninstaller).
