"""The GraphQL operations a consumer sends: find them, validate them, repair them.

Operations are read from ``.graphql``/``.gql`` files and from code:
``gql`...```/``graphql`...``` tagged templates, ``gql(`...`)``/``graphql(`...`)``
calls and ``/* GraphQL */`` templates in JS/TS, and ``gql('''...''')`` in Python.

Validation reports what a server would reject (unknown fields, arguments,
types, fragments, enum values; missing required arguments; wrong selections;
incompatible variables) plus use of deprecated schema members.

Renames found by :func:`apimend.gql.diff.diff_graphql` are repaired with
aliases, so the response keeps its shape and calling code needs no change::

    user { email }   ->   user { email: emailAddress }
"""

from __future__ import annotations

import difflib
import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .diff import GraphQLDiff, safe_for_output, type_tree
from .model import named
from .parser import (
    ExecutableDocument, FieldNode, FragmentNode, FragmentSpread, GraphQLSyntaxError, InlineFragment,
    OperationNode, document_kind, parse_operations,
)

GRAPHQL_EXTS = {".graphql", ".gql"}
CODE_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".py"}
INPUT_KINDS = {"SCALAR", "ENUM", "INPUT_OBJECT"}
COMPOSITE_KINDS = {"OBJECT", "INTERFACE", "UNION"}

_JS_START = re.compile(r"(?:\b(?:gql|graphql)\s*(?:<[^>`]*>)?\s*\(?\s*|/\*\s*GraphQL\s*\*/\s*)`")
_PY_START = re.compile(r"\bgql\(\s*[rR]?(\"\"\"|'''|\"|')")


# ------------------------------------------------------------------ extraction


@dataclass
class Snippet:
    file: str
    text: str  # same length as the source span; interpolations are blanked out
    offset: int  # where the span starts in the file


def _js_template_end(src: str, i: int) -> tuple[int, list[tuple[int, int]]]:
    """From just after an opening backtick, find the closing one; return (end, interpolation spans)."""
    holes = []
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i, holes
        if src.startswith("${", i):
            start, depth = i, 0
            i += 2
            while i < n:
                if src[i] == "{":
                    depth += 1
                elif src[i] == "}":
                    if depth == 0:
                        break
                    depth -= 1
                elif src[i] in "'\"`":  # skip simple strings inside the interpolation
                    q = src[i]
                    i += 1
                    while i < n and src[i] != q:
                        i += 2 if src[i] == "\\" else 1
                i += 1
            holes.append((start, i + 1))
            i += 1
            continue
        i += 1
    return -1, holes


PLACEHOLDER_CHAR = "_"
_NAME_BEFORE = re.compile(r"(\.\.\.|\bon|\bfragment|\bquery|\bmutation|\bsubscription)\s*$")


def _needs_name(text: str, start: int, hole: int) -> bool:
    """Does the interpolation at ``hole`` stand for a name (`...${F}`, `on ${T}`, a field) or for whole text?"""
    before = text[start:hole]
    if _NAME_BEFORE.search(before):
        return True
    depth = 0
    for c in before:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
    return depth > 0


def is_placeholder(name: str) -> bool:
    """Names made of underscores stand for interpolated text; they are not checked."""
    return len(name) >= 3 and set(name) == {PLACEHOLDER_CHAR}


def extract_snippets(path: str, text: str) -> list[Snippet]:
    ext = os.path.splitext(path)[1].lower()
    if ext in GRAPHQL_EXTS:
        return [Snippet(path, text, 0)]
    out: list[Snippet] = []
    if ext == ".py":
        for m in _PY_START.finditer(text):
            q = m.group(1)
            start = m.end()
            end = text.find(q, start)
            while end > 0 and len(q) == 1 and text[end - 1] == "\\":
                end = text.find(q, end + 1)
            if end > 0:
                out.append(Snippet(path, text[start:end], start))
    elif ext in CODE_EXTS:
        for m in _JS_START.finditer(text):
            start = m.end()
            end, holes = _js_template_end(text, start)
            if end < 0:
                continue
            chars = list(text[start:end])
            for a, b in holes:
                fill = PLACEHOLDER_CHAR if _needs_name(text, start, a) else " "
                for k in range(a - start, b - start):
                    chars[k] = fill
            out.append(Snippet(path, "".join(chars), start))
    return out


@dataclass
class SourceDoc:
    file: str
    text: str  # whole file
    document: ExecutableDocument


@dataclass
class OperationSet:
    docs: list[SourceDoc] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # files that could not be parsed

    @property
    def operations(self) -> list[tuple[SourceDoc, OperationNode]]:
        return [(d, op) for d in self.docs for op in d.document.operations]

    @property
    def fragments(self) -> dict[str, tuple[SourceDoc, FragmentNode]]:
        out = {}
        for d in self.docs:
            for fr in d.document.fragments:
                out.setdefault(fr.name, (d, fr))
        return out


def _glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + "$")


def expand_patterns(patterns: list[str], base: Path) -> list[str]:
    """Files matching the globs, minus any matching a ``!glob`` (both relative to ``base``)."""
    files: list[str] = []
    excludes = [_glob_regex(p[1:]) for p in patterns if p.startswith("!")]
    for p in patterns:
        if p.startswith("!"):
            continue
        pattern = p if os.path.isabs(p) else str(base / p)
        for f in sorted(glob.glob(pattern, recursive=True)):
            if not os.path.isfile(f) or f in files or "node_modules" in Path(f).parts:
                continue
            rel = os.path.relpath(f, base).replace(os.sep, "/")
            if any(x.match(rel) for x in excludes):
                continue
            files.append(f)
    return files


def load_operations(files: list[str], overrides: dict[str, str] | None = None,
                    root: Path | None = None) -> OperationSet:
    """Parse every operation in ``files``. ``overrides`` maps a file to replacement text."""
    out = OperationSet()

    def show(f: str) -> str:
        return os.path.relpath(f, root) if root else f

    for f in files:
        if overrides and f in overrides:
            text = overrides[f]
        else:
            try:
                text = Path(f).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                out.errors.append(f"{show(f)}: {e}")
                continue
        for snip in extract_snippets(f, text):
            if document_kind(snip.text) != "operations":
                continue  # schema definitions (e.g. server typeDefs) or not GraphQL
            try:
                doc = parse_operations(snip.text, snip.offset)
            except GraphQLSyntaxError as e:
                line = text.count("\n", 0, snip.offset + e.offset) + 1
                out.errors.append(f"{show(f)}:{line}: {str(e).split(' (line')[0]}")
                continue
            out.docs.append(SourceDoc(f, text, doc))
    return out


# ------------------------------------------------------------------ validation


@dataclass
class Issue:
    kind: str
    severity: str  # "error" | "deprecated"
    message: str
    file: str
    offset: int
    owner: str  # "query GetUser" or "fragment UserFields"
    coordinate: str = ""  # schema coordinate involved, e.g. "User.email"
    type: str = ""  # parent type
    name: str = ""  # field or argument name
    has_alias: bool = False

    def where(self, text: str) -> str:
        return f"{self.file}:{text.count(chr(10), 0, self.offset) + 1}"


@dataclass
class Usage:
    coordinates: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    fragments: set[str] = field(default_factory=set)


def _suggest(name: str, candidates: list[str]) -> str:
    from ..diff import name_similarity

    scored = sorted(((name_similarity(name, c), c) for c in candidates), reverse=True)
    if scored and scored[0][0] >= 0.6:
        return f" Did you mean `{scored[0][1]}`?"
    close = difflib.get_close_matches(name, candidates, n=1, cutoff=0.75)
    return f" Did you mean `{close[0]}`?" if close else ""


def _label(node) -> str:
    if isinstance(node, FragmentNode):
        return f"fragment {node.name}"
    return f"{node.kind} {node.name}" if node.name else f"anonymous {node.kind}"


def _var_compatible(var: tuple, loc: tuple, var_has_default: bool, loc_has_default: bool) -> bool:
    """The spec's 'AreTypesCompatible', with the allowance for defaults."""
    if loc[0] == "nonnull" and var[0] != "nonnull":
        if not (var_has_default or loc_has_default):
            return False
        loc = loc[1]
    return _subtype(var, loc)


def _subtype(var: tuple, loc: tuple) -> bool:
    if loc[0] == "nonnull":
        return var[0] == "nonnull" and _subtype(var[1], loc[1])
    if var[0] == "nonnull":
        return _subtype(var[1], loc)
    if loc[0] == "list":
        return var[0] == "list" and _subtype(var[1], loc[1])
    if var[0] == "list":
        return False
    return var[1] == loc[1]


class Validator:
    def __init__(self, model: dict, opset: OperationSet):
        self.types = model["types"]
        self.roots = model["roots"]
        self.opset = opset
        self.fragments = opset.fragments
        self.issues: list[Issue] = []
        self._fragment_usage: dict[str, Usage] = {}

    # -- helpers
    def field_def(self, tname: str, fname: str) -> dict | None:
        t = self.types.get(tname)
        if not t:
            return None
        return t.get("fields", {}).get(fname)

    def issue(self, kind, message, doc: SourceDoc, offset, owner, severity="error", **kw) -> None:
        self.issues.append(Issue(kind, severity, message, doc.file, offset, owner, **kw))

    def suggest(self, tname: str, fname: str) -> str:
        return _suggest(fname, list(self.types.get(tname, {}).get("fields", {})))

    # -- entry points
    def run(self) -> dict[str, Usage]:
        """Validate everything; return usage per operation label."""
        usage: dict[str, Usage] = {}
        for name, (doc, fr) in self.fragments.items():
            self._fragment_usage[name] = self.fragment(doc, fr)
        for doc, op in self.opset.operations:
            u = self.operation(doc, op)
            # fold in fragments, transitively
            seen, stack = set(), list(u.fragments)
            while stack:
                f = stack.pop()
                if f in seen or f not in self._fragment_usage:
                    continue
                seen.add(f)
                fu = self._fragment_usage[f]
                u.coordinates |= fu.coordinates
                u.types |= fu.types
                stack.extend(fu.fragments)
            label = _label(op)
            if label in usage:
                usage[label].coordinates |= u.coordinates
                usage[label].types |= u.types
            else:
                usage[label] = u
        return usage

    def operation(self, doc: SourceDoc, op: OperationNode) -> Usage:
        owner = _label(op)
        u = Usage()
        root = self.roots.get(op.kind)
        if not root or root not in self.types:
            self.issue("unsupported-operation", f"{owner}: the schema has no {op.kind} type", doc, op.start, owner)
            return u
        variables = {}
        for v in op.variables:
            tn = named(v.type)
            u.types.add(tn)
            t = self.types.get(tn)
            if t is None:
                self.issue("unknown-type", f"{owner}: variable `${v.name}` has unknown type `{tn}`",
                           doc, v.start, owner, coordinate=tn, type=tn)
            elif t["kind"] not in INPUT_KINDS:
                self.issue("bad-variable-type", f"{owner}: variable `${v.name}` uses `{tn}`, which is not an input type",
                           doc, v.start, owner, coordinate=tn, type=tn)
            variables[v.name] = v
        u.types.add(root)
        self.selections(doc, owner, root, op.selections, u, variables)
        return u

    def fragment(self, doc: SourceDoc, fr: FragmentNode) -> Usage:
        owner = _label(fr)
        u = Usage(types={fr.type_condition})
        if is_placeholder(fr.type_condition):
            return u
        t = self.types.get(fr.type_condition)
        if t is None:
            self.issue("unknown-type", f"{owner}: unknown type `{fr.type_condition}`", doc, fr.start, owner,
                       coordinate=fr.type_condition, type=fr.type_condition)
            return u
        if t["kind"] not in COMPOSITE_KINDS:
            self.issue("bad-fragment-type", f"{owner}: `{fr.type_condition}` is not an object, interface or union",
                       doc, fr.start, owner, coordinate=fr.type_condition)
            return u
        self.selections(doc, owner, fr.type_condition, fr.selections, u, None)
        return u

    def selections(self, doc, owner, tname, sels, u: Usage, variables) -> None:
        t = self.types[tname]
        for sel in sels:
            if isinstance(sel, FragmentSpread):
                if is_placeholder(sel.name):
                    continue
                if sel.name not in self.fragments:
                    self.issue("unknown-fragment", f"{owner}: unknown fragment `{sel.name}`", doc, sel.start, owner)
                u.fragments.add(sel.name)
            elif isinstance(sel, InlineFragment):
                if sel.type_condition and is_placeholder(sel.type_condition):
                    continue
                cond = sel.type_condition or tname
                ct = self.types.get(cond)
                if ct is None:
                    self.issue("unknown-type", f"{owner}: unknown type `{cond}` in inline fragment", doc, sel.start,
                               owner, coordinate=cond, type=cond)
                    continue
                if ct["kind"] not in COMPOSITE_KINDS:
                    self.issue("bad-fragment-type", f"{owner}: `{cond}` is not an object, interface or union",
                               doc, sel.start, owner, coordinate=cond)
                    continue
                u.types.add(cond)
                self.selections(doc, owner, cond, sel.selections, u, variables)
            else:
                self.field(doc, owner, tname, t, sel, u, variables)

    def field(self, doc, owner, tname, t, node: FieldNode, u: Usage, variables) -> None:
        if node.name == "__typename" or is_placeholder(node.name):
            return
        if node.name in ("__schema", "__type") and tname == self.roots.get("query"):
            return
        coord = f"{tname}.{node.name}"
        fdef = t.get("fields", {}).get(node.name) if t["kind"] != "UNION" else None
        if fdef is None:
            if t["kind"] == "UNION":
                msg = f"{owner}: cannot query `{node.name}` directly on union `{tname}`; use `... on Type`"
            else:
                msg = f"{owner}: cannot query field `{node.name}` on type `{tname}`.{self.suggest(tname, node.name)}"
            self.issue("unknown-field", msg, doc, node.start, owner, coordinate=coord, type=tname,
                       name=node.name, has_alias=node.alias is not None)
            return
        u.coordinates.add(coord)
        if fdef.get("deprecated"):
            self.issue("deprecated-field", f"{owner}: `{coord}` is deprecated: {fdef['deprecated']}",
                       doc, node.start, owner, severity="deprecated", coordinate=coord, type=tname, name=node.name)
        args = fdef.get("args", {})
        given = set()
        for a in node.args:
            given.add(a.name)
            acoord = f"{coord}({a.name}:)"
            adef = args.get(a.name)
            if adef is None:
                hint = _suggest(a.name, list(args))
                self.issue("unknown-argument", f"{owner}: unknown argument `{a.name}` on `{coord}`.{hint}",
                           doc, a.start, owner, coordinate=acoord, type=tname, name=a.name)
                continue
            u.coordinates.add(acoord)
            u.types.add(named(adef["type"]))
            if adef.get("deprecated"):
                self.issue("deprecated-argument", f"{owner}: argument `{acoord}` is deprecated: {adef['deprecated']}",
                           doc, a.start, owner, severity="deprecated", coordinate=acoord)
            self.value(doc, owner, a.value, adef["type"], adef.get("default") is not None, acoord, a.value_start,
                       u, variables)
        for aname, adef in args.items():
            if aname not in given and adef["type"].endswith("!") and adef.get("default") is None:
                self.issue("missing-argument", f"{owner}: `{coord}` requires argument `{aname}`",
                           doc, node.start, owner, coordinate=f"{coord}({aname}:)", type=tname, name=aname)
        ftype = named(fdef["type"])
        u.types.add(ftype)
        kind = self.types.get(ftype, {}).get("kind")
        if kind in COMPOSITE_KINDS:
            if node.selections is None:
                self.issue("missing-selection", f"{owner}: `{coord}` returns `{ftype}` and needs a selection of fields",
                           doc, node.start, owner, coordinate=coord)
            else:
                self.selections(doc, owner, ftype, node.selections, u, variables)
        elif node.selections is not None and kind is not None:
            self.issue("leaf-selection", f"{owner}: `{coord}` returns `{ftype}`, which has no fields to select",
                       doc, node.start, owner, coordinate=coord)

    def value(self, doc, owner, v: tuple, type_str: str, loc_default: bool, coord: str, offset: int,
              u: Usage, variables) -> None:
        kind = v[0]
        if kind == "var":
            if variables is None:
                return  # inside a fragment: the operation decides the variable's type
            vd = variables.get(v[1])
            if vd is None:
                self.issue("unknown-variable", f"{owner}: variable `${v[1]}` is not defined", doc, offset, owner)
            elif named(vd.type) in self.types and not _var_compatible(
                    type_tree(vd.type), type_tree(type_str), vd.default is not None, loc_default):
                self.issue("variable-type-mismatch",
                           f"{owner}: variable `${v[1]}` is `{vd.type}` but `{coord}` expects `{type_str}`",
                           doc, offset, owner, coordinate=coord)
            return
        tree = type_tree(type_str)
        if kind == "null":
            if tree[0] == "nonnull":
                self.issue("null-value", f"{owner}: `{coord}` does not accept null", doc, offset, owner, coordinate=coord)
            return
        if tree[0] == "nonnull":
            tree = tree[1]
            type_str = type_str[:-1]
        if tree[0] == "list":
            inner = type_str[1:-1]
            items = v[1] if kind == "list" else [v]
            for item in items:
                self.value(doc, owner, item, inner, False, coord, offset, u, variables)
            return
        tname = tree[1]
        t = self.types.get(tname)
        if t is None:
            return
        if t["kind"] == "ENUM":
            if kind == "enum" and not is_placeholder(v[1]):
                if v[1] not in t.get("values", {}):
                    self.issue("unknown-enum-value", f"{owner}: `{tname}` has no value `{v[1]}` (in `{coord}`)",
                               doc, offset, owner, coordinate=f"{tname}.{v[1]}")
                else:
                    u.coordinates.add(f"{tname}.{v[1]}")
                    dep = t["values"][v[1]].get("deprecated")
                    if dep:
                        self.issue("deprecated-enum-value", f"{owner}: `{tname}.{v[1]}` is deprecated: {dep}",
                                   doc, offset, owner, severity="deprecated", coordinate=f"{tname}.{v[1]}")
        elif t["kind"] == "INPUT_OBJECT" and kind == "object":
            inputs = t.get("inputs", {})
            for k, sub in v[1].items():
                fc = f"{tname}.{k}"
                idef = inputs.get(k)
                if idef is None:
                    self.issue("unknown-input-field", f"{owner}: input type `{tname}` has no field `{k}`",
                               doc, offset, owner, coordinate=fc)
                    continue
                u.coordinates.add(fc)
                u.types.add(named(idef["type"]))
                if idef.get("deprecated"):
                    self.issue("deprecated-input-field", f"{owner}: `{fc}` is deprecated: {idef['deprecated']}",
                               doc, offset, owner, severity="deprecated", coordinate=fc)
                self.value(doc, owner, sub, idef["type"], idef.get("default") is not None, fc, offset, u, variables)
            for k, idef in inputs.items():
                if k not in v[1] and idef["type"].endswith("!") and idef.get("default") is None:
                    self.issue("missing-input-field", f"{owner}: input `{tname}` requires field `{k}` (in `{coord}`)",
                               doc, offset, owner, coordinate=f"{tname}.{k}")


def validate(model: dict, opset: OperationSet) -> tuple[list[Issue], dict[str, Usage]]:
    v = Validator(model, opset)
    usage = v.run()
    v.issues.sort(key=lambda i: (i.file, i.offset, i.kind))
    return v.issues, usage


# ---------------------------------------------------------------------- repair


@dataclass
class Edit:
    file: str
    start: int
    end: int
    old: str
    new: str
    reason: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def plan_fixes(issues: list[Issue], gdiff: GraphQLDiff, texts: dict[str, str]) -> tuple[list[Edit], list[Issue]]:
    """Turn unknown-field / unknown-argument errors caused by renames into edits."""
    fields = {(r.type, r.old): r for r in gdiff.field_renames}
    args = {(r.type, r.old): r for r in gdiff.arg_renames}
    edits: list[Edit] = []
    unresolved: list[Issue] = []
    seen: set[tuple[str, int]] = set()
    for iss in issues:
        if iss.severity != "error":
            continue
        text = texts.get(iss.file, "")
        if iss.kind == "unknown-field" and (iss.type, iss.name) in fields:
            r = fields[(iss.type, iss.name)]
            if (iss.file, iss.offset) in seen:
                continue
            seen.add((iss.file, iss.offset))
            if text[iss.offset: iss.offset + len(iss.name)] != iss.name:
                unresolved.append(iss)
                continue
            # an alias keeps the response key, so code reading `email` keeps working
            replacement = r.new if iss.has_alias else f"{r.old}: {r.new}"
            reason = f"`{r.type}.{r.old}` was renamed to `{r.new}`"
            if r.old_type and r.new_type and not safe_for_output(type_tree(r.old_type), type_tree(r.new_type)):
                reason += f"; its type also changed `{r.old_type}` → `{r.new_type}`, check the code that reads it"
            edits.append(Edit(iss.file, iss.offset, iss.offset + len(iss.name), iss.name, replacement, reason))
        elif iss.kind == "unknown-argument":
            # coordinate is Type.field(arg:)
            fname = iss.coordinate.split(".", 1)[1].split("(", 1)[0]
            key = (iss.type, iss.name)
            r = args.get(key)
            if r and r.field == fname and text[iss.offset: iss.offset + len(iss.name)] == iss.name:
                if (iss.file, iss.offset) in seen:
                    continue
                seen.add((iss.file, iss.offset))
                edits.append(Edit(iss.file, iss.offset, iss.offset + len(iss.name), iss.name, r.new,
                                  f"argument `{iss.type}.{fname}({r.old}:)` was renamed to `{r.new}`"))
            else:
                unresolved.append(iss)
        else:
            unresolved.append(iss)
    return edits, unresolved


def apply_edits(text: str, edits: list[Edit]) -> str:
    """Apply edits to the text they were planned against (raises ValueError if it moved)."""
    for e in sorted(edits, key=lambda e: e.start, reverse=True):
        if text[e.start:e.end] != e.old:
            raise ValueError(f"{e.file}: expected {e.old!r} at offset {e.start}; the file changed, re-run heal")
        text = text[:e.start] + e.new + text[e.end:]
    return text


def unified_diff(texts: dict[str, str], edits: list[Edit], root: Path | None = None) -> str:
    out = []
    by_file: dict[str, list[Edit]] = {}
    for e in edits:
        by_file.setdefault(e.file, []).append(e)
    for f, es in sorted(by_file.items()):
        before = texts[f]
        after = apply_edits(before, es)
        rel = os.path.relpath(f, root) if root else f
        out.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return "".join(out)
