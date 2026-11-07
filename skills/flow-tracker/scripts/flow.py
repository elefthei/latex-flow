#!/usr/bin/env python3
"""Track definition-to-use flow in a LaTeX theory paper.

    flow.py scan   ROOT.tex [-o flow.jsonl] [--notation FILE ...] [--ignore-macro RE]
    flow.py issues flow.jsonl [-o flow-issues.jsonl]

`scan` flattens the document from ROOT following \\input/\\include in reading
order, records every item (theorem-like environment, labelled object, inference
rule name, notation macro) and every later or earlier use of it, and writes one
JSON object per item.  `issues` is a second, deterministic pass over that file
only: it never re-reads the LaTeX sources.

Positions carry `order`, a monotone offset in the flattened document, so
"before"/"after" comparisons are valid across files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------- configuration

THEOREM_ENVS = {
    "theorem", "lemma", "proposition", "corollary", "definition", "remark",
    "example", "conjecture", "assumption", "notation", "claim", "fact",
    "observation", "axiom", "property", "hypothesis",
}
# Environments whose first occurrence of a macro counts as introducing it.
DEFINING_ENVS = {"definition", "notation"}
FLOAT_ENVS = {"figure", "figure*", "table", "table*", "wrapfigure", "sidewaysfigure"}
MATH_ENVS = {
    "equation", "equation*", "align", "align*", "gather", "gather*",
    "multline", "multline*", "flalign", "flalign*", "alignat", "alignat*",
    "eqnarray", "eqnarray*", "mathpar", "displaymath",
}
SKIP_ENVS = {"comment", "verbatim", "lstlisting", "minted", "tikzpicture"}
SECTIONING = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(?:\[[^\]]*\])?\s*\{"
)
REF = re.compile(
    r"\\(?:auto|name|page|eq|c|C|label|v|V|cpage|full|f)?ref\*?\s*\{([^}]*)\}|\\hyperref\s*\[([^\]]*)\]"
)
LABEL = re.compile(r"\\label\s*\{([^}]*)\}")
BEGIN = re.compile(r"\\begin\s*\{([^}]*)\}")
END = re.compile(r"\\end\s*\{([^}]*)\}")
INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]*)\}")
NEWTHEOREM = re.compile(r"\\newtheorem\*?\s*\{([^}]*)\}")
INFERRULE = re.compile(r"\\inferrule\*?\s*\[\s*(?:right|left|Right|Left|lab)\s*=\s*([^\],]+)")
CONTROL_WORD = re.compile(r"\\([A-Za-z@]+)")
# Commands that bind a control sequence: the body group is excluded from use counting.
MACRO_DEF = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareMathOperator|DeclareRobustCommand|"
    r"NewDocumentCommand|RenewDocumentCommand|ProvideDocumentCommand|DeclarePairedDelimiter|"
    r"def|gdef|edef|let|newenvironment|renewenvironment|NewEnviron|newsavebox)\*?\s*"
    r"(?:\{\s*\\([A-Za-z@]+)\s*\}|\\([A-Za-z@]+))"
)
DEFINITIONAL_OPERATORS = re.compile(
    r":=|\\coloneqq|\\coloneq|\\triangleq|\\defeq|\\mathrel\s*\{\s*:=\s*\}|\\iff\b|&\s*="
)
DEFINING_WORDS = re.compile(r"\b(?:define[sd]?|denote[sd]?|notation|write|abbreviat\w*)\b", re.I)

SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}

# ------------------------------------------------------------------- flattening


@dataclass
class Flat:
    text: str
    # line starts: (offset, file, lineno) sorted by offset
    lines: list[tuple[int, str, int]]
    files: list[str]  # in reading order
    preamble_files: list[str]  # files \input before \begin{document}
    body_start: int

    def locate(self, offset: int) -> tuple[str, int]:
        lo, hi = 0, len(self.lines) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.lines[mid][0] <= offset:
                lo = mid
            else:
                hi = mid - 1
        _, f, n = self.lines[lo]
        return f, n

    def line_text(self, offset: int) -> str:
        f, n = self.locate(offset)
        i = next(i for i, (_, ff, nn) in enumerate(self.lines) if ff == f and nn == n)
        start = self.lines[i][0]
        end = self.lines[i + 1][0] if i + 1 < len(self.lines) else len(self.text)
        return self.text[start:end].strip()


def strip_comment(line: str) -> str:
    """Remove a trailing TeX comment, keeping escaped percent signs."""
    i = 0
    while True:
        j = line.find("%", i)
        if j < 0:
            return line
        back = j - 1
        n = 0
        while back >= 0 and line[back] == "\\":
            n += 1
            back -= 1
        if n % 2 == 0:
            return line[:j]
        i = j + 1


def resolve_input(root_dir: str, name: str) -> str | None:
    name = name.strip()
    for cand in (name, name + ".tex"):
        p = os.path.join(root_dir, cand)
        if os.path.isfile(p):
            return p
    return None


def flatten(root: str) -> Flat:
    root_dir = os.path.dirname(os.path.abspath(root))
    parts: list[str] = []
    lines: list[tuple[int, str, int]] = []
    files: list[str] = []
    preamble_files: list[str] = []
    state = {"offset": 0, "in_body": False, "body_start": -1, "skip": None}
    seen: set[str] = set()

    def rel(p: str) -> str:
        return os.path.relpath(p, root_dir).replace(os.sep, "/")

    def visit(path: str) -> None:
        key = os.path.abspath(path)
        if key in seen:
            return
        seen.add(key)
        name = rel(path)
        files.append(name)
        if not state["in_body"] and os.path.abspath(path) != os.path.abspath(root):
            preamble_files.append(name)
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.read().split("\n")
        for lineno, raw in enumerate(raw_lines, 1):
            line = strip_comment(raw)
            # Skip-environment bodies (comment, verbatim, ...) contribute nothing.
            if state["skip"]:
                if re.search(r"\\end\s*\{" + re.escape(state["skip"]) + r"\}", line):
                    state["skip"] = None
                emit("", name, lineno)
                continue
            m = re.search(r"\\begin\s*\{(" + "|".join(map(re.escape, SKIP_ENVS)) + r")\}", line)
            if m and not re.search(r"\\end\s*\{" + re.escape(m.group(1)) + r"\}", line):
                state["skip"] = m.group(1)
                emit(line[: m.start()], name, lineno)
                continue
            if not state["in_body"] and re.search(r"\\begin\s*\{document\}", line):
                state["in_body"] = True
                state["body_start"] = state["offset"]
            # Expand inputs in place, keeping surrounding text of the line.
            pos = 0
            for im in INPUT.finditer(line):
                emit(line[pos: im.start()], name, lineno, newline=False)
                target = resolve_input(root_dir, im.group(1))
                if target:
                    emit("\n", name, lineno, newline=False)
                    visit(target)
                else:
                    sys.stderr.write(f"warning: cannot resolve \\input{{{im.group(1)}}} at {name}:{lineno}\n")
                pos = im.end()
            emit(line[pos:], name, lineno)

    def emit(text: str, name: str, lineno: int, newline: bool = True) -> None:
        if not lines or lines[-1][1:] != (name, lineno) or parts and parts[-1].endswith("\n"):
            lines.append((state["offset"], name, lineno))
        chunk = text + ("\n" if newline else "")
        parts.append(chunk)
        state["offset"] += len(chunk)

    visit(os.path.abspath(root))
    text = "".join(parts)
    body_start = state["body_start"] if state["body_start"] >= 0 else 0
    return Flat(text, lines, files, preamble_files, body_start)

# ---------------------------------------------------------------------- parsing


def brace_group_end(text: str, i: int) -> int:
    """Given text[i] == '{', return index just after the matching '}'."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i


@dataclass
class Env:
    name: str
    start: int  # offset of \begin
    body: int  # offset just after \begin{..}[title]
    end: int  # offset of matching \end (or len(text))
    title: str | None


@dataclass
class Use:
    order: int
    via: str
    file: str
    line: int
    context: str
    relation: str = ""


@dataclass
class Item:
    id: str
    kind: str
    order: int
    file: str | None
    line: int | None
    title: str | None = None
    labels: list[str] = field(default_factory=list)
    end_order: int | None = None
    section: str | None = None
    introduced: dict | None = None
    uses: list[Use] = field(default_factory=list)


def parse_envs(text: str) -> list[Env]:
    stack: list[Env] = []
    envs: list[Env] = []
    tokens = sorted(
        [(m.start(), "b", m) for m in BEGIN.finditer(text)]
        + [(m.start(), "e", m) for m in END.finditer(text)],
        key=lambda t: t[0],
    )
    for pos, kind, m in tokens:
        name = m.group(1).strip()
        if kind == "b":
            j = skip_ws(text, m.end())
            title = None
            if j < len(text) and text[j] == "[":
                # Optional argument, brace-aware.
                depth = 0
                k = j
                while k < len(text):
                    c = text[k]
                    if c == "\\":
                        k += 2
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                    elif c == "]" and depth == 0:
                        break
                    k += 1
                title = text[j + 1: k].strip()
                j = k + 1
            env = Env(name, pos, j, len(text), title)
            stack.append(env)
            envs.append(env)
        else:
            for k in range(len(stack) - 1, -1, -1):
                if stack[k].name == name:
                    stack[k].end = pos
                    del stack[k:]
                    break
    envs.sort(key=lambda e: e.start)
    return envs


def enclosing(envs: list[Env], offset: int, names: set[str]) -> Env | None:
    best = None
    for e in envs:
        if e.start <= offset < e.end and e.name in names:
            if best is None or e.start > best.start:
                best = e
    return best


def macro_definition_spans(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, command, macro-name) for every macro-binding command."""
    spans = []
    for m in MACRO_DEF.finditer(text):
        name = m.group(2) or m.group(3)
        cmd = m.group(1)
        i = skip_ws(text, m.end())
        if cmd == "let":
            j = skip_ws(text, i)
            if j < len(text) and text[j] == "=":
                j = skip_ws(text, j + 1)
            cm = CONTROL_WORD.match(text, j)
            end = cm.end() if cm else j
            spans.append((m.start(), end, cmd, name))
            continue
        if cmd in ("def", "gdef", "edef"):
            # Parameter text up to the first brace.
            while i < len(text) and text[i] != "{" and text[i] != "\n":
                i += 1
        else:
            # Optional [nargs][default] / xparse arg spec, then body group(s).
            while i < len(text) and text[i] == "[":
                depth = 0
                while i < len(text):
                    if text[i] == "[":
                        depth += 1
                    elif text[i] == "]":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
                i = skip_ws(text, i)
            if cmd in ("NewDocumentCommand", "RenewDocumentCommand", "ProvideDocumentCommand") and i < len(text) and text[i] == "{":
                i = skip_ws(text, brace_group_end(text, i))  # arg spec
        end = i
        groups = 2 if cmd in ("newenvironment", "renewenvironment", "DeclarePairedDelimiter") else 1
        for _ in range(groups):
            end = skip_ws(text, end)
            if end < len(text) and text[end] == "{":
                end = brace_group_end(text, end)
        spans.append((m.start(), end, cmd, name))
    return spans


def in_spans(spans: list[tuple[int, int]], offset: int) -> bool:
    for s, e in spans:
        if s <= offset < e:
            return True
    return False


def title_pattern(title: str) -> re.Pattern | None:
    """Case-insensitive phrase match for a result title; single-word titles are too ambiguous."""
    words = [w for w in re.split(r"\s+", title.strip()) if w]
    if len(words) < 2:
        return None
    return re.compile(r"(?<![A-Za-z\\])" + r"\s+".join(re.escape(w) for w in words) + r"(?![A-Za-z])", re.I)


def rule_pattern(name: str) -> re.Pattern:
    """Rule names that look like English words (`Frame`) count only when set in small caps."""
    distinctive = re.search(r"[-0-9]|.[A-Z]", name) is not None
    word = r"(?<![A-Za-z0-9-])" + re.escape(name) + r"(?![A-Za-z0-9-])"
    if distinctive:
        return re.compile(word)
    return re.compile(r"\\(?:textsc|mkw|rulename|ruleref)\s*\{\s*" + re.escape(name) + r"\s*\}")

# ------------------------------------------------------------------------- scan


def scan(args: argparse.Namespace) -> int:
    flat = flatten(args.root)
    text = flat.text
    body_start = flat.body_start
    envs = parse_envs(text)
    theorem_envs = set(THEOREM_ENVS) | {m.group(1).strip() for m in NEWTHEOREM.finditer(text)}
    defspans_full = macro_definition_spans(text)
    excluded = [(s, e) for s, e, _, _ in defspans_full]

    def loc(offset: int) -> tuple[str, int]:
        return flat.locate(offset)

    def mk_use(offset: int, via: str) -> Use:
        f, n = loc(offset)
        return Use(offset, via, f, n, flat.line_text(offset)[:160])

    # Section tracking: nearest preceding sectioning command's title or label.
    section_marks: list[tuple[int, str]] = []
    for m in SECTIONING.finditer(text):
        if m.start() < body_start:
            continue
        end = brace_group_end(text, m.end() - 1)
        title = re.sub(r"\s+", " ", text[m.end(): end - 1]).strip()
        lm = LABEL.match(text, skip_ws(text, end))
        section_marks.append((m.start(), lm.group(1) if lm else title))

    def section_of(offset: int) -> str | None:
        cur = None
        for pos, name in section_marks:
            if pos <= offset:
                cur = name
            else:
                break
        return cur

    items: list[Item] = []
    by_label: dict[str, Item] = {}

    # 1. Theorem-like environments.
    for e in envs:
        if e.name not in theorem_envs or e.start < body_start:
            continue
        f, n = loc(e.start)
        labels = [m.group(1).strip() for m in LABEL.finditer(text, e.body, e.end)
                  if enclosing(envs, m.start(), theorem_envs) is e]
        ident = labels[0] if labels else f"unlabeled:{e.name}:{f}:{n}"
        item = Item(ident, e.name, e.start, f, n, e.title, labels, e.end, section_of(e.start))
        items.append(item)
        for lb in labels:
            by_label.setdefault(lb, item)

    # 2. Other labels: equations, floats, sections, misc.
    for m in LABEL.finditer(text):
        lb = m.group(1).strip()
        if lb in by_label or m.start() < body_start:
            continue
        off = m.start()
        if enclosing(envs, off, MATH_ENVS):
            kind = "equation"
        elif (fl := enclosing(envs, off, FLOAT_ENVS)):
            kind = "table" if fl.name.startswith("table") else "figure"
        else:
            kind = "other"
            for pos, _ in section_marks:
                if 0 <= off - pos < 400 and "\\begin" not in text[pos:off]:
                    kind = "section"
        f, n = loc(off)
        existing = next((it for it in items if it.kind == kind and it.order == off), None)
        if existing:
            existing.labels.append(lb)
            by_label[lb] = existing
            continue
        item = Item(lb, kind, off, f, n, None, [lb], None, section_of(off))
        items.append(item)
        by_label[lb] = item

    # 3. Inference rule names.
    rule_items: dict[str, Item] = {}
    rule_spans: list[tuple[int, int]] = []
    for m in INFERRULE.finditer(text):
        if m.start() < body_start:
            continue
        name = re.sub(r"\\textsc\s*\{([^}]*)\}", r"\1", m.group(1)).strip()
        rule_spans.append((m.start(), m.end()))
        if name in rule_items:
            rule_items[name].uses.append(mk_use(m.start(), "rule-redefinition"))
            continue
        f, n = loc(m.start())
        item = Item(f"rule:{name}", "rule", m.start(), f, n, name, [], m.end(), section_of(m.start()))
        rule_items[name] = item
        items.append(item)

    # 4. Notation macros.
    notation_files = set(args.notation) if args.notation else set(flat.preamble_files)
    ignore = re.compile(args.ignore_macro) if args.ignore_macro else None
    notation: dict[str, Item] = {}
    for s, e, cmd, name in defspans_full:
        f, n = loc(s)
        if f not in notation_files or (ignore and ignore.search(name)):
            continue
        if name in notation:
            continue
        notation[name] = Item(f"\\{name}", "macro", s, f, n, None, [], e, None)
    items.extend(notation.values())

    # ---- uses
    # 4a. \ref-family.
    undefined: dict[str, Item] = {}
    for m in REF.finditer(text):
        if m.start() < body_start or in_spans(excluded, m.start()):
            continue
        via = text[m.start() + 1: text.index("{", m.start()) if m.group(1) is not None else text.index("[", m.start())]
        for lb in (m.group(1) or m.group(2)).split(","):
            lb = lb.strip()
            if not lb:
                continue
            target = by_label.get(lb)
            if target is None:
                target = undefined.get(lb)
                if target is None:
                    target = Item(lb, "undefined-label", m.start(), None, None)
                    undefined[lb] = target
            target.uses.append(mk_use(m.start(), via))
    items.extend(undefined.values())

    # 4b. Mentions of theorem titles by name, outside their own environment.
    for it in items:
        if it.kind not in theorem_envs or not it.title:
            continue
        pat = title_pattern(it.title)
        if pat is None:
            continue
        for m in pat.finditer(text, body_start):
            if it.order <= m.start() < (it.end_order or it.order):
                continue
            if in_spans(excluded, m.start()):
                continue
            it.uses.append(mk_use(m.start(), "mention"))

    # 4c. Rule-name mentions.
    for name, it in rule_items.items():
        pat = rule_pattern(name)
        for m in pat.finditer(text, body_start):
            if in_spans(rule_spans, m.start()) or in_spans(excluded, m.start()):
                continue
            it.uses.append(mk_use(m.start(), "mention"))

    # 4d. Notation macro uses in the body, outside macro definitions.
    if notation:
        for m in CONTROL_WORD.finditer(text, body_start):
            it = notation.get(m.group(1))
            if it is None or in_spans(excluded, m.start()):
                continue
            off = m.start()
            use = mk_use(off, "macro")
            it.uses.append(use)
            if it.introduced is None:
                how = None
                if enclosing(envs, off, DEFINING_ENVS):
                    how = "definition-environment"
                else:
                    line = flat.line_text(off)
                    f, n = loc(off)
                    rest = line[line.find("\\" + m.group(1)):]
                    if DEFINITIONAL_OPERATORS.search(rest):
                        how = "definitional-equation"
                    else:
                        # Preceding two lines of prose.
                        idx = next(i for i, (_, ff, nn) in enumerate(flat.lines) if ff == f and nn == n)
                        prev_start = flat.lines[max(0, idx - 2)][0]
                        if DEFINING_WORDS.search(text[prev_start: off]):
                            how = "defining-prose"
                if how:
                    it.introduced = {"order": off, "file": use.file, "line": use.line, "via": how, "context": use.context}

    # ---- finalize
    for it in items:
        it.uses.sort(key=lambda u: (u.order, u.via))
        for u in it.uses:
            if it.kind == "macro":
                ref = it.introduced["order"] if it.introduced else None
                u.relation = "unintroduced" if ref is None else ("before" if u.order < ref else ("inside" if u.order == ref else "after"))
            elif it.kind == "undefined-label":
                u.relation = "undefined"
            elif it.order <= u.order < (it.end_order if it.end_order is not None else it.order + 1):
                u.relation = "inside"
            else:
                u.relation = "before" if u.order < it.order else "after"
    items.sort(key=lambda it: (it.order, it.id))

    out = args.output or "flow.jsonl"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for it in items:
            counts = {"before": 0, "after": 0, "inside": 0}
            for u in it.uses:
                counts[u.relation] = counts.get(u.relation, 0) + 1
            first_after = next((u for u in it.uses if u.relation == "after"), None)
            rec = {
                "id": it.id,
                "kind": it.kind,
                "title": it.title,
                "labels": it.labels,
                "file": it.file,
                "line": it.line,
                "order": it.order,
                "section": it.section,
                "introduced": it.introduced,
                "uses": [u.__dict__ for u in it.uses],
                "counts": counts,
                "first_use_after": first_after.__dict__ if first_after else None,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    kinds: dict[str, int] = {}
    for it in items:
        kinds[it.kind] = kinds.get(it.kind, 0) + 1
    print(f"{out}: {len(items)} items from {len(flat.files)} files; "
          + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    if not args.notation:
        print(f"notation files (default = preamble inputs): {', '.join(flat.preamble_files) or 'none'}")
    return 0

# ----------------------------------------------------------------------- issues


def issues(args: argparse.Namespace) -> int:
    with open(args.flow, encoding="utf-8") as fh:
        items = [json.loads(line) for line in fh if line.strip()]
    special = {"equation", "figure", "table", "section", "other", "rule", "macro", "undefined-label"}
    theorem_kinds = {it["kind"] for it in items} - special
    found: list[dict] = []

    def add(sev: str, code: str, it: dict, message: str, hint: str, evidence: list[dict] | None = None) -> None:
        found.append({
            "severity": sev,
            "code": code,
            "id": it["id"],
            "kind": it["kind"],
            "title": it.get("title"),
            "file": it.get("file"),
            "line": it.get("line"),
            "order": it.get("order"),
            "section": it.get("section"),
            "message": message,
            "hint": hint,
            "evidence": dedupe_evidence(evidence or []),
        })

    def dedupe_evidence(uses: list[dict]) -> list[dict]:
        seen: set[tuple] = set()
        out = []
        for u in uses:
            key = (u["file"], u["line"], u["via"])
            if key in seen:
                continue
            seen.add(key)
            out.append({k: u[k] for k in ("file", "line", "via", "relation", "context")})
        return out[:8]

    for it in items:
        kind, c = it["kind"], it["counts"]
        uses = it["uses"]
        outside = [u for u in uses if u["relation"] in ("before", "after")]
        name = it.get("title") or it["id"]
        if kind == "undefined-label":
            add("error", "undefined-label", it,
                f"`{it['id']}` is referenced {len(uses)} time(s) but no \\label defines it.",
                "Add the label at the defining statement, or fix the reference key.", uses)
        elif kind in theorem_kinds:
            if not it["labels"]:
                add("info", "unlabeled-result", it,
                    f"{kind} “{name}” has no \\label; it can only be found by name mentions ({len(outside)} found).",
                    "Add a label so later uses can reference it unambiguously.")
            if not outside:
                hint = ("Possibly a top-level result: confirm it is the paper's final statement in its line of argument."
                        if kind == "theorem" else
                        "Either a standalone result worth keeping (then say so where it appears) or dead weight: "
                        "cite it from where it is needed, move it to an appendix, or remove it.")
                add("warning", "unused-result", it,
                    f"{kind} “{name}” is never referenced or mentioned again after its statement.", hint)
            else:
                before = [u for u in outside if u["relation"] == "before"]
                after = [u for u in outside if u["relation"] == "after"]
                if before and not after:
                    add("warning", "forward-reference-only", it,
                        f"{kind} “{name}” is referenced only before it is stated ({len(before)} time(s)).",
                        "Every use precedes the statement: move the statement earlier or add the downstream use.", before)
                elif before:
                    add("warning", "used-before-defined", it,
                        f"{kind} “{name}” is referenced {len(before)} time(s) before it is stated "
                        f"(first at {before[0]['file']}:{before[0]['line']}).",
                        "Acceptable for roadmap text; otherwise reorder so the statement precedes its use.", before)
        elif kind == "macro":
            if not uses:
                add("warning", "unused-notation", it,
                    f"macro `{it['id']}` is defined but never used in the document body.",
                    "Remove the macro, or the notation it stands for was dropped from the text.")
            else:
                intro = it.get("introduced")
                before = [u for u in uses if u["relation"] == "before"]
                if intro is None:
                    add("info", "notation-never-introduced", it,
                        f"macro `{it['id']}` is used {len(uses)} time(s) but no use looks like a definition "
                        "(definition environment, definitional equation, or defining prose).",
                        "If this is notation, introduce it where it first appears; if it is a name or formatting macro, ignore.",
                        uses[:3])
                elif before:
                    add("warning", "notation-used-before-introduction", it,
                        f"macro `{it['id']}` is used {len(before)} time(s) before its apparent introduction at "
                        f"{intro['file']}:{intro['line']} ({intro['via']}).",
                        "Introduce the notation earlier, or confirm the detected introduction point.", before)
        elif kind == "rule":
            after = [u for u in uses if u["relation"] == "after" and u["via"] == "mention"]
            before = [u for u in uses if u["relation"] == "before" and u["via"] == "mention"]
            if not after and not before:
                add("info", "unused-rule", it,
                    f"rule {name} is never mentioned outside its own inference rule.",
                    "Rules shown in a figure need not be cited, but check that the text explains it.")
            elif before and not after:
                add("info", "rule-used-before-defined", it,
                    f"rule {name} is mentioned only before it is presented ({len(before)} time(s)).",
                    "Reorder or mention the rule after its presentation.", before)
        elif kind == "equation" and not outside:
            add("info", "unreferenced-equation", it,
                f"equation label `{it['id']}` is never referenced.",
                "Drop the label or reference the equation.")

    found.sort(key=lambda r: (SEVERITY_RANK[r["severity"]], r["order"] if r["order"] is not None else -1, r["id"]))
    out = args.output or "flow-issues.jsonl"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for rec in found:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tally: dict[str, int] = {}
    for rec in found:
        tally[rec["code"]] = tally.get(rec["code"], 0) + 1
    print(f"{out}: {len(found)} issues; " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 2 if any(r["severity"] == "error" for r in found) else (1 if found else 0)

# ------------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="flatten the paper and record items and uses")
    s.add_argument("root", help="root .tex file")
    s.add_argument("-o", "--output", help="output JSONL (default flow.jsonl)")
    s.add_argument("--notation", action="append", metavar="FILE",
                   help="file (relative to root) whose macro definitions count as notation; repeatable; "
                        "default: every file \\input before \\begin{document}")
    s.add_argument("--ignore-macro", metavar="REGEX", help="skip notation macros whose name matches")
    s.set_defaults(func=scan)
    i = sub.add_parser("issues", help="derive flow-issues.jsonl from flow.jsonl")
    i.add_argument("flow", help="flow.jsonl from `scan`")
    i.add_argument("-o", "--output", help="output JSONL (default flow-issues.jsonl)")
    i.set_defaults(func=issues)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
