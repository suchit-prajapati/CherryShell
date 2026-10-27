# CherryShell

A small PowerShell-style shell written in Python, with an object pipeline.

```
ls | where Length -gt 100 | select Name,Length
ps | sort MemoryKB -Descending | select -First 3
gci | ? { $_.Length -gt 100 } | measure Length
$x = "cherry"; echo "hello $x"
```

## Install

Download `CherryShell-Setup.exe` from the [latest release](https://github.com/suchit-prajapati/CherryShell/releases/latest) and run it.
Windows SmartScreen may warn because the installer is not code-signed yet: click **More info → Run anyway**.

## Run from source

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

## Releasing

1. Bump `__version__` in `cherryshell.py` and commit.
2. Push a matching tag: `git tag v0.3 && git push origin main v0.3`.
3. GitHub Actions builds the installer and attaches it to the `v0.3` release.
4. For winget, copy `winget/manifests/.../0.2` to a new version folder, update the version, URL and `InstallerSha256` (from the release's `.sha256` file), and open a PR to [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).

## License

MIT, see [LICENSE](LICENSE).
