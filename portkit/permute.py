"""A permuter for the last differences of a recovered function, as decomp-permuter does for the N64
decompilations: small rewrites of the function's C++ text, each compiled on the Mac (`msvc`, one unity blob)
and scored against the shipped function (`decomp.match`'s score: instructions not the same once aligned,
0 when matched). Improvements are kept and rewritten further; it stops at 0 or at a limit.

It works on a staged copy of the project, never the recovered-code repository: the staged file is changed
for each try and put back at the end; the best candidates are written beside it, for a person (or a
session) to carry over by hand. The rewrites are textual, no C++ parser: a rewrite that does not compile
simply scores nothing.
"""

import random
import re
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


def find_function(text, name):
    """(start, end) of the body of `name` (Scope::Function) in text: from its opening brace to just past its
    closing one."""
    for m in re.finditer(rf"^[^\n;{{}}#]*?\b{re.escape(name)}\s*\(", text, re.M):
        close = _match_close(text, m.end() - 1)
        brace = text.find("{", close)
        if brace < 0 or ";" in text[close:brace]:
            continue   # a declaration or a call, not the definition
        return brace, _match_close(text, brace, "{", "}") + 1
    raise Fail(f"no definition of {name} found")


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
    return m.group(1) if m and m.group(1) not in ("return", "delete") else None


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


def score_once(project, blob, function, out_dir, module="FactoryGameRecomp"):
    """(score, seconds) for the staged file as it is now; score None when it does not compile or the
    function is not in the object."""
    started = time.monotonic()
    c = msvc.compile_one(project, module, blob, out_dir)
    if not c.ok:
        return None, time.monotonic() - started
    for r in decomp.match(msvc.shipped_dll(module), c.obj):
        if function in (r.name, r.shipped):
            return r.score, time.monotonic() - started
    return None, time.monotonic() - started


def recovered_file(project, name):
    path = Path(project) / "Source" / "FactoryGameRecomp-Recovered" / Path(name).name
    if not path.is_file():
        raise Fail(f"{path} not found: stage the project first (Project/stage.sh <dir>)")
    return path


def permute(project, file_name, function, tries=100, minutes=60, seed=None, keep=5, log=print):
    project = Path(project).resolve()
    if project.is_relative_to(config.RECOVERED_REPO.resolve()) or config.inside_repository(project):
        raise Fail(f"{project}: the permuter works on a staged copy, never in the recovered-code repository or here")
    path = recovered_file(project, file_name)
    blob = msvc.blob_name(file_name, project)
    original = path.read_text(encoding="utf-8")
    start, end = find_function(original, function)
    base = original[start:end]
    out_dir = config.MSVC_BUILDS / "permute" / project.name
    rng = random.Random(seed)
    deadline = time.monotonic() + 60 * minutes
    beside = path.parent / f"{path.name}.permute"
    beside.mkdir(exist_ok=True)
    try:
        if msvc.pch_of("FactoryGameRecomp", blob):
            msvc.build_pch(msvc.pch_of("FactoryGameRecomp", blob))
        first, took = score_once(project, blob, function, out_dir)
        if first is None:
            raise Fail(f"{file_name} does not compile as staged, or {function} is not in {blob}'s object")
        run = Run(function, first, [Candidate(first, base, took)])
        log(f"start: score {first} ({took:.0f} s a try)")
        seen, idle = {base}, 0
        while run.best[0].score > 0 and run.tries < tries and time.monotonic() < deadline:
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
            path.write_text(original[:start] + body + original[end:], encoding="utf-8")
            score, took = score_once(project, blob, function, out_dir)
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
    finally:
        path.write_text(original, encoding="utf-8")
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
