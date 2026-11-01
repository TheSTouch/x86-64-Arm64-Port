"""Valve's text KeyValues format (appmanifest_*.acf, libraryfolders.vdf)."""

import re

_TOKEN = re.compile(r'\s*(?://[^\n]*\n|"((?:[^"\\]|\\.)*)"|([{}]))', re.S)
_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\n": "\n", "\\t": "\t"}


def _unescape(s):
    return re.sub(r"\\.", lambda m: _UNESCAPE.get(m.group(0), m.group(0)), s)


def _tokens(text):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip():
                raise ValueError(f"bad VDF near: {text[pos:pos + 40]!r}")
            return
        pos = m.end()
        if m.group(1) is not None:
            yield ("str", _unescape(m.group(1)))
        elif m.group(2):
            yield (m.group(2), None)


def loads(text):
    stack = [{}]
    key = None
    for kind, value in _tokens(text):
        if kind == "str":
            if key is None:
                key = value
            else:
                stack[-1][key] = value
                key = None
        elif kind == "{":
            if key is None:
                raise ValueError("VDF block without a key")
            child = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        else:
            if len(stack) == 1:
                raise ValueError("unbalanced } in VDF")
            stack.pop()
    if len(stack) != 1:
        raise ValueError("unclosed { in VDF")
    return stack[0]


def _escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def dumps(obj, indent=0):
    out = []
    tabs = "\t" * indent
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append(f'{tabs}"{_escape(k)}"\n{tabs}{{\n{dumps(v, indent + 1)}{tabs}}}\n')
        else:
            out.append(f'{tabs}"{_escape(k)}"\t\t"{_escape(v)}"\n')
    return "".join(out)


def load(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return loads(f.read())
