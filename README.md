# CherryShell

A small PowerShell-style shell written in Python, with an object pipeline.

```
ls | where Length -gt 100 | select Name,Length
ps | sort MemoryKB -Descending | select -First 3
gci | ? { $_.Length -gt 100 } | measure Length
$x = "cherry"; echo "hello $x"
```

## Scripting

PowerShell-style statements, with Python for the expressions. Use them at the prompt (an open `{` continues on the next line) or in a `.cherry` file run with `cherryshell script.cherry`.

```powershell
$files = ls | where { $_.Length -gt 0 }
foreach ($f in $files) { echo "$($f.Name): $($f.Length) bytes" }

if ($x -gt 10) { echo "big" } elseif ($x -ge 5) { echo "medium" } else { echo "small" }

for ($i = 3; $i -gt 0; $i--) { echo "T-minus $i" }
while ($n -lt 100) { $n += 7; if ($n % 2 -eq 0) { continue }; echo $n }

function greet($who, $times = 1) {
    foreach ($t in 1..$times) { echo "Hello $who" }
}
greet "World" -times 2

function fact($n) { if ($n -le 1) { return 1 }; return $n * (fact ($n - 1)) }
echo "5! = $(fact 5)"
```

- Operators: `-eq -ne -gt -ge -lt -le -like -notlike -match -notmatch -contains -in -and -or -not`, ranges `1..10`, `++ -- += -= *= /=`.
- Anything else in an expression is Python: `math.sqrt($x)`, `len($list)`, `$name.upper()`, `[1, 2, 3]`. A comma list needs brackets after `-in`: `"a" -in ("a", "b")`.
- Functions return everything they output; `$args` holds extra arguments and `$input` the pipeline input. Variables set inside a function stay local.
- Also: `break`, `continue`, `return`, `exit`, `Test-Path`, `Read-Host`, `Start-Sleep`, `Get-Function`.

## Python in the pipeline

CherryShell runs on Python, so Python is part of the shell. `py` evaluates an expression where `_` is the list of pipeline items (or one item at a time with `-Each`). Modules like `math`, `json` or `statistics` are imported automatically.

```
ls | py "sum(x.Length or 0 for x in _)"
cat data.csv | from-csv | where Price -gt 100
from-csv data.csv | py "statistics.mean(x.Price for x in _)"
ls | py -Each "_.Name.upper()"
get-json https://api.github.com/repos/suchit-prajapati/CherryShell | select name,stargazers_count
py "a = 21"; py "a * 2"
```

Also: `to-csv`, `from-json`, `to-json` (pass a file name to read from or write to a file). The installer bundles common standard-library modules; to use pip packages such as `requests` or `pandas`, run CherryShell from source with `python cherryshell.py`.

## Server manager

Save servers once, then connect by name or run a command on all of them in parallel. Uses the OpenSSH client that ships with Windows.

```
Add-Server web1 deploy@203.0.113.10 -Tags web,prod
Add-Server db1 admin@203.0.113.20 -Port 2222 -Key ~/.ssh/id_ed25519 -Tags db
servers                                 # list saved servers
connect web1                            # interactive SSH session
Invoke-Server "uptime" -Tag web         # run on every web server at once
Invoke-Server "df -h /" | where Output -like *9?%*
server-status -Detailed                 # online/offline, ping, load, memory, disk
```

`Invoke-Server` and `server-status -Detailed` need key-based SSH login (no password prompts). Servers are stored in `~/.cherryshell/servers.json`.

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
4. For winget, copy `winget/manifests/c/CherryShell/CherryShell/0.2` to a new version folder, update the version, URL and `InstallerSha256` (from the release's `.sha256` file), and open a PR to [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).

## License

MIT, see [LICENSE](LICENSE).
