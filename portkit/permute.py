"""A permuter for the last differences of a recovered function, as decomp-permuter does for the N64
decompilations: small rewrites of the function's C++ text, each compiled on the Mac (`msvc`, one unity blob)
and scored against the shipped function (`decomp.match`'s score: instructions not the same once aligned,
0 when matched). Improvements are kept and rewritten further; it stops at 0 or at a limit.

It works on a staged copy of the project, never the recovered-code repository, and leaves the staged file
as it is: `jobs` candidates compile at once, each from a worker folder of its own searched before the
project's include folders, so the blob includes the worker's copy of the file. The best candidates are
written beside the staged file, for a person (or a session) to carry over by hand. The rewrites are textual, no C++ parser: a rewrite that does not compile
simply scores nothing.
"""

import concurrent.futures
import difflib
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, decomp, msvc
from .util import Fail, step

IDENT = r"[A-Za-z_]\w*"


# ---------------------------------------------------------------------------------------------- the text

def _match_close(text, i, open_="(", close=")"):
    """The index of the bracket closing the one at text[i]; strings, character literals and comments skipped."""
    depth, j, quote = 0, i, None
    while j < len(text):
        c = text[j]
        if not quote and text.startswith("//", j):
            j = text.find("\n", j)
            if j < 0:
                break
            continue
        if not quote and text.startswith("/*", j):
            j = text.find("*/", j)
            if j < 0:
                break
            j += 2
            continue
        if quote:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise Fail(f"unbalanced {open_}{close}")


def _body_brace(text, close):
    """The index of the body's opening brace after a parameter list closing at text[close], past the
    qualifiers and a constructor's init list (whose entries may be braced themselves); None for a
    declaration or a call."""
    i = close + 1
    m = re.match(r"(?:\s|const\b|override\b|final\b|noexcept\b|volatile\b|&)*", text[i:])
    i += m.end()
    if text.startswith(":", i) and not text.startswith("::", i):
        i += 1
        while True:
            m = re.match(rf"\s*(?:{IDENT}(?:::{IDENT})*(?:\s*<[^(){{}};]*>)?)\s*", text[i:])
            if not m or i + m.end() >= len(text) or text[i + m.end()] not in "({":
                return None
            j = i + m.end()
            j = _match_close(text, j, text[j], ")" if text[j] == "(" else "}") + 1
            m = re.match(r"\s*,", text[j:])
            if not m:
                i = j
                break
            i = j + m.end()
    brace = text.find("{", i)
    if brace < 0 or ";" in text[i:brace] or "(" in text[i:brace]:
        return None
    return brace


def find_definition(text, name):
    """(start, end) of the definition of `name` (Scope::Function) in text: from the start of its signature's
    line to just past its closing brace."""
    for m in re.finditer(rf"^[^\n;{{}}#]*?\b{re.escape(name)}\s*\(", text, re.M):
        close = _match_close(text, m.end() - 1)
        brace = _body_brace(text, close)
        if brace is None:
            continue   # a declaration or a call, not the definition
        return m.start(), _match_close(text, brace, "{", "}") + 1
    raise Fail(f"no definition of {name} found")


def find_function(text, name):
    """(start, end) of the body of `name` (Scope::Function) in text: from its opening brace to just past its
    closing one."""
    start, end = find_definition(text, name)
    return start + Definition(text[start:end]).body_start, end


def source_function(name):
    """The function whose text holds `name`'s code: a lambda or local class's enclosing function
    (`Scope::F'::`2'::<lambda_1>::operator() -> Scope::F), else the name itself."""
    m = re.match(r"`([^']+)'", name)
    return m.group(1) if m else name


def statements(body):
    """The top-level statements of a body ('{ ... }' included): (start, end) spans in body, end past the ';'
    or the closing brace of a compound statement."""
    out, i, depth, start = [], 1, 0, None
    while i < len(body) - 1:
        c = body[i]
        if body.startswith("//", i) or body.startswith("/*", i):   # a comment belongs to no statement
            i = body.find("\n" if body[i + 1] == "/" else "*/", i)
            i = len(body) if i < 0 else i + (1 if body[i] == "\n" else 2)
            continue
        if start is None and not c.isspace():
            start = i
        if c in "\"'":
            q, i = c, i + 1
            while i < len(body) and body[i] != q:
                i += 2 if body[i] == "\\" else 1
        elif c == "(":
            i = _match_close(body, i)
        elif c == "{":
            i = _match_close(body, i, "{", "}")
            rest = body[i + 1:].lstrip()
            if depth == 0 and not rest.startswith("else") and start is not None:
                out.append((start, i + 1))
                start = None
        elif c == ";" and start is not None:
            out.append((start, i + 1))
            start = None
        i += 1
    return out


# ---------------------------------------------------------------------------------------------- the rewrites

def _calls(text):
    """(start, end) of call expressions in text: a name chain (a, a->b, a.b, A::b, templates aside) and its
    balanced argument list."""
    out = []
    for m in re.finditer(rf"(?<![\w>.:])(?:{IDENT}(?:->|\.|::))*{IDENT}\s*\(", text):
        if m.group(0).split("(")[0].strip() in ("if", "for", "while", "switch", "return", "sizeof"):
            continue
        try:
            out.append((m.start(), _match_close(text, m.end() - 1) + 1))
        except Fail:
            pass
    return out


def _declared(statement):
    """The name a declaration statement introduces (const int32 n = ...; FFoo foo( ... );), or None."""
    m = re.match(rf"(?:const\s+)?(?:auto&*|{IDENT}(?:::{IDENT})*(?:<[^;=]*>)?\s*[*&]?)\s+({IDENT})\s*(?:=|\(|\{{|;)", statement)
    if not m or re.match(r"(?:return|delete|throw|goto|else|case|new|co_return|co_yield|using|typedef)\b", statement):
        return None
    return m.group(1)


def r_swap_statements(body, rng):
    """Two neighbouring simple statements swapped when neither uses a name the other declares."""
    sts = [(a, b) for a, b in statements(body) if not re.match(r"(return|if|for|while|switch|do)\b", body[a:b])
           and "{" not in body[a:b]]
    pairs = [(x, y) for x, y in zip(sts, sts[1:]) if not body[x[1]:y[0]].strip()]
    rng.shuffle(pairs)
    for (a1, b1), (a2, b2) in pairs:
        s1, s2 = body[a1:b1], body[a2:b2]
        if any(n and re.search(rf"\b{n}\b", other) for n, other in ((_declared(s1), s2), (_declared(s2), s1))):
            continue
        return body[:a1] + s2 + body[b1:a2] + s1 + body[b2:]
    return None


def r_introduce_temp(body, rng):
    """A call taken out of its statement into a temporary just before it."""
    sts = statements(body)
    options = []
    for a, b in sts:
        st = body[a:b]
        if re.match(r"(for|while|do|switch)\b", st):
            continue
        head = st if not st.startswith("if") else st[:st.find("{")] if "{" in st else st
        for c0, c1 in _calls(head):
            expr = head[c0:c1]
            if head.strip() == expr + ";" or re.match(rf"\s*(?:auto|const|{IDENT}\s*\*?)\s+{IDENT}\s*=\s*$", head[:c0]):
                continue   # the statement is the call, or it already is a temporary
            options.append((a, a + c0, a + c1, expr))
    if not options:
        return None
    a, c0, c1, expr = rng.choice(options)
    name = f"tmp{rng.randint(0, 99)}"
    qual = rng.choice(["auto", "const auto", "auto&&"])
    indent = re.search(r"[^\S\n]*$", body[:a]).group(0)
    return body[:a] + f"{qual} {name} = {expr};\n{indent}" + body[a:c0] + name + body[c1:]


def r_remove_temp(body, rng):
    """A temporary used once put back into the statement that uses it."""
    found = list(re.finditer(rf"(?m)^[^\S\n]*(?:const\s+)?(?:auto&*|{IDENT}(?:<[^;=]*>)?\s*[*&]?)\s+({IDENT})\s*=\s*([^;]+);[^\S\n]*\n", body))
    rng.shuffle(found)
    for m in found:
        name, expr = m.group(1), m.group(2).strip()
        rest = body[m.end():]
        if len(re.findall(rf"\b{name}\b", rest)) != 1:
            continue
        bare = re.fullmatch(rf"(?:{IDENT}(?:->|\.|::))*{IDENT}", expr) or (_calls(expr) and _calls(expr)[0] == (0, len(expr)))
        rest = re.sub(rf"\b{name}\b", lambda _: expr if bare else f"( {expr} )", rest)
        return body[:m.start()] + rest
    return None


def r_split_if_init(body, rng):
    """if( T x = e ) -> T x = e; if( x ), or back."""
    m = re.search(rf"\bif\(\s*((?:const\s+)?{IDENT}(?:<[^()]*>)?\s*\*?\s*({IDENT})\s*=\s*)", body)
    if m:
        close = _match_close(body, body.index("(", m.start()))
        decl = body[m.start(1):close].strip()
        indent = re.search(r"[^\S\n]*$", body[:m.start()]).group(0)
        return body[:m.start()] + f"{decl};\n{indent}if( {m.group(2)} )" + body[close + 1:]
    m = re.search(rf"(?m)^([^\S\n]*)((?:const\s+)?{IDENT}(?:<[^;()]*>)?\s*\*\s*({IDENT})\s*=\s*[^;]+);\s*\n\s*if\(\s*\3\s*\)", body)
    if m:   # if the name is used after the if, the rewrite does not compile and scores nothing
        return body[:m.start()] + f"{m.group(1)}if( {m.group(2)} )" + body[m.end():]
    return None


def r_invert_early_return(body, rng):
    """if( c ) { return a; } return b;  ->  if( !( c ) ) { return b; } return a;"""
    m = re.search(r"\bif\(", body)
    if not m:
        return None
    close = _match_close(body, m.end() - 1)
    cond = body[m.end():close].strip()
    if re.match(rf"(?:const\s+)?{IDENT}(?:<[^()]*>)?\s*\*?\s*{IDENT}\s*=", cond):
        return None   # a declaration: split it first
    rest = body[close + 1:]
    t = re.match(r"\s*\{\s*(return[^;]*;)\s*\}\s*(return[^;]*;)", rest) or re.match(r"\s*(return[^;]*;)\s*(return[^;]*;)", rest)
    if not t:
        return None
    neg = cond[1:].strip() if cond.startswith("!") and "(" not in cond[1:2] and " " not in cond[1:].strip() else f"!( {cond} )"
    end = close + 1 + t.end()
    return body[:m.start()] + f"if( {neg} ) {{ {t.group(2)} }}\n\t{t.group(1)}" + body[end:]


def r_swap_if_else(body, rng):
    """if( c ) { A } else { B }  ->  if( !( c ) ) { B } else { A }"""
    for m in re.finditer(r"\bif\(", body):
        close = _match_close(body, m.end() - 1)
        b1 = body.find("{", close)
        if b1 < 0 or body[close + 1:b1].strip():
            continue
        e1 = _match_close(body, b1, "{", "}")
        rest = body[e1 + 1:]
        em = re.match(r"\s*else\s*\{", rest)
        if not em:
            continue
        b2 = e1 + 1 + em.end() - 1
        e2 = _match_close(body, b2, "{", "}")
        cond = body[m.end():close].strip()
        return (body[:m.start()] + f"if( !( {cond} ) )" + body[close + 1:b1] + body[b2:e2 + 1]
                + body[e1 + 1:b2] + body[b1:e1 + 1] + body[e2 + 1:])
    return None


def r_condition(body, rng):
    """A condition written another way: x <-> x != nullptr, !x <-> x == nullptr, a == b <-> !( a != b )."""
    options = []
    for m in re.finditer(r"\bif\(", body):
        close = _match_close(body, m.end() - 1)
        options.append((m.end(), close))
    if not options:
        return None
    a, b = rng.choice(options)
    cond = body[a:b].strip()
    if re.fullmatch(rf"!\s*{IDENT}", cond):
        new = f"{cond[1:].strip()} == nullptr"
    elif re.fullmatch(rf"({IDENT}) == nullptr", cond):
        new = f"!{cond.split()[0]}"
    elif re.fullmatch(rf"{IDENT}(?:->{IDENT}|\.{IDENT})*", cond):
        new = f"{cond} != nullptr"
    elif m := re.fullmatch(r"(.+?) != nullptr", cond):
        new = m.group(1)
    elif " == " in cond and "&&" not in cond and "||" not in cond:
        new = "!( " + cond.replace(" == ", " != ", 1) + " )"
    else:
        new = f"!!( {cond} )"
    return body[:a] + f" {new} " + body[b:]


def r_cast(body, rng):
    """(T)x <-> static_cast< T >( x ); a bool return made an explicit comparison, or back."""
    found = list(re.finditer(rf"\((u?int(?:8|16|32|64)|float|double|bool|{IDENT}\s*\*)\)\s*({IDENT}(?:->{IDENT}|\.{IDENT})*)", body))
    found += list(re.finditer(rf"static_cast<\s*([^<>]+?)\s*>\(\s*({IDENT}(?:->{IDENT}|\.{IDENT})*)\s*\)", body))
    ret = list(re.finditer(rf"\breturn\s+({IDENT}(?:->{IDENT}|\.{IDENT})*)\s*;", body)) + \
        list(re.finditer(rf"\breturn\s+({IDENT}(?:->{IDENT}|\.{IDENT})*) != 0\s*;", body))
    choice = rng.choice(found + ret) if found + ret else None
    if choice is None:
        return None
    text = choice.group(0)
    if text.startswith("return"):
        new = f"return {choice.group(1)};" if "!= 0" in text else f"return {choice.group(1)} != 0;"
    elif text.startswith("static_cast"):
        new = f"({choice.group(1)}){choice.group(2)}"
    else:
        new = f"static_cast< {choice.group(1)} >( {choice.group(2)} )"
    return body[:choice.start()] + new + body[choice.end():]


def r_const(body, rng):
    """const added to or removed from a local declaration."""
    found = list(re.finditer(rf"(?m)^([^\S\n]*)(const\s+)?((?:auto&*|{IDENT}(?:<[^;=]*>)?\s*[*&]?)\s+{IDENT}\s*=)", body))
    if not found:
        return None
    m = rng.choice(found)
    return body[:m.start()] + m.group(1) + ("" if m.group(2) else "const ") + m.group(3) + body[m.end():]


COMMUTATIVE = (" == ", " != ", " + ", " * ", " & ", " | ", " ^ ")
LEFT_EDGE = re.compile(r"(?:[(,=?:]|\breturn|&&|\|\|)\s*$")
RIGHT_EDGE = re.compile(r"\s*(?:[),;?:]|&&|\|\|)")


def _operand_end(text, i):
    """The end of the operand starting at text[i]: a name or a number, then members, scopes, calls and
    subscripts; or a parenthesized expression. None where none starts."""
    m = re.match(rf"{IDENT}|\d[\w.]*|\(", text[i:])
    if not m:
        return None
    j = _match_close(text, i) + 1 if m.group(0) == "(" else i + m.end()
    while j < len(text):
        if m := re.match(rf"(?:->|\.|::){IDENT}", text[j:]):
            j += m.end()
        elif text[j] in "([":
            j = _match_close(text, j, text[j], ")" if text[j] == "(" else "]") + 1
        else:
            break
    return j


def r_swap_operands(body, rng):
    """a == b -> b == a, and the same for != + * & | ^: only where the expression is a whole operand of its
    own (between brackets, commas, =, return, && or ||), so precedence keeps its meaning."""
    options = []
    for op in COMMUTATIVE:
        for m in re.finditer(re.escape(op), body):
            k, r0 = m.start(), m.end()
            r1 = _operand_end(body, r0)
            if r1 is None or not RIGHT_EDGE.match(body, r1):
                continue
            for l0 in range(k - 1, max(k - 200, 0), -1):
                if (l0 == 0 or not re.match(r"[\w.>:]", body[l0 - 1])) and _operand_end(body, l0) == k:
                    if LEFT_EDGE.search(body[:l0]):
                        options.append((l0, k, r0, r1))
                    break
    if not options:
        return None
    l0, k, r0, r1 = rng.choice(options)
    return body[:l0] + body[r0:r1] + body[k:r0] + body[l0:k] + body[r1:]


SYMMETRIC = ("Dist", "DistSquared", "Dist2D", "DistSquared2D", "DistXY", "DistSquaredXY", "DotProduct", "Min", "Max")


def _arguments(text):
    """(start, end) of each top-level argument of an argument list's text, spaces trimmed."""
    out, depth, start = [], 0, 0
    for i, c in enumerate(text + ","):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            piece = text[start:i]
            out.append((start + len(piece) - len(piece.lstrip()), i - (len(piece) - len(piece.rstrip()))))
            start = i + 1
    return out


def r_swap_arguments(body, rng):
    """The two arguments of a symmetric call swapped: FVector::Dist( a, b ) -> FVector::Dist( b, a ), and
    DistSquared, Dist2D, DotProduct, FMath::Min and Max."""
    options = []
    for m in re.finditer(rf"\b(?:{IDENT}::)*(?:{'|'.join(SYMMETRIC)})\s*(?:<[^<>()]*>)?\s*\(", body):
        close = _match_close(body, m.end() - 1)
        args = _arguments(body[m.end():close])
        if len(args) == 2 and all(a < b for a, b in args):
            options.append([(m.end() + a, m.end() + b) for a, b in args])
    if not options:
        return None
    (a0, a1), (b0, b1) = rng.choice(options)
    return body[:a0] + body[b0:b1] + body[a1:b0] + body[a0:a1] + body[b1:]


def r_move_declaration(body, rng):
    """A local's declaration moved up or down one statement, in any block: not past a statement that uses
    the name, nor above one that declares a name its initializer uses."""
    options = []
    for b0 in [i for i, c in enumerate(body) if c == "{"]:
        try:
            block = body[b0:_match_close(body, b0, "{", "}") + 1]
            sts = statements(block)
        except Fail:
            continue
        for k, (a, e) in enumerate(sts):
            name = _declared(block[a:e])
            if not name or "{" in block[a:e]:
                continue
            for other in (k - 1, k + 1):
                if not 0 <= other < len(sts):
                    continue
                oa, oe = sts[other]
                st = block[oa:oe]
                if re.match(r"(return|break|continue|goto|case|default)\b", st) or re.search(rf"\b{name}\b", st):
                    continue
                if (theirs := _declared(st)) and re.search(rf"\b{theirs}\b", block[a:e]):
                    continue
                first, second = sorted([(a, e), (oa, oe)])
                if block[first[1]:second[0]].strip():
                    continue  # a comment between them
                options.append((b0 + first[0], b0 + first[1], b0 + second[0], b0 + second[1]))
    if not options:
        return None
    f0, f1, s0, s1 = rng.choice(options)
    return body[:f0] + body[s0:s1] + body[f1:s0] + body[f0:f1] + body[s1:]


REWRITES = [r_swap_statements, r_introduce_temp, r_remove_temp, r_split_if_init, r_invert_early_return,
            r_swap_if_else, r_condition, r_cast, r_const, r_swap_operands, r_swap_arguments, r_move_declaration]


def mutate(body, rng, max_steps=2):
    """One candidate: one or two rewrites, each picked at random among those that apply."""
    out = body
    for _ in range(rng.randint(1, max_steps)):
        for rewrite in rng.sample(REWRITES, len(REWRITES)):
            try:
                new = rewrite(out, rng)
            except (Fail, IndexError, ValueError):
                new = None
            if new and new != out:
                out = new
                break
    return out if out != body else None


# ---------------------------------------------------------------------------------------------- targeted rules
# What the careful passes found by hand, tried in turn before any random rewrite: each rule gives one candidate
# per place it fits, deterministically. Each works on a whole definition (signature, init list, body) and
# returns the rewritten definitions.

@dataclass
class InitEntry:
    name: str
    start: int     # in the definition's text
    end: int
    args: str      # between the brackets, trimmed
    braced: bool   # mX{ ... } rather than mX( ... )


class Definition:
    """A function definition's parts: head (the signature to its parameter list's closing bracket), the
    init list's entries, the body (from its opening brace)."""

    def __init__(self, text):
        self.text = text
        m = None
        for m in re.finditer(r"\(", text):
            if not re.search(r"\boperator\s*$", text[:m.start()]):
                break
        if m is None:
            raise Fail("no parameter list")
        close = _match_close(text, m.start())
        self.head = text[:close + 1]
        self.body_start = _body_brace(text, close)
        if self.body_start is None:
            raise Fail("no body")
        self.body = text[self.body_start:]
        q = re.search(rf"((?:{IDENT}(?:<[^()]*>)?::)*)(~?{IDENT})\s*$", text[:m.start()])
        scopes = [re.sub(r"<.*", "", x) for x in q.group(1).split("::") if x] if q else []
        self.name = q.group(2) if q else ""
        self.cls = scopes[-1] if scopes else ""
        self.constructor = bool(self.cls) and self.name == self.cls
        self.init, self.init_colon = [], None
        i = close + 1
        colon = re.match(r"[^:{]*?(?<!:):(?!:)", text[i:self.body_start])
        if colon:
            self.init_colon = i + colon.end() - 1
            i = self.init_colon + 1
            while True:
                m2 = re.match(rf"\s*({IDENT}(?:::{IDENT})*(?:\s*<[^(){{}};]*>)?)\s*", text[i:])
                if not m2 or text[i + m2.end()] not in "({":
                    break
                j = i + m2.end()
                e = _match_close(text, j, text[j], ")" if text[j] == "(" else "}")
                self.init.append(InitEntry(m2.group(1), i + m2.start(1), e + 1, text[j + 1:e].strip(), text[j] == "{"))
                m3 = re.match(r"\s*,", text[e + 1:])
                if not m3:
                    break
                i = e + 1 + m3.end()

    def with_init(self, entries, body=None):
        """The definition with these init-list entries (texts, in order) and this body."""
        body = self.body if body is None else body
        t = self.text
        if self.init:
            first, last = self.init[0], self.init[-1]
            if len(self.init) > 1:
                sep = t[self.init[0].end:self.init[1].start]
                sep = sep[:sep.index(",") + 1] + sep[sep.index(",") + 1:]
            else:
                prefix = t[self.init_colon - 50 if self.init_colon > 50 else 0:self.init_colon]
                line = prefix[prefix.rfind("\n") + 1:]
                sep = ("\n" + line + ", ") if "\n" in t[len(self.head):first.start] else ", "
            if not entries:
                return self.head + t[last.end:self.body_start].rstrip(" \t").lstrip(" \t") + body \
                    if "\n" in t[last.end:self.body_start] else self.head + " " + body
            return t[:first.start] + sep.join(entries) + t[last.end:self.body_start] + body
        if not entries:
            return t[:self.body_start] + body
        between = t[len(self.head):self.body_start]
        return self.head + between.rstrip() + "\n\t: " + "\n\t, ".join(entries) + "\n" + body


@dataclass
class Member:
    name: str
    type: str
    array: bool
    initialized: bool   # a default in the class (= x, { x })
    bitfield: bool = False


MACRO = re.compile(r"\s*(?:(?:public|protected|private)\s*:|[A-Z][A-Z0-9_]*\s*(?=\()|[A-Z][A-Z0-9_]*\b(?=\s*$))")


def _strip_comments(text):
    return re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.S)


def class_members(header, cls):
    """The data members a class declares in a header's text, in declaration order (static ones, typedefs,
    friends, methods and nested types left out); None when the header does not define the class."""
    text = _strip_comments(header)
    m = re.search(rf"\b(?:class|struct)\s+(?:{IDENT}\s+)?{re.escape(cls)}\b[^;{{()]*\{{", text)
    if not m:
        return None
    end = _match_close(text, m.end() - 1, "{", "}")
    i, acc, out = m.end(), "", []
    while i < end:
        c = text[i]
        if c == "(":
            j = _match_close(text, i)
            acc += text[i:j + 1]
            i = j + 1
            continue
        if c == "{":
            j = _match_close(text, i, "{", "}")
            head = _without_macros(acc)
            if "(" in head or re.search(r"\b(?:class|struct|enum|union|namespace)\b", head):
                acc = ""   # a method's body or a nested type: skipped, with its ';'
                k = re.match(r"\s*;", text[j + 1:])
                i = j + 1 + (k.end() if k else 0)
                continue
            acc += text[i:j + 1]
            i = j + 1
            continue
        if c == ";":
            member = _member(_without_macros(acc))
            if member:
                out.append(member)
            acc = ""
        else:
            acc += c
        i += 1
    return out


def _without_macros(decl):
    """A declaration without the UE macros and access labels before it (UPROPERTY( ... ), GENERATED_BODY())."""
    while True:
        m = MACRO.match(decl)
        if not m or not m.group(0).strip():
            return decl.strip()
        j = m.end()
        if j < len(decl) and decl[j] == "(":
            j = _match_close(decl, j) + 1
        decl = decl[j:]


def _member(decl):
    if not decl or "(" in decl or re.match(r"(?:static|typedef|using|friend|enum|template|virtual|class|struct)\b", decl):
        return None
    m = re.fullmatch(rf"(?:mutable\s+)?(.+?)\s*\b({IDENT})\s*(\[[^\]]*\])?\s*(:\s*\w+)?\s*(=.*|\{{.*\}})?", decl, re.S)
    if not m or re.search(r",(?![^<]*>)", m.group(1)) or not re.search(r"\w", m.group(1)):
        return None
    return Member(m.group(2), " ".join(m.group(1).split()), bool(m.group(3)), bool(m.group(5)), bool(m.group(4)))


def _pointer_like(member):
    return "*" in member.type or re.match(r"(?:TObjectPtr|TSubclassOf|TWeakObjectPtr|TSoftObjectPtr)\b", member.type)


def _init_order(members, names):
    """For init-list entries named `names`, the index of each in the class's declaration order (-1 for a base
    class or anything the class does not declare, which stays first)."""
    order = {m.name: k for k, m in enumerate(members or [])}
    return [order.get(n, -1) for n in names]


def _insert_entry(d, entries, name, text, members):
    """entries (texts) of d with `text` inserted where the class declares `name` (at the end when unknown)."""
    names = [e.name for e in d.init] if len(entries) == len(d.init) else [re.match(rf"\s*({IDENT})", e).group(1) for e in entries]
    order = _init_order(members, names)
    mine = _init_order(members, [name])[0]
    at = len(entries)
    if mine >= 0:
        at = next((k for k, o in enumerate(order) if o > mine), len(entries))
    return entries[:at] + [text] + entries[at:]


def _body_statements(d):
    """(start, end, text) of the body's top-level statements, in the definition's offsets."""
    return [(d.body_start + a, d.body_start + b, d.body[a:b]) for a, b in statements(d.body)]


def _cut_statement(text, a, b):
    """text without the statement at a:b: its whole line when it is alone on it."""
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    if not text[ls:a].strip() and (le < 0 or not text[b:le].strip()):
        return text[:ls] + text[le + 1:] if le >= 0 else text[:ls]
    return text[:a] + text[b:].lstrip(" ")


def t_init_in(definition, members=None):
    """A constructor's body assignment (mX = v;) moved into the init list (mX( v )), where the class declares
    the member; and all of them at once."""
    d = Definition(definition)
    if not d.constructor:
        return []
    declared = {m.name for m in members or []}
    found = []
    for a, b, st in _body_statements(d):
        m = re.fullmatch(rf"({IDENT})\s*=\s*([^;]+?)\s*;", st, re.S)
        if m and (not members or m.group(1) in declared) and m.group(1) not in {e.name for e in d.init}:
            found.append((a, b, m.group(1), m.group(2)))
    out, everything = [], None
    for a, b, name, value in found:
        entries = _insert_entry(d, [definition[e.start:e.end] for e in d.init], name, f"{name}( {value} )", members)
        body = _cut_statement(definition, a, b)[d.body_start:]
        out.append(d.with_init(entries, body))
    if len(found) > 1:
        entries, text = [definition[e.start:e.end] for e in d.init], definition
        for a, b, name, value in reversed(found):
            text = _cut_statement(text, a, b)
        for a, b, name, value in found:
            entries = _insert_entry(d, entries, name, f"{name}( {value} )", members)
        out.append(d.with_init(entries, text[d.body_start:]))
    return out


def _is_base(entry, d):
    return entry.name == "Super" or re.match(r"[AUFTIES][A-Z]", entry.name) or "::" in entry.name or "<" in entry.name


def t_init_out(definition, members=None):
    """An init-list entry with one value (mX( v )) moved to the top of the constructor's body (mX = v;)."""
    d = Definition(definition)
    out = []
    for k, e in enumerate(d.init):
        if _is_base(e, d) or not e.args or _arguments(e.args)[1:] or re.fullmatch(r"ForceInit\w*|EForceInit\w*", e.args):
            continue
        entries = [definition[x.start:x.end] for x in d.init if x is not e]
        indent = re.search(r"\n([ \t]*)\S", d.body)
        body = d.body[0] + "\n" + (indent.group(1) if indent else "\t") + f"{e.name} = {e.args};" + d.body[1:]
        out.append(d.with_init(entries, body))
    return out


ZERO = re.compile(r"ForceInitToZero|ForceInit|EForceInit::ForceInitToZero|0|0\.0?f?|nullptr|false")


def t_init_value(definition, members=None):
    """Init-list entries value-initialized: mX( ForceInitToZero ), mX( 0 ), mX{ 0, 0 }, mX( nullptr ) -> mX();
    mX() -> mX{} and mX( nullptr )."""
    d = Definition(definition)
    out = []
    for e in d.init:
        if _is_base(e, d):
            continue
        if e.braced or (e.args and ZERO.fullmatch(e.args)):
            news = [f"{e.name}()"]
        elif not e.args:
            news = [f"{e.name}{{}}", f"{e.name}( nullptr )"]
        else:
            continue
        for new in news:
            out.append(definition[:e.start] + new + definition[e.end:])
    return out


def t_init_add(definition, members=None):
    """Members the class declares without a default, that the constructor neither names in its init list nor
    assigns in its body, named in the init list: mX() (an array, a vector, a number) or mX( nullptr )
    (a pointer, TObjectPtr, TSubclassOf); each alone, then the arrays together, then all of them."""
    d = Definition(definition)
    if not d.constructor or not members:
        return []
    named = {e.name for e in d.init}
    assigned = set(re.findall(rf"(?m)^\s*({IDENT})\s*(?:=[^=]|\.|->|\[)", d.body))
    todo = [m for m in members if not m.initialized and not m.bitfield and m.name not in named and m.name not in assigned]
    entries = [definition[e.start:e.end] for e in d.init]

    def entry(m):
        return f"{m.name}( nullptr )" if _pointer_like(m) and not m.array else f"{m.name}()"

    def adding(ms):
        es = entries
        for m in ms:
            es = _insert_entry(d, es, m.name, entry(m), members)
        return d.with_init(es)

    out = [adding([m]) for m in todo]
    arrays = [m for m in todo if m.array]
    if len(arrays) > 1:
        out.append(adding(arrays))
    if len(todo) > 1 and len(todo) != len(arrays):
        out.append(adding(todo))
    return out


STRING = r'"(?:[^"\\\n]|\\.)*"'
LITERAL = rf'(?:TEXT\(\s*{STRING}\s*\)|{STRING})'
NOT_WRAPPED = ("TEXT", "FName", "FString", "FText", "TCHAR_TO_ANSI", "ANSI_TO_TCHAR", "UE_LOG", "checkf", "ensureMsgf",
               "TEXTVIEW", "INVTEXT", "LOCTEXT", "NSLOCTEXT", "Printf", "static_assert", "printf", "sizeof")


def _after_head(definition):
    return len(Definition(definition).head)


def _bare_literals(text, start):
    """(start, end) of plain "..." literals passed straight as a call's argument, after `start`, not inside
    TEXT(), a log macro or a constructor of a string type."""
    out = []
    for m in re.finditer(STRING, text):
        if m.start() < start or text[m.start() - 1:m.start()] in ("L", "u", "U", "8"):
            continue
        before = text[:m.start()].rstrip()
        after = text[m.end():].lstrip()
        if not before.endswith(("(", ",")) or not after.startswith((")", ",")):
            continue
        call = re.search(rf"({IDENT})\s*\($", before[:len(before) - (len(before) - before.rfind("(") - 1)] if before.endswith("(") else "")
        inner = _enclosing_call(text, m.start())
        if inner in NOT_WRAPPED or (call and call.group(1) in NOT_WRAPPED):
            continue
        out.append((m.start(), m.end()))
    return out


def _enclosing_call(text, i):
    """The name of the call whose argument list holds text[i], or None."""
    depth = 0
    for j in range(i - 1, -1, -1):
        c = text[j]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                m = re.search(rf"({IDENT})\s*$", text[:j])
                return m.group(1) if m else None
            depth -= 1
        elif c in ";{}" and depth == 0:
            return None
    return None


def t_fname(definition, members=None):
    """FName( "X" ) passed as an argument -> "X" (the implicit conversion: the temporary is then addressed by
    its stack slot); and a plain literal argument -> FName( "X" )."""
    start = _after_head(definition)
    out = []
    for m in re.finditer(rf"\bFName\(\s*({LITERAL})\s*\)", definition):
        if m.start() >= start:
            out.append(definition[:m.start()] + m.group(1) + definition[m.end():])
    for a, b in _bare_literals(definition, start):
        out.append(definition[:a] + f"FName( {definition[a:b]} )" + definition[b:])
    return out


def t_fstring(definition, members=None):
    """An explicit FString temporary made implicit (FString( TEXT( "X" ) ) -> TEXT( "X" ), FString( "X" ) + s
    -> "X" + s), and a literal assigned or concatenated made an explicit FString( ... )."""
    start = _after_head(definition)
    out = []
    for m in re.finditer(rf"\bFString\(\s*({LITERAL})\s*\)", definition):
        if m.start() >= start:
            out.append(definition[:m.start()] + m.group(1) + definition[m.end():])
    for m in re.finditer(LITERAL, definition):
        if m.start() < start or (m.group(0).startswith('"') and definition[m.start() - 5:m.start()].rstrip().endswith("TEXT(")):
            continue
        before = definition[:m.start()].rstrip()
        after = definition[m.end():].lstrip()
        if re.search(r"(?<![=!<>+\-*/%&|^])=$|\+$|\+=$", before) or after.startswith("+") and not after.startswith("++"):
            if _enclosing_call(definition, m.start()) in NOT_WRAPPED:
                continue
            out.append(definition[:m.start()] + f"FString( {m.group(0)} )" + definition[m.end():])
    return out


RANGE_FOR = re.compile(rf"\bfor\(\s*(const\s+)?((?:{IDENT}(?:::{IDENT})*)(?:\s*<[^;:()]*>)?\s*\**)\s*(&{{0,2}})\s*({IDENT})\s*:(?!:)")


def t_loop_reference(definition, members=None):
    """A range-for variable by reference instead of by value (T* p -> T*& p, T x -> T& x and const T& x), and
    the reverse (const T& x -> const T x and T x, T& x -> T x)."""
    out = []
    start = _after_head(definition)
    for m in RANGE_FOR.finditer(definition):
        if m.start() < start or m.group(3) == "&&":
            continue
        const, typ, ref, name = m.group(1) or "", m.group(2).strip(), m.group(3), m.group(4)
        if ref:
            forms = [f"{const}{typ} {name}"] + ([f"{typ} {name}"] if const else [])
        elif typ.endswith("*"):
            forms = [f"{const}{typ}& {name}"]
        else:
            forms = [f"{const}{typ}& {name}"] + ([] if const else [f"const {typ}& {name}"])
        for form in forms:
            out.append(definition[:m.start()] + f"for( {form} :" + definition[m.end():])
    return out


COUNTERS = ("int32", "uint32", "int64", "uint64", "int")


def t_counter_type(definition, members=None):
    """A for loop's counter of another integer type: int32, uint32, int64, uint64, int."""
    out = []
    for m in re.finditer(r"\bfor\(\s*(int32|uint32|int64|uint64|int|int8|uint8|int16|uint16|SIZE_T|size_t)\s+(?=[A-Za-z_]\w*\s*=)", definition):
        for t in COUNTERS:
            if t != m.group(1):
                out.append(definition[:m.start(1)] + t + definition[m.end(1):])
    return out


def _blocks(text, start):
    """Offsets of every '{' opening a block at or after start (the body and the blocks inside it)."""
    return [i for i in range(start, len(text)) if text[i] == "{"]


def _names(text):
    return set(re.findall(IDENT, text))


def _writes(text, names):
    """Whether text assigns, increments or takes the address of one of names."""
    for n in names & _names(text):
        if re.search(rf"\b{n}\s*(?:[-+*/%&|^]?=(?!=)|\+\+|--)|(?:\+\+|--|&)\s*{n}\b", text):
            return True
    return False


MOVE_REACH = 4   # statements a declaration moves at most, each way


def t_move_declaration(definition, members=None):
    """A local's declaration moved to every other place in its block it can go, at most MOVE_REACH statements
    away: not past a statement that names it, not across one that assigns something its initializer reads."""
    d = Definition(definition)
    out, seen = [], set()
    for b0 in _blocks(definition, d.body_start):
        try:
            block = definition[b0:_match_close(definition, b0, "{", "}") + 1]
            sts = statements(block)
        except Fail:
            continue
        for k, (a, e) in enumerate(sts):
            st = block[a:e]
            name = _declared(st)
            if not name or "{" in st:
                continue
            init = st.split("=", 1)[1] if "=" in st else st[st.find("(") + 1:] if "(" in st else ""
            reads = _names(init) - {name}
            for direction in (-1, 1):
                j = k
                while 0 <= j + direction < len(sts) and abs(j + direction - k) <= MOVE_REACH:
                    oa, oe = sts[j + direction]
                    other = block[oa:oe]
                    if re.match(r"(return|break|continue|goto|case|default)\b", other) or name in _names(other) \
                            or _writes(other, reads):
                        break
                    if block[min(oe, a):max(oa, e)].count("//") or "/*" in block[min(oe, a):max(oa, e)]:
                        break
                    j += direction
                    pieces = [block[x:y] for x, y in sts]
                    moved = pieces[:k] + pieces[k + 1:]
                    moved.insert(j, pieces[k])
                    new_block = block[:sts[0][0]]
                    for i, (x, y) in enumerate(sts):
                        new_block += moved[i] + (block[y:sts[i + 1][0]] if i + 1 < len(sts) else "")
                    new_block += block[sts[-1][1]:]
                    text = definition[:b0] + new_block + definition[b0 + len(block):]
                    if text not in seen:
                        seen.add(text)
                        out.append(text)
    return out


def t_hoist_test(definition, members=None):
    """An if's test taken into a local before it (const bool bTest = c; if( bTest ))."""
    d = Definition(definition)
    out = []
    for m in re.finditer(r"\bif\(", definition):
        if m.start() < d.body_start or re.search(r"\belse\s*$", definition[:m.start()]):
            continue
        close = _match_close(definition, m.end() - 1)
        cond = definition[m.end():close].strip()
        if re.fullmatch(rf"!?\s*{IDENT}", cond) or re.match(rf"(?:const\s+)?{IDENT}(?:<[^()]*>)?\s*[*&]?\s*{IDENT}\s*=[^=]", cond):
            continue
        ls = definition.rfind("\n", 0, m.start()) + 1
        if definition[ls:m.start()].strip():
            continue   # not at the start of its line
        indent = definition[ls:m.start()]
        out.append(definition[:m.start()] + f"const bool bTest = {cond};\n{indent}if( bTest )" + definition[close + 1:])
    return out


def t_inline_local(definition, members=None):
    """A local used once put back into the statement that uses it (each one)."""
    d = Definition(definition)
    out = []
    pat = rf"(?m)^[^\S\n]*(?:const\s+)?(?:auto&*|{IDENT}(?:::{IDENT})*(?:<[^;=]*>)?\s*[*&]?)\s+({IDENT})\s*=\s*([^;]+);[^\S\n]*\n"
    for m in re.finditer(pat, definition):
        if m.start() < d.body_start:
            continue
        name, expr = m.group(1), m.group(2).strip()
        if name in ("return", "delete"):
            continue
        rest = definition[m.end():]
        if len(re.findall(rf"\b{name}\b", rest)) != 1:
            continue
        bare = re.fullmatch(rf"(?:{IDENT}(?:->|\.|::))*{IDENT}", expr) or (_calls(expr) and _calls(expr)[0] == (0, len(expr)))
        rest = re.sub(rf"\b{name}\b", lambda _: expr if bare else f"( {expr} )", rest)
        out.append(definition[:m.start()] + rest)
    return out


def t_swap_terms(definition, members=None):
    """Two neighbouring terms of an if's or a while's || or && chain swapped (MSVC lays the terms out in
    source order)."""
    out = []
    for m in re.finditer(r"\b(?:if|while)\(", definition):
        close = _match_close(definition, m.end() - 1)
        cond = definition[m.end():close]
        for op in ("||", "&&"):
            pieces, depth, last = [], 0, 0
            k = 0
            while k < len(cond):
                c = cond[k]
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                elif depth == 0 and cond.startswith(op, k):
                    pieces.append((last, k))
                    last = k + 2
                    k += 2
                    continue
                k += 1
            pieces.append((last, len(cond)))
            if len(pieces) < 2:
                continue
            other = "&&" if op == "||" else "||"
            if any(other in cond[a:b] and not cond[a:b].strip().startswith("(") for a, b in pieces):
                continue   # mixed without brackets: precedence
            for i in range(len(pieces) - 1):
                (a1, b1), (a2, b2) = pieces[i], pieces[i + 1]
                t1, t2 = cond[a1:b1], cond[a2:b2]
                new = cond[:a1] + _respace(t1, t2) + cond[b1:a2] + _respace(t2, t1) + cond[b2:]
                out.append(definition[:m.end()] + new + definition[close:])
    return out


def _respace(old, new):
    """new with old's leading and trailing spaces."""
    lead = old[:len(old) - len(old.lstrip())]
    trail = old[len(old.rstrip()):]
    return lead + new.strip() + trail


SIMPLE_ARG = re.compile(rf"(?:[&*]\s*)?(?:this|{IDENT}(?:(?:->|\.|::){IDENT})*|-?\d[\w.]*|{STRING})")


def t_swap_arguments(definition, members=None):
    """Two neighbouring arguments of a call swapped, where every argument is free of side effects (names,
    members, numbers, literals): one swap per candidate."""
    d = Definition(definition)
    out = []
    for c0, c1 in _calls(definition):
        if c0 < d.body_start:
            continue
        open_ = definition.index("(", c0)
        args = _arguments(definition[open_ + 1:c1 - 1])
        if len(args) < 2 or not all(SIMPLE_ARG.fullmatch(definition[open_ + 1 + a:open_ + 1 + b]) for a, b in args):
            continue
        for i in range(len(args) - 1):
            (a0, a1), (b0, b1) = [(open_ + 1 + x, open_ + 1 + y) for x, y in args[i:i + 2]]
            out.append(definition[:a0] + definition[b0:b1] + definition[a1:b0] + definition[a0:a1] + definition[b1:])
    return out


def t_swap_statements(definition, members=None):
    """Two neighbouring simple statements swapped (every pair, one per candidate), in every block, when
    neither names what the other declares."""
    d = Definition(definition)
    out = []
    for b0 in _blocks(definition, d.body_start):
        try:
            block = definition[b0:_match_close(definition, b0, "{", "}") + 1]
            sts = statements(block)
        except Fail:
            continue
        sts = [(a, b) for a, b in sts if not re.match(r"(return|if|for|while|switch|do|break|continue|case|default)\b", block[a:b])
               and "{" not in block[a:b]]
        for (a1, b1), (a2, b2) in zip(sts, sts[1:]):
            if block[b1:a2].strip():
                continue
            s1, s2 = block[a1:b1], block[a2:b2]
            if any(n and re.search(rf"\b{n}\b", other) for n, other in ((_declared(s1), s2), (_declared(s2), s1))):
                continue
            new_block = block[:a1] + s2 + block[b1:a2] + s1 + block[b2:]
            out.append(definition[:b0] + new_block + definition[b0 + len(block):])
    return out


def _each(rule, tries=60, cap=12):
    def run(definition, members=None):
        d = Definition(definition)
        head, body = definition[:d.body_start], d.body
        out = []
        for seed in range(tries):
            try:
                new = rule(body, random.Random(seed))
            except (Fail, IndexError, ValueError):
                new = None
            if new and new != body and head + new not in out:
                out.append(head + new)
                if len(out) >= cap:
                    break
        return out
    run.__name__ = "t_" + rule.__name__[2:]
    run.__doc__ = f"Each distinct candidate of the random rule {rule.__name__}."
    return run


TARGETED = [t_init_add, t_init_in, t_init_value, t_init_out, t_fname, t_fstring, t_loop_reference, t_counter_type,
            t_move_declaration, t_hoist_test, t_inline_local, t_swap_terms, t_swap_arguments, t_swap_statements,
            _each(r_swap_operands), _each(r_condition), _each(r_split_if_init), _each(r_invert_early_return),
            _each(r_swap_if_else), _each(r_cast), _each(r_const), _each(r_introduce_temp, cap=6)]


def rule_name(rule):
    return rule.__name__[2:].replace("_", "-")


@dataclass
class Edit:
    rule: str
    text: str      # the whole definition, rewritten
    change: str    # what changed, with some context: the same change made on another base reads the same


def change_of(base, new, context=40):
    p = 0
    while p < min(len(base), len(new)) and base[p] == new[p]:
        p += 1
    s = 0
    while s < min(len(base), len(new)) - p and base[-1 - s] == new[-1 - s]:
        s += 1
    return f"{base[max(0, p - context):p]}[{base[p:len(base) - s]}->{new[p:len(new) - s]}]{base[len(base) - s:len(base) - s + context]}"


def targeted(definition, members=None, rules=None):
    """Every targeted candidate for a definition, rule by rule (TARGETED's order), each text once."""
    out, seen = [], {definition}
    for rule in rules or TARGETED:
        try:
            texts = rule(definition, members=members)
        except (Fail, IndexError, ValueError):
            texts = []
        for text in texts:
            if text not in seen:
                seen.add(text)
                out.append(Edit(rule_name(rule), text, rule_name(rule) + ": " + change_of(definition, text)))
    return out


def keep_lines(old, new):
    """new laid out on as many lines as old: where it grew, blank lines after the change (or before it) are
    given back; where it shrank, blank lines are added after the change. So what follows the function keeps
    its line numbers (a laid-out file's functions sit on their PDB lines)."""
    a, b = old.split("\n"), new.split("\n")
    if len(a) == len(b):
        return new
    ops = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    bounds = [0] + [x for op in ops for x in (op[3], op[4])] + [len(b)]
    for n, (tag, i1, i2, j1, j2) in reversed(list(enumerate(ops))):
        d = (j2 - j1) - (i2 - i1)
        if d < 0:
            b[j2:j2] = [""] * -d
        elif d > 0:
            hi = bounds[2 * n + 3] if 2 * n + 3 < len(bounds) else len(b)
            lo = bounds[2 * n]
            after = [k for k in range(j2, min(hi, len(b) - 1)) if not b[k].strip()]
            before = [k for k in range(j1 - 1, max(lo, 1) - 1, -1) if not b[k].strip()]
            for k in sorted((after + before)[:d], reverse=True):
                del b[k]
    return "\n".join(b)


# ---------------------------------------------------------------------------------------------- the loop

@dataclass
class Candidate:
    score: int
    body: str
    seconds: float = 0.0


@dataclass
class Run:
    function: str
    start: int
    best: list = field(default_factory=list)   # Candidates, best first
    tries: int = 0       # candidates compiled
    failed: int = 0      # of them, those that did not compile
    exhausted: bool = False


_MATCH = threading.Lock()   # workers share the shipped module, whose match state is per call


def score_once(project, blob, function, out_dir, module="FactoryGameRecomp", shipped=None, include_first=None):
    """(score, seconds) for the staged file as it is now, or with the files in include_first standing in for
    the staged ones; score None when it does not compile or the function is not in the object. shipped: the
    shipped module already read (decomp.Module), else read here; only `function` is compared."""
    started = time.monotonic()
    c = msvc.compile_one(project, module, blob, out_dir, include_first=include_first)
    if not c.ok:
        return None, time.monotonic() - started
    with _MATCH:
        for r in decomp.match(shipped or msvc.shipped_dll(module), c.obj, only=function):
            return r.score, time.monotonic() - started
    return None, time.monotonic() - started


def recovered_file(project, name):
    path = Path(project) / "Source" / "FactoryGameRecomp-Recovered" / Path(name).name
    if not path.is_file():
        raise Fail(f"{path} not found: stage the project first (Project/stage.sh <dir>)")
    return path


def permute(project, file_name, function, tries=100, minutes=60, seed=None, keep=5, log=print, jobs=3):
    project = Path(project).resolve()
    if project.is_relative_to(config.RECOVERED_REPO.resolve()) or config.inside_repository(project):
        raise Fail(f"{project}: the permuter works on a staged copy, never in the recovered-code repository or here")
    path = recovered_file(project, file_name)
    blob = msvc.blob_name(file_name, project)
    original = path.read_text(encoding="utf-8")
    start, end = find_function(original, function)
    base = original[start:end]
    out_dir = config.MSVC_BUILDS / "permute" / project.name
    workers = [out_dir / f"worker{k}" for k in range(max(1, jobs))]   # each: its objects, and src/ first
    for worker in workers:   # src/ is searched first: a copy an earlier run left there would replace a staged file
        shutil.rmtree(worker / "src", ignore_errors=True)
    rng = random.Random(seed)
    deadline = time.monotonic() + 60 * minutes
    beside = path.parent / f"{path.name}.permute"
    beside.mkdir(exist_ok=True)
    if msvc.pch_of("FactoryGameRecomp", blob):
        msvc.build_pch(msvc.pch_of("FactoryGameRecomp", blob))
    shipped = decomp.Module(msvc.shipped_dll("FactoryGameRecomp"))   # read once for every candidate
    first, took = score_once(project, blob, function, out_dir, shipped=shipped)
    if first is None:
        raise Fail(f"{file_name} does not compile as staged, or {function} is not in {blob}'s object")
    run = Run(function, first, [Candidate(first, base, took)])
    log(f"start: score {first} ({took:.0f} s a try, {len(workers)} at once)")
    seen, idle = {base}, 0

    def attempt(worker, body):
        src = worker / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / path.name).write_text(original[:start] + body + original[end:], encoding="utf-8")
        return score_once(project, blob, function, worker, shipped=shipped, include_first=src)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as pool:
        while run.best[0].score > 0 and run.tries < tries and time.monotonic() < deadline and not run.exhausted:
            bodies = []
            while len(bodies) < min(len(workers), tries - run.tries):
                # rewrite one of the best so far, the best most often
                parent = run.best[min(int(rng.expovariate(1.5)), len(run.best) - 1)]
                body = mutate(parent.body, rng)
                if body is None or body in seen:
                    idle += 1
                    if idle >= 500:   # the rewrites have nothing new to offer
                        run.exhausted = True
                        break
                    continue
                seen.add(body)
                idle = 0
                bodies.append(body)
            for body, (score, took) in zip(bodies, pool.map(attempt, workers, bodies)):
                run.tries += 1
                if score is None:
                    run.failed += 1
                    continue
                if score < run.best[-1].score or len(run.best) < keep:
                    if score < run.best[0].score:
                        log(f"try {run.tries}: score {score}")
                    run.best.append(Candidate(score, body, took))
                    run.best.sort(key=lambda c: c.score)
                    del run.best[keep:]
                    _write(beside, function, original, start, end, run)
    _write(beside, function, original, start, end, run)
    return run, beside


def _write(beside, function, original, start, end, run):
    """The best candidates beside the staged file, the whole file each: <Scope_Function>.<rank>.score<n>.cpp."""
    safe = re.sub(r"\W+", "_", function)
    for old in beside.glob(f"{safe}.*.cpp"):
        old.unlink()
    for i, c in enumerate(run.best):
        (beside / f"{safe}.{i + 1}.score{c.score}.cpp").write_text(original[:start] + c.body + original[end:],
                                                                     encoding="utf-8")


def try_variants(project, file_name, variants, functions=(), jobs=3, module="FactoryGameRecomp"):
    """Variants of one recovered file (each a whole file), compiled side by side, each from a worker folder of its
    own searched first (as a copy of the staged project would, without copying it), and matched: rows (variant,
    function, state, score) for each of `functions` (every function of the blob that differs or matches when
    none is named); (variant, None, "failed", the first error) for one that does not compile."""
    project = Path(project).resolve()
    if project.is_relative_to(config.RECOVERED_REPO.resolve()) or config.inside_repository(project):
        raise Fail(f"{project}: variants compile in a staged copy, never in the recovered-code repository or here")
    path = recovered_file(project, file_name)
    blob = msvc.blob_name(file_name, project)
    if msvc.pch_of(module, blob):
        msvc.build_pch(msvc.pch_of(module, blob))
    shipped = decomp.Module(msvc.shipped_dll(module))
    root = config.MSVC_BUILDS / "try" / project.name
    variants = [Path(v) for v in variants]

    def one(k):
        worker = root / f"v{k}"
        src = worker / "src"
        shutil.rmtree(src, ignore_errors=True)
        src.mkdir(parents=True)
        shutil.copyfile(variants[k], src / path.name)
        c = msvc.compile_one(project, module, blob, worker, include_first=src)
        if not c.ok:
            first = next((line for line in c.output.splitlines() if "error" in line), c.output.strip()[:200])
            return [(variants[k], None, "failed", first.strip())]
        with _MATCH:
            rows = decomp.match(shipped, c.obj)
        out = []
        for f in functions or [r.shipped or r.name for r in rows if r.state != "missing"]:
            mine = [r for r in rows if f in (r.name, r.shipped)]
            out.append((variants[k], f, mine[0].state if mine else "missing", mine[0].score if mine else None))
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return [row for rows in pool.map(one, range(len(variants))) for row in rows]
