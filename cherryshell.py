"""CherryShell - a small PowerShell-style shell with an object pipeline."""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime


class Obj:
    """A PowerShell-like object: ordered named properties, attribute access."""

    def __init__(self, **props):
        self.__dict__["_props"] = dict(props)

    def __getattr__(self, name):
        for k, v in self._props.items():
            if k.lower() == name.lower():
                return v
        return None

    def __setattr__(self, name, value):
        self._props[name] = value

    def props(self):
        return list(self._props.keys())

    def __repr__(self):
        return "@{" + "; ".join(f"{k}={v}" for k, v in self._props.items()) + "}"


def get_prop(item, name):
    if isinstance(item, Obj):
        return getattr(item, name)
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


# ---------------------------------------------------------------- tokenizer

def tokenize(line):
    tokens, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
        elif c == "#":
            break
        elif c in "|;=":
            tokens.append(("op", c))
            i += 1
        elif c in "\"'":
            j = i + 1
            while j < n and line[j] != c:
                j += 1
            tokens.append(("str" if c == "'" else "dstr", line[i + 1:j]))
            i = j + 1
        elif c == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(line[j], 0)
                j += 1
            tokens.append(("block", line[i + 1:j - 1].strip()))
            i = j
        else:
            j = i
            while j < n and not line[j].isspace() and line[j] not in "|;\"'{":
                if line[j] == "=" and j == i:
                    break
                j += 1
            tokens.append(("word", line[i:j]))
            i = j
    return tokens


def split_on(tokens, op):
    parts, cur = [], []
    for t in tokens:
        if t == ("op", op):
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    parts.append(cur)
    return parts


# ------------------------------------------------------------------ shell

OPS = {"-eq": "==", "-ne": "!=", "-gt": ">", "-ge": ">=", "-lt": "<", "-le": "<=",
       "-and": " and ", "-or": " or ", "-not": " not "}


class Shell:
    def __init__(self):
        self.vars = {"true": True, "false": False, "null": None,
                     "home": os.path.expanduser("~"), "pid": os.getpid()}
        self.running = True
        self.commands = {
            "get-childitem": self.get_childitem, "get-location": self.get_location,
            "set-location": self.set_location, "where-object": self.where_object,
            "sort-object": self.sort_object, "select-object": self.select_object,
            "measure-object": self.measure_object, "foreach-object": self.foreach_object,
            "write-output": self.write_output, "get-content": self.get_content,
            "get-process": self.get_process, "get-date": self.get_date,
            "get-variable": self.get_variable, "get-command": self.get_command,
            "format-table": self.format_table_cmd, "clear-host": self.clear_host,
            "exit": self.exit, "new-item": self.new_item, "remove-item": self.remove_item,
        }
        self.aliases = {
            "ls": "get-childitem", "dir": "get-childitem", "gci": "get-childitem",
            "pwd": "get-location", "cd": "set-location", "where": "where-object",
            "?": "where-object", "sort": "sort-object", "select": "select-object",
            "measure": "measure-object", "%": "foreach-object", "foreach": "foreach-object",
            "echo": "write-output", "cat": "get-content", "gc": "get-content",
            "ps": "get-process", "gcm": "get-command", "ft": "format-table",
            "cls": "clear-host", "clear": "clear-host", "quit": "exit",
            "mkdir": "new-item", "rm": "remove-item", "del": "remove-item",
        }

    # ---- evaluation helpers

    def expand(self, text):
        return re.sub(r"\$(\w+)", lambda m: str(self.vars.get(m.group(1).lower(), "")), text)

    def value(self, tok):
        kind, text = tok
        if kind == "str":
            return text
        if kind == "dstr":
            return self.expand(text)
        if kind == "block":
            return ("block", text)
        if text.startswith("$"):
            name, _, attr = text[1:].partition(".")
            v = self.vars.get(name.lower())
            return get_prop(v, attr) if attr else v
        for cast in (int, float):
            try:
                return cast(text)
            except ValueError:
                pass
        return text

    def eval_block(self, code, item):
        expr = re.sub(r"\$_\b", "_", code)
        expr = re.sub(r"\$(\w+)", lambda m: f"_v[{m.group(1).lower()!r}]", expr)
        for k, v in OPS.items():
            expr = re.sub(re.escape(k) + r"\b", v, expr, flags=re.I)
        expr = re.sub(r"(\w+)\s+-like\s+(['\"][^'\"]*['\"])", r"_like(\1, \2)", expr, flags=re.I)
        env = {"_": item, "_v": self.vars, "_like": _like, "True": True, "False": False}
        return eval(expr, {"__builtins__": {"len": len, "str": str, "int": int}}, env)

    @staticmethod
    def parse_args(args):
        pos, named = [], {}
        i = 0
        while i < len(args):
            a = args[i]
            if isinstance(a, str) and a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
                key = a[1:].lower()
                if i + 1 < len(args) and not (isinstance(args[i + 1], str) and args[i + 1].startswith("-")):
                    named[key] = args[i + 1]
                    i += 2
                else:
                    named[key] = True
                    i += 1
            else:
                pos.append(a)
                i += 1
        return pos, named

    # ---- execution

    def run_line(self, line):
        line = line.lstrip(chr(0xFEFF))  # BOM from piped input / Notepad files
        for stmt in split_on(tokenize(line), ";"):
            if not stmt:
                continue
            if len(stmt) >= 3 and stmt[0][0] == "word" and stmt[0][1].startswith("$") and stmt[1] == ("op", "="):
                result = self.run_pipeline(stmt[2:], capture=True)
                self.vars[stmt[0][1][1:].lower()] = result[0] if len(result) == 1 else result
            else:
                output(self.run_pipeline(stmt))

    def run_pipeline(self, tokens, capture=False):
        stages = split_on(tokens, "|")
        if len(stages) == 1 and stages[0] and stages[0][0][0] != "word":
            return [self.value(stages[0][0])]
        if len(stages) == 1 and stages[0] and stages[0][0][1].startswith("$"):
            v = self.value(stages[0][0])
            return v if isinstance(v, list) else [v]
        data = None
        for i, stage in enumerate(stages):
            if not stage:
                raise ShellError("empty pipeline element")
            name = stage[0][1]
            args = [self.value(t) for t in stage[1:]]
            live = not capture and i == len(stages) - 1
            data = self.invoke(name, args, data, live)
        return data or []

    def invoke(self, name, args, pipe, live=False):
        key = self.aliases.get(name.lower(), name.lower())
        if key in self.commands:
            pos, named = self.parse_args(args)
            return list(self.commands[key](pos, named, pipe or []))
        return self.run_external(name, args, pipe, live)

    def run_external(self, name, args, pipe, live=False):
        exe = shutil.which(name)
        if not exe:
            raise ShellError(f"The term '{name}' is not recognized as a command or program.")
        stdin = None
        if pipe:
            stdin = "\n".join(format_lines(pipe)) + "\n"
        argv = [exe] + [str(a) for a in args]
        if live:
            # Last command in the pipeline: give it the real console so progress
            # bars, colours and y/n prompts work like in PowerShell.
            try:
                subprocess.run(argv, input=stdin, text=True)
            except KeyboardInterrupt:
                print()
            return []
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, errors="replace")
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        return proc.stdout.splitlines()

    # ---- built-in cmdlets

    def get_childitem(self, pos, named, pipe):
        path = pos[0] if pos else "."
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
        for e in entries:
            st = e.stat()
            yield Obj(Mode="d----" if e.is_dir() else "-a---",
                      LastWriteTime=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                      Length=None if e.is_dir() else st.st_size, Name=e.name)

    def get_location(self, pos, named, pipe):
        yield Obj(Path=os.getcwd())

    def set_location(self, pos, named, pipe):
        os.chdir(os.path.expanduser(str(pos[0])) if pos else os.path.expanduser("~"))
        return []

    def where_object(self, pos, named, pipe):
        if pos and isinstance(pos[0], tuple):
            return [x for x in pipe if self.eval_block(pos[0][1], x)]
        # Where-Object Prop -gt Value
        if not pos:
            raise ShellError("Where-Object needs a condition")
        prop = pos[0]
        op, val = next(((k, v) for k, v in named.items()), (None, None))
        if op is None:
            return [x for x in pipe if get_prop(x, prop)]
        if op == "like":
            return [x for x in pipe if _like(get_prop(x, prop), val)]
        if op == "match":
            return [x for x in pipe if re.search(str(val), str(get_prop(x, prop)), re.I)]
        pyop = OPS.get("-" + op)
        if not pyop:
            raise ShellError(f"unsupported operator -{op}")
        return [x for x in pipe if _compare(get_prop(x, prop), pyop, val)]

    def sort_object(self, pos, named, pipe):
        prop = pos[0] if pos else None
        key = (lambda x: _sort_key(get_prop(x, prop))) if prop else _sort_key
        return sorted(pipe, key=key, reverse=bool(named.get("descending")))

    def select_object(self, pos, named, pipe):
        items = list(pipe)
        if "first" in named:
            items = items[:int(named["first"])]
        if "last" in named:
            items = items[-int(named["last"]):]
        if pos:
            props = [p.strip() for a in pos for p in str(a).split(",") if p.strip()]
            items = [Obj(**{p: get_prop(x, p) for p in props}) for x in items]
        return items

    def measure_object(self, pos, named, pipe):
        items = list(pipe)
        result = {"Count": len(items)}
        if pos:
            nums = [get_prop(x, pos[0]) for x in items]
            nums = [n for n in nums if isinstance(n, (int, float))]
            result.update(Sum=sum(nums), Average=round(sum(nums) / len(nums), 2) if nums else None,
                          Maximum=max(nums, default=None), Minimum=min(nums, default=None),
                          Property=pos[0])
        yield Obj(**result)

    def foreach_object(self, pos, named, pipe):
        if not pos or not isinstance(pos[0], tuple):
            raise ShellError("ForEach-Object needs a { script block }")
        for x in pipe:
            yield self.eval_block(pos[0][1], x)

    def write_output(self, pos, named, pipe):
        return list(pipe) + [p for p in pos]

    def get_content(self, pos, named, pipe):
        with open(pos[0], encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()

    def get_process(self, pos, named, pipe):
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
            for row in out.splitlines():
                cols = [c.strip('"') for c in row.split('","')]
                if len(cols) >= 5:
                    mem = int(re.sub(r"\D", "", cols[4]) or 0)
                    yield Obj(Id=int(cols[1]), ProcessName=cols[0], MemoryKB=mem)
        else:
            out = subprocess.run(["ps", "-eo", "pid,rss,comm"], capture_output=True, text=True).stdout
            for row in out.splitlines()[1:]:
                pid, rss, comm = row.split(None, 2)
                yield Obj(Id=int(pid), ProcessName=os.path.basename(comm), MemoryKB=int(rss))

    def get_date(self, pos, named, pipe):
        yield datetime.now().strftime("%A, %d %B %Y %H:%M:%S")

    def get_variable(self, pos, named, pipe):
        for k, v in self.vars.items():
            yield Obj(Name=k, Value=v)

    def get_command(self, pos, named, pipe):
        rev = {}
        for a, c in self.aliases.items():
            rev.setdefault(c, []).append(a)
        for c in sorted(self.commands):
            yield Obj(Name=_title(c), Aliases=", ".join(rev.get(c, [])))

    def format_table_cmd(self, pos, named, pipe):
        return format_lines(list(pipe))

    def clear_host(self, pos, named, pipe):
        os.system("cls" if os.name == "nt" else "clear")
        return []

    def exit(self, pos, named, pipe):
        self.running = False
        return []

    def new_item(self, pos, named, pipe):
        path = pos[0]
        if named.get("itemtype", "directory").lower() == "file":
            open(path, "a").close()
        else:
            os.makedirs(path, exist_ok=True)
        parent = os.path.dirname(os.path.abspath(path))
        return [x for x in self.get_childitem([parent], {}, []) if x.Name == os.path.basename(path)]

    def remove_item(self, pos, named, pipe):
        targets = pos or [get_prop(x, "Name") for x in pipe]
        for t in targets:
            if os.path.isdir(t):
                if named.get("recurse"):
                    shutil.rmtree(t)
                else:
                    os.rmdir(t)
            else:
                os.remove(t)
        return []


class ShellError(Exception):
    pass


# --------------------------------------------------------------- helpers

def _title(name):
    return "-".join(p.capitalize() for p in name.split("-"))


def _like(value, pattern):
    regex = "^" + re.escape(str(pattern)).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(regex, str(value), re.I) is not None


def _compare(a, op, b):
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.lower(), b.lower()
    try:
        return eval(f"_a {op} _b", {}, {"_a": a, "_b": b})
    except TypeError:
        return False


def _sort_key(v):
    if v is None:
        return (0, "")
    if isinstance(v, (int, float)):
        return (1, v)
    return (2, str(v).lower())


def format_lines(items):
    """Render pipeline output like PowerShell: tables for objects, plain text otherwise."""
    lines, objs = [], []

    def flush():
        if not objs:
            return
        cols = []
        for o in objs:
            for p in o.props():
                if p not in cols:
                    cols.append(p)
        cells = [["" if get_prop(o, c) is None else str(get_prop(o, c)) for c in cols] for o in objs]
        widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
        numeric = [all(isinstance(get_prop(o, c), (int, float)) or get_prop(o, c) is None for o in objs)
                   for c in cols]
        fmt = lambda row: "  ".join(v.rjust(w) if num else v.ljust(w)
                                    for v, w, num in zip(row, widths, numeric)).rstrip()
        lines.extend(["", fmt(cols), fmt(["-" * len(c) for c in cols])])
        lines.extend(fmt(r) for r in cells)
        lines.append("")
        objs.clear()

    for item in items:
        if isinstance(item, Obj):
            if objs and objs[0].props() != item.props():
                flush()
            objs.append(item)
        else:
            flush()
            lines.append("" if item is None else str(item))
    flush()
    return lines


def output(items):
    for line in format_lines(items):
        print(line)


# ---------------------------------------------------------------- main

RED, CHERRY, RESET = "\033[91m", "\033[38;5;161m", "\033[0m"


def main():
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(errors="replace")
    shell = Shell()
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8-sig") as f:
            for line in f:
                shell.run_line(line)
        return
    print(f"{CHERRY}CherryShell 0.1{RESET} - type Get-Command for commands, exit to quit.\n")
    while shell.running:
        try:
            line = input(f"{CHERRY}CS{RESET} {os.getcwd()}> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        try:
            shell.run_line(line)
        except ShellError as e:
            print(f"{RED}{e}{RESET}")
        except Exception as e:
            print(f"{RED}{type(e).__name__}: {e}{RESET}")


if __name__ == "__main__":
    main()
