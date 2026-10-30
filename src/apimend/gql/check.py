"""GraphQL drift as the CLI sees it: schema changes, plus their effect on your operations."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..diff import BREAKING, INFO, WARNING, Change
from .diff import GraphQLDiff, diff_graphql
from .model import named
from .operations import (
    Edit, Issue, OperationSet, Usage, apply_edits, load_operations, plan_fixes, unified_diff, validate,
)

PLAN_VERSION = 1
# changes that concern a whole type: match them on any use of the type
TYPE_LEVEL = {"type-removed", "type-kind-changed", "union-member-removed", "union-member-added", "interface-removed",
              "interface-added", "enum-value-removed", "enum-value-added"}
# argument changes that matter even when the operation does not pass the argument
FIELD_LEVEL_ARG = {"required-argument-added", "default-changed"}


def _rel(path: str, root: Path) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _where(issue: Issue, texts: dict[str, str], root: Path) -> str:
    text = texts.get(issue.file, "")
    return f"{_rel(issue.file, root)}:{text.count(chr(10), 0, issue.offset) + 1}"


def _strip_owner(issue: Issue) -> str:
    prefix = issue.owner + ": "
    return issue.message[len(prefix):] if issue.message.startswith(prefix) else issue.message


def _input_closure(model: dict, types: set[str]) -> set[str]:
    """Input types reachable from ``types`` (a variable of type A can carry B inside it)."""
    out, stack = set(types), list(types)
    while stack:
        t = model["types"].get(stack.pop())
        if not t or t["kind"] != "INPUT_OBJECT":
            continue
        for iv in t.get("inputs", {}).values():
            n = named(iv["type"])
            if n not in out:
                out.add(n)
                stack.append(n)
    return out


def _users(change: Change, usage: dict[str, Usage]) -> list[str]:
    coord = change.path
    type_name = coord.split(".", 1)[0].split("(", 1)[0]
    field_coord = coord.split("(", 1)[0]
    users = []
    for label, u in usage.items():
        if coord in u.coordinates:
            users.append(label)
        elif change.kind in TYPE_LEVEL and type_name in u.types:
            users.append(label)
        elif change.kind in FIELD_LEVEL_ARG and "(" in coord and field_coord in u.coordinates:
            users.append(label)
        elif "(" not in coord and "." in coord and type_name in u.types and \
                change.kind.startswith(("input-field", "required-input-field")):
            users.append(label)
    return sorted(users)


@dataclass
class GraphQLCheck:
    changes: list[Change] = field(default_factory=list)
    diff: GraphQLDiff | None = None
    operations: int = 0


def check(old: dict, new: dict, files: list[str], root: Path, only_used: bool = False,
          location: str = "graphql schema") -> GraphQLCheck:
    gd = diff_graphql(old, new, location)
    result = GraphQLCheck(list(gd.changes), gd)
    if not files:
        return result
    opset = load_operations(files, root=root)
    texts = {d.file: d.text for d in opset.docs}
    result.operations = len(opset.operations)
    for err in opset.errors:
        result.changes.append(Change(WARNING, "operation-unreadable", "operations", "", f"could not parse {err}"))

    old_issues, old_usage = validate(old, opset)
    new_issues, new_usage = validate(new, opset)
    for model, usage in ((old, old_usage), (new, new_usage)):
        for u in usage.values():
            u.types |= _input_closure(model, u.types)

    # which new errors a rename can fix
    fixable = {(e.file, e.start) for e in plan_fixes(new_issues, gd, texts)[0]}

    def key(i: Issue) -> tuple:
        return (i.file, i.offset, i.kind, i.coordinate)

    before = {key(i) for i in old_issues}
    for i in new_issues:
        where = f"{i.owner} ({_where(i, texts, root)})"
        if i.severity == "deprecated":
            sev, kind = (WARNING, "operation-uses-deprecated") if key(i) not in before else (INFO, "operation-uses-deprecated")
        elif key(i) in before:
            sev, kind = WARNING, "operation-invalid"
        else:
            sev, kind = BREAKING, "operation-broken"
        msg = _strip_owner(i)
        if kind == "operation-invalid":
            msg += " (also invalid against the baseline schema)"
        result.changes.append(Change(sev, kind, where, i.coordinate, msg,
                                     healable=(i.file, i.offset) in fixable and kind == "operation-broken"))

    # annotate schema changes with the operations they touch
    labels = set(old_usage) | set(new_usage)
    usage = {}
    for label in labels:
        u = Usage()
        for src in (old_usage.get(label), new_usage.get(label)):
            if src:
                u.coordinates |= src.coordinates
                u.types |= src.types
        usage[label] = u
    unused = 0
    kept = []
    for c in result.changes:
        if c.kind.startswith("operation-"):
            kept.append(c)
            continue
        c.used_by = _users(c, usage) or None
        if only_used and not c.used_by:
            unused += 1
            continue
        kept.append(c)
    if only_used and unused:
        kept.append(Change(INFO, "unused-changes-hidden", location, "",
                           f"{unused} schema change(s) don't touch your operations and are hidden (only_used: true)"))
    result.changes = kept
    return result


# ---------------------------------------------------------------- heal/verify


def heal(old: dict, new: dict, files: list[str], root: Path, api: str) -> tuple[dict, str]:
    opset = load_operations(files, root=root)
    texts = {d.file: d.text for d in opset.docs}
    issues, _ = validate(new, opset)
    gd = diff_graphql(old, new)
    edits, unresolved = plan_fixes(issues, gd, texts)
    files = {}
    for f in sorted({e.file for e in edits}):
        after = apply_edits(texts[f], [e for e in edits if e.file == f])
        files[_rel(f, root)] = {"before": _sha(texts[f]), "after": _sha(after)}
    plan = {
        "version": PLAN_VERSION,
        "api": api,
        "files": files,
        "edits": [{**e.to_dict(), "file": _rel(e.file, root)} for e in edits],
        "unresolved": [{"where": f"{i.owner} ({_where(i, texts, root)})", "message": _strip_owner(i)}
                       for i in unresolved if i.severity == "error"],
    }
    patch = unified_diff(texts, edits, root) if edits else ""
    return plan, patch


def plan_edits(plan: dict, root: Path) -> dict[str, list[Edit]]:
    by_file: dict[str, list[Edit]] = {}
    for e in plan.get("edits", []):
        path = e["file"] if os.path.isabs(e["file"]) else str(root / e["file"])
        by_file.setdefault(path, []).append(Edit(path, e["start"], e["end"], e["old"], e["new"], e.get("reason", "")))
    return by_file


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def patched_texts(plan: dict, root: Path) -> dict[str, str]:
    """File contents with the plan's edits applied.

    A file already in its fixed state (``--apply``, or the .patch applied by hand) is
    returned as it is; one that changed in any other way raises ValueError.
    """
    out = {}
    hashes = plan.get("files", {})
    for path, edits in plan_edits(plan, root).items():
        text = Path(path).read_text(encoding="utf-8")
        h = hashes.get(_rel(path, root), {})
        if h and _sha(text) == h.get("after"):
            out[path] = text
            continue
        if h and _sha(text) != h.get("before"):
            raise ValueError(f"{_rel(path, root)} changed since `apimend heal` made its fixes; "
                             "run `apimend heal` again to refresh them")
        out[path] = apply_edits(text, edits)
    return out


def apply(plan: dict, root: Path) -> list[str]:
    """Write the plan's edits to disk; returns the files that changed."""
    texts = patched_texts(plan, root)  # check every file before writing any
    changed = []
    for path, text in texts.items():
        if Path(path).read_text(encoding="utf-8") != text:
            Path(path).write_text(text, encoding="utf-8")
            changed.append(path)
    return sorted(changed)


@dataclass
class VerifyResult:
    operations: int
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.operations > 0 and not self.errors


def verify(new: dict, files: list[str], plan: dict | None, root: Path) -> VerifyResult:
    overrides = patched_texts(plan, root) if plan else {}
    opset: OperationSet = load_operations(files, overrides, root=root)
    texts = {d.file: d.text for d in opset.docs}
    issues, _ = validate(new, opset)
    errors = [f"{i.owner} ({_where(i, texts, root)}): {_strip_owner(i)}" for i in issues if i.severity == "error"]
    errors += [f"could not parse {e}" for e in opset.errors]
    return VerifyResult(len(opset.operations), errors)
