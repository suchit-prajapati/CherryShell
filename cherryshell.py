"""CherryShell - a small PowerShell-style shell with an object pipeline."""
__version__ = "0.3"

import ast
import csv
import glob
import importlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


class Obj:
    """A PowerShell-like object: ordered named properties, attribute access."""

    def __init__(self, **props):
        self.__dict__["_props"] = dict(props)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        for k, v in self._props.items():
            if k.lower() == name.lower():
                return v
        return None

    def __getitem__(self, name):  # lets `py` code use x["Name"] as well as x.Name
        return getattr(self, name)

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
        elif c in "{(":
            j = scan_balanced(line, i)
            tokens.append(("block" if c == "{" else "paren", line[i + 1:j - 1].strip()))
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


CLOSERS = {"{": "}", "(": ")", "[": "]"}


def scan_balanced(text, i):
    """text[i] is an opening bracket; return the index just past its match (quote-aware)."""
    stack, j, n = [], i, len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = text.find(c, j + 1)
            if j < 0:
                return n
        elif c == "#" and (j == 0 or text[j - 1] in " \t\n;{"):
            j = text.find("\n", j)
            if j < 0:
                return n
            continue
        elif c in CLOSERS:
            stack.append(CLOSERS[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return n


def is_incomplete(text):
    """True while a typed block still has an open bracket or quote, so the REPL asks for more."""
    depth, j, n = 0, 0, len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = text.find(c, j + 1)
            if j < 0:
                return True
        elif c == "#" and (j == 0 or text[j - 1] in " \t\n;{"):
            j = text.find("\n", j)
            if j < 0:
                break
            continue
        elif c in CLOSERS:
            depth += 1
        elif c in ")}]":
            depth -= 1
        j += 1
    return depth > 0


def tokens_to_text(tokens):
    parts = []
    for kind, text in tokens:
        parts.append({"str": f"'{text}'", "dstr": f'"{text}"', "block": "{" + text + "}",
                      "paren": f"({text})"}.get(kind, text))
    return " ".join(parts)


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


# ---------------------------------------------------------------- scripting

class ReturnSignal(Exception):
    def __init__(self, items):
        self.items = items


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


KEYWORDS = ("if", "foreach", "while", "for", "function", "return", "break", "continue")


class ScriptParser:
    """Splits script text into statements. Block bodies stay as text and are parsed when run."""

    def __init__(self, text):
        self.text, self.i = text, 0

    def error(self, msg):
        line = self.text.count("\n", 0, self.i) + 1
        raise ShellError(f"Parse error on line {line}: {msg}")

    def skip(self, newlines=True):
        while self.i < len(self.text):
            c = self.text[self.i]
            if c == "#":
                end = self.text.find("\n", self.i)
                self.i = len(self.text) if end < 0 else end
            elif c in " \t\r" or (newlines and c in "\n;"):
                self.i += 1
            else:
                break

    def keyword(self):
        m = re.match(r"([A-Za-z]+)(?![\w-])", self.text[self.i:])
        return m.group(1).lower() if m and m.group(1).lower() in KEYWORDS + ("elseif", "else") else None

    def bracket(self, opener):
        self.skip()
        if self.i >= len(self.text) or self.text[self.i] != opener:
            self.error(f"expected '{opener}'")
        end = scan_balanced(self.text, self.i)
        if end > len(self.text) or self.text[end - 1] != CLOSERS[opener]:
            self.error(f"missing '{CLOSERS[opener]}'")
        inner, self.i = self.text[self.i + 1:end - 1], end
        return inner

    def rest_of_statement(self):
        start, n = self.i, len(self.text)
        while self.i < n and self.text[self.i] not in "\n;":
            c = self.text[self.i]
            if c in CLOSERS or c in "\"'":
                self.i = scan_balanced(self.text, self.i) if c in CLOSERS else \
                    (self.text.find(c, self.i + 1) + 1 or n)
            elif c == "#" and self.text[self.i - 1:self.i] in (" ", "\t", ""):
                break
            else:
                self.i += 1
        return self.text[start:self.i].strip()

    def statements(self):
        out = []
        while True:
            self.skip()
            if self.i >= len(self.text):
                return out
            out.append(self.statement())

    def statement(self):
        kw = self.keyword()
        if kw:
            self.i += len(kw)
        if kw == "if":
            branches = [(self.bracket("("), self.bracket("{"))]
            otherwise = None
            while True:
                save = self.i
                self.skip()
                nxt = self.keyword()
                if nxt == "elseif":
                    self.i += 6
                    branches.append((self.bracket("("), self.bracket("{")))
                elif nxt == "else":
                    self.i += 4
                    otherwise = self.bracket("{")
                    break
                else:
                    self.i = save
                    break
            return ("if", branches, otherwise)
        if kw == "foreach":
            m = re.fullmatch(r"\s*\$(\w+)\s+in\s+(.+)", self.bracket("("), re.S | re.I)
            if not m:
                self.error("expected: foreach ($item in <collection>) { ... }")
            return ("foreach", m.group(1).lower(), m.group(2).strip(), self.bracket("{"))
        if kw == "while":
            return ("while", self.bracket("("), self.bracket("{"))
        if kw == "for":
            parts = split_top_level(self.bracket("("), ";")
            if len(parts) != 3:
                self.error("expected: for (init; condition; step) { ... }")
            return ("for", *parts, self.bracket("{"))
        if kw == "function":
            self.skip()
            m = re.match(r"[\w-]+", self.text[self.i:])
            if not m:
                self.error("expected a function name")
            name = m.group(0)
            self.i += len(name)
            self.skip(newlines=False)
            params = self.bracket("(") if self.text[self.i:self.i + 1] == "(" else ""
            body = self.bracket("{")
            pm = re.match(r"\s*param\s*\(", body, re.I)
            if pm:  # function f { param($a, $b) ... }
                end = scan_balanced(body, pm.end() - 1)
                params, body = body[pm.end():end - 1], body[end:]
            return ("function", name.lower(), name, parse_params(params), body)
        if kw in ("return", "break", "continue"):
            return (kw, self.rest_of_statement())
        return ("simple", self.rest_of_statement())


def split_top_level(text, sep):
    parts, start, j = [], 0, 0
    while j < len(text):
        c = text[j]
        if c in CLOSERS:
            j = scan_balanced(text, j)
            continue
        if c in "\"'":
            j = (text.find(c, j + 1) + 1) or len(text)
            continue
        if c == sep:
            parts.append(text[start:j].strip())
            start = j + 1
        j += 1
    parts.append(text[start:].strip())
    return parts


def parse_params(text):
    params = []
    for part in split_top_level(text, ",") if text.strip() else []:
        m = re.fullmatch(r"\$(\w+)\s*(?:=\s*(.+))?", part, re.S)
        if not m:
            raise ShellError(f"Bad parameter '{part}' (expected $name or $name = default)")
        params.append((m.group(1).lower(), m.group(2)))
    return params


class _Op:
    """Infix helper so `a |_LIKE| b` works inside translated expressions."""

    def __init__(self, fn):
        self.fn = fn

    def __ror__(self, left):
        return _Op(lambda right: self.fn(left, right))

    def __or__(self, right):
        return self.fn(right)


def _ps_range(a, b):
    a, b = int(a), int(b)
    return list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))


EXPR_OPS = [  # PowerShell operator -> Python; longer names first so -notlike wins over -not
    (r"-notlike\b", " |_NOTLIKE| "), (r"-like\b", " |_LIKE| "),
    (r"-notmatch\b", " |_NOTMATCH| "), (r"-match\b", " |_MATCH| "),
    (r"-notcontains\b", " |_NOTCONTAINS| "), (r"-contains\b", " |_CONTAINS| "),
    (r"-notin\b", " not in "), (r"-in\b", " in "),
    (r"-eq\b", " == "), (r"-ne\b", " != "), (r"-gt\b", " > "), (r"-ge\b", " >= "),
    (r"-lt\b", " < "), (r"-le\b", " <= "), (r"-and\b", " and "), (r"-or\b", " or "),
    (r"-not\b", " not "), (r"!(?!=)", " not "),
]

EXPR_HELPERS = {
    "_LIKE": _Op(lambda a, b: _like(a, b)), "_NOTLIKE": _Op(lambda a, b: not _like(a, b)),
    "_MATCH": _Op(lambda a, b: re.search(str(b), str(a), re.I) is not None),
    "_NOTMATCH": _Op(lambda a, b: re.search(str(b), str(a), re.I) is None),
    "_CONTAINS": _Op(lambda a, b: b in a), "_NOTCONTAINS": _Op(lambda a, b: b not in a),
    "_range": _ps_range,
}


def translate_expr(code):
    """PowerShell-flavoured expression -> Python source. Strings are kept apart so they are not rewritten."""
    out, j, n = [], 0, len(code)
    while j < n:
        c = code[j]
        if c in "\"'":
            end = code.find(c, j + 1)
            end = n if end < 0 else end
            body = code[j + 1:end]
            out.append(f"_s({body!r})" if c == '"' and "$" in body else repr(body))
            j = end + 1
            continue
        k = j
        while k < n and code[k] not in "\"'":
            k += 1
        seg = re.sub(r"\$(\w+)", lambda m: f"_v.get({m.group(1).lower()!r})", code[j:k])
        seg = re.sub(r"(-?\d+|_v\.get\('\w+'\))\s*\.\.\s*(-?\d+|_v\.get\('\w+'\))", r"_range(\1, \2)", seg)
        for pat, rep in EXPR_OPS:
            seg = re.sub(pat, rep, seg, flags=re.I)
        out.append(seg)
        j = k
    return "".join(out)


# ------------------------------------------------------------------ shell

OPS = {"-eq": "==", "-ne": "!=", "-gt": ">", "-ge": ">=", "-lt": "<", "-le": "<=",
       "-and": " and ", "-or": " or ", "-not": " not "}


class Shell:
    def __init__(self):
        self.vars = {"true": True, "false": False, "null": None,
                     "home": os.path.expanduser("~"), "pid": os.getpid()}
        self.running = True
        self.history = []
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
            "copy-item": self.copy_item, "move-item": self.move_item,
            "rename-item": self.rename_item, "get-history": self.get_history,
            "test-path": self.test_path, "read-host": self.read_host, "start-sleep": self.start_sleep,
            "get-function": self.get_function,
            # Python integration
            "invoke-python": self.invoke_python,
            "convertfrom-csv": self.convertfrom_csv, "convertto-csv": self.convertto_csv,
            "convertfrom-json": self.convertfrom_json, "convertto-json": self.convertto_json,
            "invoke-restmethod": self.invoke_restmethod,
            # Server manager
            "add-server": self.add_server, "remove-server": self.remove_server,
            "get-server": self.get_server, "connect-server": self.connect_server,
            "invoke-server": self.invoke_server, "get-serverstatus": self.get_serverstatus,
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
            "cp": "copy-item", "copy": "copy-item", "cpi": "copy-item",
            "mv": "move-item", "move": "move-item", "mi": "move-item",
            "ren": "rename-item", "rni": "rename-item",
            "history": "get-history", "h": "get-history", "sleep": "start-sleep",
            "py": "invoke-python", "from-csv": "convertfrom-csv", "to-csv": "convertto-csv",
            "from-json": "convertfrom-json", "to-json": "convertto-json",
            "get-json": "invoke-restmethod", "irm": "invoke-restmethod",
            "servers": "get-server", "connect": "connect-server",
            "on-servers": "invoke-server", "server-status": "get-serverstatus",
        }
        self.py_ns = {}  # persists between `py` calls, so `py "a = 5"` then `py "a * 2"` works
        self.functions = {}  # name -> (display name, params, body) from `function` statements
        self.sinks = []  # output collectors; empty means print straight to the console
        self._parsed = {}  # script text -> statements, so loop bodies are parsed once

    # ---- evaluation helpers

    def expand(self, text):
        # $_ is left alone so it can reach script blocks and `py` code
        return self._interpolate(text, keep_underscore=True)

    def value(self, tok):
        kind, text = tok
        if kind == "str":
            return text
        if kind == "dstr":
            return self.expand(text)
        if kind == "block":
            return ("block", text)
        if kind == "paren":
            return self.evaluate(text)
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

    def eval_expr(self, code):
        """Evaluate an expression: PowerShell operators and $variables, otherwise plain Python."""
        ns = dict(EXPR_HELPERS, _v=self.vars, _s=self._interpolate)
        code = self._run_command_groups(code, ns)
        src = translate_expr(code)
        _auto_import(src, ns, "eval")
        try:
            return eval(src, ns)
        except SyntaxError:
            raise ShellError(f"Invalid expression: {code}")

    def _run_command_groups(self, code, ns):
        """In `$n * (fact ($n - 1))`, run the bracketed command and use its result as a value."""
        out, j, n = [], 0, len(code)
        while j < n:
            c = code[j]
            if c in "\"'":
                end = code.find(c, j + 1)
                end = n if end < 0 else end + 1
                out.append(code[j:end])
                j = end
                continue
            before = "".join(out).rstrip()[-1:]
            if c == "(" and not (before and (before.isalnum() or before in "_.)]")):  # a group, not f(...)
                end = scan_balanced(code, j)
                inner = code[j + 1:end - 1]
                if self.is_command(tokenize(inner)):
                    name = f"__group{len(ns)}"
                    ns[name] = self.evaluate(inner)
                    out.append(name)
                    j = end
                    continue
            out.append(c)
            j += 1
        return "".join(out)

    def _interpolate(self, text, keep_underscore=False):
        """Expand $name and $(expression) inside a double-quoted string."""
        out, j = [], 0
        while j < len(text):
            if text.startswith("$(", j):
                end = scan_balanced(text, j + 1)
                items = _iterate(self.evaluate(text[j + 2:end - 1]))
                out.append(" ".join(str(x) for x in items))
                j = end
                continue
            m = re.match(r"\$(\w+)", text[j:])
            if m and not (keep_underscore and m.group(1) == "_"):
                out.append(str(_none_to_empty(self.vars.get(m.group(1).lower()))))
                j += m.end()
                continue
            out.append(text[j])
            j += 1
        return "".join(out)

    def is_command(self, tokens):
        """Does this statement start with a command (vs. a value like 5, "text", $x or (expr))?"""
        if not tokens or tokens[0][0] != "word":
            return False
        word = tokens[0][1]
        # a call like math.sqrt(...) or len(...) is a Python expression, not a command
        return not re.match(r"[$\d\[@!]|-\w|-?\.?\d", word) and "(" not in word

    def evaluate(self, text):
        """Value of a condition / right-hand side: run it if it is a command, else evaluate it."""
        tokens = tokenize(text)
        if self.is_command(tokens) or (tokens and ("op", "|") in tokens):
            items = self.run_pipeline(tokens, capture=True)
            return items[0] if len(items) == 1 else PSList(items)
        return self.eval_expr(text)

    def run_block(self, code, item=None):
        """Run a { script block } with $_ set; returns what it outputs (used by where / foreach)."""
        saved = self.vars.get("_")
        self.vars["_"] = item
        try:
            return self.capture(lambda: self.run_script(code))
        finally:
            self.vars["_"] = saved

    def capture(self, fn):
        self.sinks.append([])
        try:
            fn()
        finally:
            items = self.sinks.pop()
        return items

    def emit(self, items):
        if self.sinks:
            self.sinks[-1].extend(items)
        else:
            output(items)

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
        try:
            self.run_script(line.lstrip(chr(0xFEFF)))  # BOM from piped input / Notepad files
        except ReturnSignal as r:  # `return` in a script file just ends it
            self.emit(r.items)
        except (BreakSignal, ContinueSignal):
            raise ShellError("break/continue can only be used inside a loop")

    def run_script(self, text):
        stmts = self._parsed.get(text)
        if stmts is None:
            stmts = self._parsed[text] = ScriptParser(text).statements()
        for stmt in stmts:
            if not self.running:
                return
            self.exec_stmt(stmt)

    def exec_stmt(self, stmt):
        kind = stmt[0]
        if kind == "simple":
            self.run_simple(stmt[1])
        elif kind == "if":
            for cond, body in stmt[1]:
                if _truthy(self.evaluate(cond)):
                    return self.run_script(body)
            if stmt[2] is not None:
                self.run_script(stmt[2])
        elif kind == "foreach":
            _, var, source, body = stmt
            for item in _iterate(self.evaluate(source)):
                self.vars[var] = item
                if self._loop_body(body):
                    break
        elif kind == "while":
            while _truthy(self.evaluate(stmt[1])):
                if self._loop_body(stmt[2]):
                    break
        elif kind == "for":
            _, init, cond, step, body = stmt
            if init:
                self.run_simple(init)
            while not cond or _truthy(self.evaluate(cond)):
                if self._loop_body(body):
                    break
                if step:
                    self.run_simple(step)
        elif kind == "function":
            _, key, name, params, body = stmt
            self.functions[key] = (name, params, body)
        elif kind == "return":
            items = []
            if stmt[1]:
                items = _iterate(self.evaluate(stmt[1]))
            raise ReturnSignal(items)
        elif kind == "break":
            raise BreakSignal()
        elif kind == "continue":
            raise ContinueSignal()

    def _loop_body(self, body):
        """Run one loop iteration; True means `break`."""
        if not self.running:  # `exit` inside a loop
            return True
        try:
            self.run_script(body)
        except BreakSignal:
            return True
        except ContinueSignal:
            pass
        return False

    def run_simple(self, text):
        m = re.fullmatch(r"\$(\w+)\s*(\+\+|--)", text)
        if m:
            name = m.group(1).lower()
            self.vars[name] = (self.vars.get(name) or 0) + (1 if m.group(2) == "++" else -1)
            return
        m = re.fullmatch(r"\$(\w+)\s*([-+*/]?)=(?!=)\s*(.+)", text, re.S)
        if m:
            name, op, rhs = m.group(1).lower(), m.group(2), m.group(3)
            value = self.evaluate(rhs)
            if op:
                current = self.vars.get(name)
                value = {"+": lambda a, b: a + b, "-": lambda a, b: a - b,
                         "*": lambda a, b: a * b, "/": lambda a, b: a / b}[op](current, value)
            self.vars[name] = value
            return
        self.emit(self.run_pipeline(tokenize(text), capture=bool(self.sinks)))

    def run_pipeline(self, tokens, capture=False):
        stages = split_on(tokens, "|")
        data = None
        for i, stage in enumerate(stages):
            if not stage:
                raise ShellError("empty pipeline element")
            if i == 0 and not self.is_command(stage):
                data = to_items(self.eval_expr(tokens_to_text(stage)))
                continue
            name = stage[0][1]
            args = [self.value(t) for t in stage[1:]]
            live = not capture and i == len(stages) - 1
            data = self.invoke(name, args, data, live)
        return data or []

    def call_function(self, key, args, pipe):
        name, params, body = self.functions[key]
        pos, named = self.parse_args(args)
        outer = self.vars
        self.vars = dict(outer)  # function scope: sees the caller's variables, its own changes stay local
        try:
            for pname, default in params:
                if pname in named:
                    self.vars[pname] = named.pop(pname)
                elif pos:
                    self.vars[pname] = pos.pop(0)
                else:
                    self.vars[pname] = self.evaluate(default) if default else None
            self.vars["args"] = pos
            self.vars["input"] = list(pipe or [])
            self.sinks.append([])  # everything the body outputs is the function's result
            returned = []
            try:
                self.run_script(body)
            except ReturnSignal as r:
                returned = r.items
            finally:
                items = self.sinks.pop()
            return items + list(returned)
        finally:
            self.vars = outer

    def invoke(self, name, args, pipe, live=False):
        if name.lower() in self.functions:
            return self.call_function(name.lower(), args, pipe)
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
            def keep(x):
                try:
                    return _truthy(self.run_block(pos[0][1], x))
                except TypeError:  # e.g. folder Length (None) -gt 100: treat as no match, like PowerShell
                    return False
            return [x for x in pipe if keep(x)]
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
            yield from self.run_block(pos[0][1], x)

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

    def _sources_and_dest(self, pos, named, pipe):
        dest = named.get("destination")
        paths = [named["path"]] if "path" in named else list(pos)
        if dest is None:
            if len(paths) < 2:
                raise ShellError("usage: <command> <source> <destination>")
            dest = paths.pop()
        if not paths:
            paths = [get_prop(x, "Name") for x in pipe]
        sources = []
        for p in paths:
            matches = glob.glob(str(p))
            if not matches:
                raise ShellError(f"Cannot find path '{p}' because it does not exist.")
            sources.extend(matches)
        dest = str(dest)
        if len(sources) > 1 and not os.path.isdir(dest):
            raise ShellError(f"Destination '{dest}' must be an existing folder for multiple items.")
        return sources, dest

    def copy_item(self, pos, named, pipe):
        sources, dest = self._sources_and_dest(pos, named, pipe)
        for src in sources:
            target = os.path.join(dest, os.path.basename(src)) if os.path.isdir(dest) else dest
            if os.path.isdir(src):
                if not named.get("recurse"):
                    raise ShellError(f"'{src}' is a folder; use -Recurse to copy it.")
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.copy2(src, target)
        return []

    def move_item(self, pos, named, pipe):
        sources, dest = self._sources_and_dest(pos, named, pipe)
        for src in sources:
            shutil.move(src, dest)
        return []

    def rename_item(self, pos, named, pipe):
        path = named.get("path", pos[0] if pos else None)
        new = named.get("newname", pos[1] if len(pos) > 1 else None)
        if path is None or new is None:
            raise ShellError("usage: Rename-Item <path> <new name>")
        if not os.path.exists(path):
            raise ShellError(f"Cannot find path '{path}' because it does not exist.")
        os.rename(path, os.path.join(os.path.dirname(str(path)), str(new)))
        return []

    def get_history(self, pos, named, pipe):
        for i, line in enumerate(self.history, 1):
            yield Obj(Id=i, CommandLine=line)

    def test_path(self, pos, named, pipe):
        return [all(os.path.exists(str(p)) for p in pos)] if pos else [False]

    def read_host(self, pos, named, pipe):
        try:
            return [input(f"{' '.join(str(p) for p in pos)}: " if pos else "")]
        except EOFError:
            return [""]

    def start_sleep(self, pos, named, pipe):
        seconds = float(named.get("seconds", pos[0] if pos else 1))
        time.sleep(float(named["milliseconds"]) / 1000 if "milliseconds" in named else seconds)
        return []

    def get_function(self, pos, named, pipe):
        for name, params, body in self.functions.values():
            yield Obj(Name=name, Parameters=", ".join("$" + p for p, _ in params))

    # ---- Python integration

    def invoke_python(self, pos, named, pipe):
        """py "expr" - `_` is the list of pipeline items; with -Each, `_` is one item at a time."""
        each = named.get("each")
        code = each if isinstance(each, str) else " ".join(str(p) for p in pos)
        if not code.strip():
            raise ShellError('usage: py "<python expression>"   (pipeline input is `_`)')
        code = re.sub(r"\$_\b", "_", code)
        ns = self.py_ns
        ns.update({k: v for k, v in self.vars.items() if k.isidentifier()})
        try:
            compiled = compile(code, "<py>", "eval")
        except SyntaxError:
            _auto_import(code, ns, "exec")
            exec(compile(code, "<py>", "exec"), ns)
            return []
        _auto_import(code, ns, "eval")
        if each:
            out = []
            for item in pipe:
                ns["_"] = item
                out.extend(to_items(eval(compiled, ns)))
            return out
        ns["_"] = list(pipe)
        return to_items(eval(compiled, ns))

    def _read_text(self, pos, pipe):
        if pos:
            with open(pos[0], encoding="utf-8-sig", errors="replace") as f:
                return f.read()
        return "\n".join(str(x) for x in pipe)

    def _write_or_return(self, pos, lines):
        if pos:
            with open(pos[0], "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(lines) + "\n")
            return []
        return lines

    def convertfrom_csv(self, pos, named, pipe):
        for row in csv.DictReader(io.StringIO(self._read_text(pos, pipe))):
            yield Obj(**{k: _auto_type(v) for k, v in row.items() if k is not None})

    def convertto_csv(self, pos, named, pipe):
        rows = [to_plain(x) for x in pipe]
        cols = []
        for r in rows:
            for k in (r if isinstance(r, dict) else {"Value": r}):
                if k not in cols:
                    cols.append(k)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(cols)
        for r in rows:
            r = r if isinstance(r, dict) else {"Value": r}
            writer.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
        return self._write_or_return(pos, buf.getvalue().splitlines())

    def convertfrom_json(self, pos, named, pipe):
        return to_items(json.loads(self._read_text(pos, pipe)))

    def convertto_json(self, pos, named, pipe):
        items = [to_plain(x) for x in pipe]
        data = items[0] if len(items) == 1 else items
        return self._write_or_return(pos, json.dumps(data, indent=2, default=str).splitlines())

    def invoke_restmethod(self, pos, named, pipe):
        url = named.get("uri", pos[0] if pos else None)
        if not url:
            raise ShellError("usage: get-json <url> [-Method POST] [-Body '{...}']")
        body = named.get("body")
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(to_plain(body))
        req = urllib.request.Request(
            str(url), method=str(named.get("method", "POST" if body else "GET")).upper(),
            data=body.encode() if isinstance(body, str) else body,
            headers={"User-Agent": f"CherryShell/{__version__}", "Accept": "application/json",
                     **({"Content-Type": "application/json"} if body else {})})
        try:
            with urllib.request.urlopen(req, timeout=float(named.get("timeout", 30))) as resp:
                text = resp.read().decode(resp.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ShellError(f"HTTP {e.code} {e.reason} for {url}")
        except urllib.error.URLError as e:
            raise ShellError(f"Could not reach {url}: {e.reason}")
        try:
            return to_items(json.loads(text))
        except ValueError:
            return text.splitlines()

    # ---- server manager

    def _pick_servers(self, pos, named, pipe):
        servers = load_servers()
        names = [get_prop(x, "Name") for x in pipe if get_prop(x, "Name")]
        for p in pos:
            names.extend(n for n in str(p).split(",") if n)
        if "name" in named:
            names.extend(n for n in str(named["name"]).split(",") if n)
        picked = list(servers) if not names else []
        for n in names:
            matches = [s for s in servers if _like(s, n)]
            if not matches:
                raise ShellError(f"No server named '{n}'. See: Get-Server")
            picked.extend(m for m in matches if m not in picked)
        if "tag" in named:
            picked = [s for s in picked if str(named["tag"]).lower() in servers[s]["tags"]]
        return [(s, servers[s]) for s in picked]

    def add_server(self, pos, named, pipe):
        if len(pos) < 2:
            raise ShellError("usage: Add-Server <name> <user@host> [-Port 22] [-Key path] [-Tags web,db]")
        name, target = str(pos[0]), str(pos[1])
        user, _, host = target.rpartition("@")
        user = named.get("user", user) or os.environ.get("USERNAME") or os.environ.get("USER", "")
        servers = load_servers()
        servers[name] = {
            "host": host, "user": str(user), "port": int(named.get("port", 22)),
            "key": os.path.abspath(os.path.expanduser(str(named["key"]))) if "key" in named else "",
            "tags": [t.strip().lower() for t in str(named.get("tags", "")).split(",") if t.strip()],
        }
        save_servers(servers)
        return [server_obj(name, servers[name])]

    def remove_server(self, pos, named, pipe):
        servers = load_servers()
        for name, _ in self._pick_servers(pos, named, pipe) if (pos or pipe or named) else []:
            del servers[name]
        save_servers(servers)
        return []

    def get_server(self, pos, named, pipe):
        servers = load_servers()
        if pos or named:
            return [server_obj(n, s) for n, s in self._pick_servers(pos, named, pipe)]
        return [server_obj(n, s) for n, s in servers.items()]

    def connect_server(self, pos, named, pipe):
        picked = self._pick_servers(pos[:1], {}, [])
        if not pos or len(picked) != 1:
            raise ShellError("usage: Connect-Server <name>")
        _, srv = picked[0]
        try:
            subprocess.run(ssh_argv(srv) + [str(p) for p in pos[1:]])
        except FileNotFoundError:
            raise ShellError("ssh was not found. Install the Windows OpenSSH Client.")
        except KeyboardInterrupt:
            print()
        return []

    def invoke_server(self, pos, named, pipe):
        """Run one command on many servers in parallel: Invoke-Server "uptime" [-Name a,b] [-Tag web]"""
        if not pos:
            raise ShellError('usage: Invoke-Server "<command>" [-Name web1,web2] [-Tag web]')
        command = str(pos[0])
        picked = self._pick_servers(pos[1:], named, pipe)
        if not picked:
            raise ShellError("No servers saved yet. Add one with: Add-Server <name> <user@host>")
        timeout = float(named.get("timeout", 60))

        def run(srv):
            try:
                p = subprocess.run(ssh_argv(srv, batch=True) + [command], capture_output=True, text=True,
                                   errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
            except subprocess.TimeoutExpired:
                return [], [f"timed out after {timeout:g}s"], None
            except FileNotFoundError:
                return [], ["ssh was not found. Install the Windows OpenSSH Client."], None
            return p.stdout.splitlines(), p.stderr.splitlines(), p.returncode

        with ThreadPoolExecutor(max_workers=min(16, len(picked))) as pool:
            results = list(pool.map(lambda ns: run(ns[1]), picked))
        for (name, _), (out, err, code) in zip(picked, results):
            for line in out:
                yield Obj(Server=name, Output=line)
            for line in err:
                yield Obj(Server=name, Output="! " + line)
            if code:
                yield Obj(Server=name, Output=f"! exit code {code}")

    def get_serverstatus(self, pos, named, pipe):
        """Reachability and latency for each server; -Detailed also logs in for load, memory and disk."""
        picked = self._pick_servers(pos, named, pipe)
        detailed = bool(named.get("detailed"))

        def check(srv):
            row = {"Status": "Offline", "PingMs": None}
            start = time.perf_counter()
            try:
                socket.create_connection((srv["host"], srv["port"]), timeout=3).close()
                row.update(Status="Online", PingMs=round((time.perf_counter() - start) * 1000))
            except OSError:
                return row
            if detailed:
                row.update(Load=None, MemUsed=None, DiskUsed=None)
                try:
                    p = subprocess.run(ssh_argv(srv, batch=True) + [STATUS_COMMAND], capture_output=True,
                                       text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=20)
                    vals = p.stdout.split()
                    if p.returncode == 0 and len(vals) >= 3:
                        row.update(Load=_auto_type(vals[0]), MemUsed=vals[1] + "%", DiskUsed=vals[2])
                    else:
                        row["Status"] = "Online (login failed)"
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    row["Status"] = "Online (login failed)"
            return row

        if not picked:
            return []
        with ThreadPoolExecutor(max_workers=min(16, len(picked))) as pool:
            rows = list(pool.map(lambda ns: check(ns[1]), picked))
        return [Obj(Name=name, Host=srv["host"], **row) for (name, srv), row in zip(picked, rows)]


class ShellError(Exception):
    pass


# --------------------------------------------------------------- helpers

_WORDS = {"childitem": "ChildItem", "foreach": "ForEach", "convertfrom": "ConvertFrom",
          "convertto": "ConvertTo", "restmethod": "RestMethod", "serverstatus": "ServerStatus"}


def _title(name):
    return "-".join(_WORDS.get(p, p.capitalize()) for p in name.split("-"))


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


class PSList(list):
    """Command results: a normal Python list that also has PowerShell's .Count / .Length."""

    @property
    def Count(self):
        return len(self)

    Length = Count


def _truthy(value):
    if isinstance(value, list):
        return bool(value[0]) if len(value) == 1 else bool(value)
    return bool(value)


def _iterate(value):
    if value is None:
        return []
    if isinstance(value, (str, bytes, dict, Obj)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _none_to_empty(value):
    return "" if value is None else value


def _auto_type(text):
    """CSV/ssh values arrive as text; turn numbers into numbers so -gt and sum() work."""
    if not isinstance(text, str):
        return text
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def to_obj(value):
    return Obj(**{str(k): v for k, v in value.items()}) if isinstance(value, dict) else value


def to_items(result):
    """Turn a Python value into pipeline items: lists/iterables spread out, dicts become objects."""
    if result is None:
        return []
    if isinstance(result, (str, bytes, int, float, bool, dict, Obj)):
        return [to_obj(result)]
    try:
        return [to_obj(x) for x in result]
    except TypeError:
        return [result]


def to_plain(value):
    if isinstance(value, Obj):
        return {k: to_plain(v) for k, v in value._props.items()}
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def _auto_import(code, ns, mode):
    """Import modules that `py` code mentions (math, json, urllib.request, ...) without an import line."""
    try:
        tree = ast.parse(code, mode=mode)
    except SyntaxError:
        return
    # names the code binds itself (loop/comprehension variables, lambda args) are not modules
    bound = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)}
    bound |= {a.arg for a in ast.walk(tree) if isinstance(a, ast.arg)}
    for node in ast.walk(tree):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.insert(0, node.attr)
            node = node.value
        if not isinstance(node, ast.Name) or node.id in ns or node.id in bound \
                or hasattr(__builtins__, node.id) \
                or (isinstance(__builtins__, dict) and node.id in __builtins__):
            continue
        parts.insert(0, node.id)
        for i in range(len(parts), 0, -1):
            try:
                importlib.import_module(".".join(parts[:i]))
            except ImportError:
                continue
            ns[node.id] = importlib.import_module(node.id)
            break


# ---- server manager storage

STATUS_COMMAND = ("cut -d' ' -f1 /proc/loadavg; "
                  "free | awk '/Mem:/ {printf \"%.0f\\n\", $3/$2*100}'; "
                  "df -P / | awk 'NR==2 {print $5}'")


def servers_file():
    home = os.environ.get("CHERRYSHELL_HOME") or os.path.join(os.path.expanduser("~"), ".cherryshell")
    return os.path.join(home, "servers.json")


def load_servers():
    try:
        with open(servers_file(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_servers(servers):
    path = servers_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2)


def server_obj(name, srv):
    return Obj(Name=name, User=srv["user"], Host=srv["host"], Port=srv["port"],
               Tags=", ".join(srv["tags"]), Key=srv["key"])


def ssh_argv(srv, batch=False):
    argv = ["ssh", "-p", str(srv["port"])]
    if srv["key"]:
        argv += ["-i", srv["key"]]
    if batch:  # never stop to ask for a password or host-key confirmation when running on many servers
        argv += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
    return argv + [f"{srv['user']}@{srv['host']}" if srv["user"] else srv["host"]]


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
HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".cherryshell_history")


def make_reader(shell):
    """Line editor with persistent history and Tab completion; plain input() as fallback."""
    if not sys.stdin.isatty():
        return input
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.history import FileHistory
        session = PromptSession(history=FileHistory(HISTORY_FILE), completer=make_completer(shell),
                                complete_while_typing=False)
    except Exception:  # not installed, or not a real console (e.g. Git Bash/mintty)
        return input
    return lambda prompt: session.prompt(ANSI(prompt))


def make_completer(shell):
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document

    class ShellCompleter(Completer):
        paths = PathCompleter(expanduser=True)

        def get_completions(self, document, event):
            segment = re.split(r"[|;]", document.text_before_cursor)[-1].lstrip()
            if " " not in segment:
                names = [_title(c) for c in shell.commands] + list(shell.aliases)
                for name in sorted(names, key=str.lower):
                    if name.lower().startswith(segment.lower()):
                        yield Completion(name, start_position=-len(segment))
                return
            word = segment.rsplit(" ", 1)[-1].strip("\"'")
            yield from self.paths.get_completions(Document(word, len(word)), event)

    return ShellCompleter()


def main():
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(errors="replace")
    shell = Shell()
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8-sig") as f:
            script = f.read()
        shell.vars["args"] = sys.argv[2:]
        try:
            shell.run_line(script)
        except ShellError as e:
            print(f"{RED}{e}{RESET}")
            sys.exit(1)
        except Exception as e:
            print(f"{RED}{type(e).__name__}: {e}{RESET}")
            sys.exit(1)
        return
    print(f"{CHERRY}CherryShell {__version__}{RESET} - type Get-Command for commands, exit to quit.\n")
    read_line = make_reader(shell)
    while shell.running:
        try:
            line = read_line(f"{CHERRY}CS{RESET} {os.getcwd()}> ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            print()
            break
        try:
            while is_incomplete(line):  # an open { or ( : keep reading the block
                line += "\n" + read_line(">> ")
        except KeyboardInterrupt:
            print()
            continue
        except EOFError:
            pass
        if line.strip():
            shell.history.append(line.strip())
        try:
            shell.run_line(line)
        except ShellError as e:
            print(f"{RED}{e}{RESET}")
        except KeyboardInterrupt:
            print(f"{RED}Stopped.{RESET}")
        except Exception as e:
            print(f"{RED}{type(e).__name__}: {e}{RESET}")


if __name__ == "__main__":
    main()
