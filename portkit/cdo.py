"""Constructor drafts from the running game's default objects.

The hook loader's dump mode (windows/recomp-hooks, RecompDump.txt next to the hook DLL) writes RecompCDO.tsv:
for each FactoryGame class, each property of its default object as reflection exports it (class, property,
owner class, C++ type, offset, value). A constructor sets what differs from the zeroed object and from the
parent's constructor, so `draft` turns each row into a constructor-body line: the class's own properties
unless zero, and the inherited ones its default object changes (`PrimaryActorTick.bCanEverTick = true;`).
What reflection does not see (members that are not UPROPERTYs, subobjects being created) still needs the
disassembly. The dump is game-derived: it stays on the owner's disks.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .util import Fail

NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
ZERO = {"", "0", "False", "None", "()", "\"\"", "NSLOCTEXT(\"\", \"\", \"\")"}


@dataclass
class Row:
    cls: str
    prop: str
    owner: str
    type: str
    offset: int
    value: str

    @property
    def declared(self):
        return self.owner == self.cls


def _unescape(text):
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r"}.get(m.group(1), m.group(1)), text)


def read(tsv):
    rows = []
    for line in Path(tsv).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        cls, prop, owner, type_, offset, value = parts[:6]
        rows.append(Row(cls, prop, owner, _unescape(type_), int(offset), _unescape(value)))
    return rows


def split_fields(text):
    """'(A=1,B=(C=2,D="x,y"))' -> [('A', '1'), ('B', '(C=2,D="x,y")')]; None if it is not a struct's text."""
    if not (text.startswith("(") and text.endswith(")")):
        return None
    body, fields, depth, quoted, start = text[1:-1], [], 0, False, 0
    for i, c in enumerate(body + ","):
        if c == '"' and (i == 0 or body[i - 1] != "\\"):
            quoted = not quoted
        elif not quoted and c in "([":
            depth += 1
        elif not quoted and c in ")]":
            depth -= 1
        elif not quoted and depth == 0 and c == ",":
            part = body[start:i]
            start = i + 1
            if not part:
                continue
            name, sep, value = part.partition("=")
            if not sep or not re.fullmatch(r"[A-Za-z_]\w*(\[\d+\])?", name):
                return None
            fields.append((name, value))
    return fields


def _float(text, suffix):
    v = float(text)
    s = repr(v)
    if "e" not in s and "." not in s:
        s += ".0"
    return s + suffix


def literal(value, type_, classes=()):
    """A C++ expression for one exported value, or None when it needs a human (an object, an array...)."""
    t = type_.strip()
    if value in ("True", "False"):
        return value.lower()
    enum = re.fullmatch(r"TEnumAsByte<(\w+)::Type>", t)
    if enum or re.fullmatch(r"E[A-Z]\w*", t):
        name = enum.group(1) if enum else t
        short = value.split("::")[-1]
        return f"{name}::{short}" if re.fullmatch(r"\w+", short) else None
    if t in ("float",) and NUMBER.match(value):
        return _float(value, "f")
    if t == "double" and NUMBER.match(value):
        return _float(value, "")
    if re.fullmatch(r"u?int(8|16|32|64)|int|uint", t) and re.fullmatch(r"-?\d+", value):
        return value
    if t == "FString":
        return 'TEXT( "' + value.replace("\\", "\\\\").replace('"', '\\"') + '" )'
    if t == "FName":
        return 'FName( TEXT( "' + value + '" ) )'
    if t == "FText" and re.match(r"^(NSLOCTEXT|INVTEXT|LOCTEXT|LOCTABLE)\(", value):
        return value
    if re.match(r"^(TSubclassOf|UClass|TObjectPtr<UClass>)", t):
        hit = re.fullmatch(r"(?:Class')?/Script/\w+\.(\w+)'?", value)
        if hit:
            for prefix in ("U", "A", ""):
                if prefix + hit.group(1) in classes:
                    return f"{prefix + hit.group(1)}::StaticClass()"
    if NUMBER.match(value) and "." in value:
        return _float(value, "f")      # a struct member: its type is not in the dump
    if re.fullmatch(r"-?\d+", value):
        return value
    return None


def lines_for(row, classes=()):
    """The constructor-body lines for one row, or a comment saying what to do by hand."""
    if row.declared and (row.value in ZERO or (NUMBER.match(row.value) and float(row.value) == 0)):
        return []
    return _assign(row.prop, row.value, row.type, classes)


def _assign(target, value, type_, classes):
    fields = split_fields(value)
    if fields is not None and not re.match(r"^(TArray|TMap|TSet)", type_):
        out = []
        for name, v in fields:
            out += _assign(f"{target}.{name}", v, "", classes)
        return out
    expr = literal(value, type_, classes)
    if expr is None:
        return [f"// {target} = {value}" + (f"   ({type_})" if type_ else "")]
    return [f"{target} = {expr};"]


def draft(tsv, cls):
    """The constructor-body draft of one class (UFGFoo), with its rows."""
    rows = read(tsv)
    classes = {r.cls for r in rows}
    mine = [r for r in rows if r.cls == cls]
    if not mine:
        close = sorted(c for c in classes if cls.lower().lstrip("ua") in c.lower())[:5]
        raise Fail(f"no class {cls} in {tsv}" + (f" (did you mean {', '.join(close)}?)" if close else ""))
    mine.sort(key=lambda r: (not r.declared, r.offset))
    out = []
    for r in mine:
        out += lines_for(r, classes)
    return mine, out
