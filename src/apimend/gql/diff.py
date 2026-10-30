"""Breaking-change rules for GraphQL schemas.

Severities follow the reference implementation's ``findBreakingChanges`` and
``findDangerousChanges`` (breaking / warning), plus two additions:

* a newly deprecated field, argument, input field or enum value is a warning,
  because deprecation is usually the notice before removal;
* a removed field that looks renamed is reported as a rename, which
  :mod:`apimend.gql.operations` can heal with an alias.

Paths are schema coordinates: ``User``, ``User.email``, ``Query.users(first:)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..diff import BREAKING, INFO, WARNING, Change, name_similarity
from .model import named

TypeTree = tuple


def type_tree(t: str) -> TypeTree:
    if t.endswith("!"):
        return ("nonnull", type_tree(t[:-1]))
    if t.startswith("[") and t.endswith("]"):
        return ("list", type_tree(t[1:-1]))
    return ("named", t)


def safe_for_output(old: TypeTree, new: TypeTree) -> bool:
    """Can clients that read ``old`` still read ``new``?"""
    if old[0] == "list":
        return (new[0] == "list" and safe_for_output(old[1], new[1])) or \
               (new[0] == "nonnull" and safe_for_output(old, new[1]))
    if old[0] == "nonnull":
        return new[0] == "nonnull" and safe_for_output(old[1], new[1])
    return (new[0] == "named" and new[1] == old[1]) or (new[0] == "nonnull" and safe_for_output(old, new[1]))


def safe_for_input(old: TypeTree, new: TypeTree) -> bool:
    """Is every value clients could send for ``old`` still accepted by ``new``?"""
    if old[0] == "list":
        return new[0] == "list" and safe_for_input(old[1], new[1])
    if old[0] == "nonnull":
        return (new[0] == "nonnull" and safe_for_input(old[1], new[1])) or \
               (new[0] != "nonnull" and safe_for_input(old[1], new))
    return new[0] == "named" and new[1] == old[1]


def _norm_default(v: str | None) -> str | None:
    return None if v is None else re.sub(r"\s+", "", v)


@dataclass
class FieldRename:
    type: str
    old: str
    new: str
    score: float
    old_type: str = ""
    new_type: str = ""


@dataclass
class ArgRename:
    type: str
    field: str
    old: str
    new: str
    score: float


def _match(removed: dict[str, str], added: dict[str, str]) -> list[tuple[str, str, float]]:
    """Pair removed and added names whose names are similar and named types match."""
    cands = []
    for r, rtype in removed.items():
        for a, atype in added.items():
            if named(rtype) != named(atype):
                continue
            score = name_similarity(r, a)
            if score >= 0.6:
                cands.append((score, r, a))
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_r, used_a, out = set(), set(), []
    for score, r, a in cands:
        if r not in used_r and a not in used_a:
            used_r.add(r)
            used_a.add(a)
            out.append((r, a, round(score, 2)))
    return out


class _Differ:
    def __init__(self, old: dict, new: dict, location: str):
        self.old, self.new = old, new
        self.loc = location
        self.changes: list[Change] = []
        self.field_renames: list[FieldRename] = []
        self.arg_renames: list[ArgRename] = []

    def add(self, severity: str, kind: str, path: str, message: str, **kw) -> None:
        self.changes.append(Change(severity, kind, self.loc, path, message, **kw))

    def run(self) -> None:
        ot, nt = self.old["types"], self.new["types"]
        # compare root types by role, so renaming "Query" to "RootQuery" is not a removal
        pairs: dict[str, str] = {}
        for role in ("query", "mutation", "subscription"):
            o, n = self.old["roots"].get(role), self.new["roots"].get(role)
            if o and n and o in ot and n in nt:
                pairs[o] = n
            elif o and not n:
                self.add(BREAKING, "root-removed", role, f"the schema no longer supports {role} operations")
        for name in ot:
            if name in nt and name not in pairs and name not in pairs.values():
                pairs[name] = name
        matched_new = set(pairs.values())
        for name in sorted(set(ot) - set(pairs)):
            self.add(BREAKING, "type-removed", name, f"type `{name}` was removed")
        for name in sorted(set(nt) - matched_new):
            self.add(INFO, "type-added", name, f"new type `{name}`")
        for oname, nname in sorted(pairs.items()):
            self.compare_type(oname, ot[oname], nname, nt[nname])

    def compare_type(self, oname: str, o: dict, name: str, n: dict) -> None:
        if o["kind"] != n["kind"]:
            self.add(BREAKING, "type-kind-changed", name,
                     f"`{name}` changed from {o['kind'].lower()} to {n['kind'].lower()}", old=o["kind"], new=n["kind"])
            return
        kind = o["kind"]
        if kind in ("OBJECT", "INTERFACE"):
            self.compare_fields(name, o.get("fields", {}), n.get("fields", {}))
            for iface in sorted(set(o.get("interfaces", [])) - set(n.get("interfaces", []))):
                self.add(BREAKING, "interface-removed", name, f"`{name}` no longer implements `{iface}`")
            for iface in sorted(set(n.get("interfaces", [])) - set(o.get("interfaces", []))):
                self.add(WARNING, "interface-added", name, f"`{name}` now implements `{iface}`")
        elif kind == "INPUT_OBJECT":
            self.compare_inputs(name, f"{name}.", "", o.get("inputs", {}), n.get("inputs", {}), "input field")
        elif kind == "ENUM":
            ov, nv = o.get("values", {}), n.get("values", {})
            for v in sorted(set(ov) - set(nv)):
                self.add(BREAKING, "enum-value-removed", f"{name}.{v}", f"enum value `{name}.{v}` was removed")
            for v in sorted(set(nv) - set(ov)):
                self.add(WARNING, "enum-value-added", f"{name}.{v}",
                         f"new enum value `{name}.{v}` (code that switches on `{name}` may not handle it)")
            for v in sorted(set(ov) & set(nv)):
                self.deprecation(f"{name}.{v}", ov[v], nv[v], "enum value")
        elif kind == "UNION":
            op, np_ = set(o.get("possible", [])), set(n.get("possible", []))
            for m in sorted(op - np_):
                self.add(BREAKING, "union-member-removed", name, f"`{m}` was removed from union `{name}`")
            for m in sorted(np_ - op):
                self.add(WARNING, "union-member-added", name,
                         f"`{m}` was added to union `{name}` (code that switches on `__typename` may not handle it)")

    def deprecation(self, path: str, o: dict, n: dict, what: str) -> None:
        od, nd = o.get("deprecated"), n.get("deprecated")
        if nd and not od:
            self.add(WARNING, "deprecated", path, f"{what} `{path}` is now deprecated: {nd}", new=nd)
        elif od and not nd:
            self.add(INFO, "undeprecated", path, f"{what} `{path}` is no longer deprecated")

    def compare_fields(self, tname: str, of: dict, nf: dict) -> None:
        removed = {k: of[k]["type"] for k in set(of) - set(nf)}
        added = {k: nf[k]["type"] for k in set(nf) - set(of)}
        renames = {r: (a, s) for r, a, s in _match(removed, added)}
        for f in sorted(removed):
            path = f"{tname}.{f}"
            if f in renames:
                a, score = renames[f]
                self.field_renames.append(FieldRename(tname, f, a, score, of[f]["type"], nf[a]["type"]))
                self.add(BREAKING, "field-renamed", path,
                         f"`{path}` was removed; it appears to be renamed to `{a}` (confidence {score:.2f})",
                         healable=True, old=f, new=a)
            else:
                dep = of[f].get("deprecated")
                hint = " (it was deprecated)" if dep else ""
                self.add(BREAKING, "field-removed", path, f"`{path}` was removed{hint}")
        renamed_to = {a for a, _ in renames.values()}
        for f in sorted(set(added) - renamed_to):
            self.add(INFO, "field-added", f"{tname}.{f}", f"new field `{tname}.{f}`")
        for f in sorted(set(of) & set(nf)):
            o, n = of[f], nf[f]
            path = f"{tname}.{f}"
            if o["type"] != n["type"]:
                safe = safe_for_output(type_tree(o["type"]), type_tree(n["type"]))
                self.add(INFO if safe else BREAKING, "field-type-changed", path,
                         f"`{path}` changed type `{o['type']}` → `{n['type']}`", old=o["type"], new=n["type"])
            self.deprecation(path, o, n, "field")
            self.compare_inputs(tname, f"{path}(", ":)", o.get("args", {}), n.get("args", {}), "argument",
                                field_name=f)

    def compare_inputs(self, tname: str, prefix: str, suffix: str, oi: dict, ni: dict, what: str,
                       field_name: str | None = None) -> None:
        removed = {k: oi[k]["type"] for k in set(oi) - set(ni)}
        added = {k: ni[k]["type"] for k in set(ni) - set(oi)}
        renames = {r: (a, s) for r, a, s in _match(removed, added)} if field_name else {}
        for a in sorted(removed):
            path = f"{prefix}{a}{suffix}"
            if a in renames:
                new, score = renames[a]
                self.arg_renames.append(ArgRename(tname, field_name or "", a, new, score))
                self.add(BREAKING, "argument-renamed", path,
                         f"{what} `{path}` was removed; it appears to be renamed to `{new}` (confidence {score:.2f})",
                         healable=True, old=a, new=new)
            else:
                self.add(BREAKING, f"{what.replace(' ', '-')}-removed", path, f"{what} `{path}` was removed")
        renamed_to = {a for a, _ in renames.values()}
        for a in sorted(set(added) - renamed_to):
            path = f"{prefix}{a}{suffix}"
            v = ni[a]
            required = v["type"].endswith("!") and v.get("default") is None
            if required:
                self.add(BREAKING, f"required-{what.replace(' ', '-')}-added", path, f"new required {what} `{path}`")
            else:
                self.add(INFO, f"{what.replace(' ', '-')}-added", path, f"new optional {what} `{path}`")
        for a in sorted(set(oi) & set(ni)):
            o, n = oi[a], ni[a]
            path = f"{prefix}{a}{suffix}"
            if o["type"] != n["type"]:
                safe = safe_for_input(type_tree(o["type"]), type_tree(n["type"]))
                self.add(INFO if safe else BREAKING, f"{what.replace(' ', '-')}-type-changed", path,
                         f"{what} `{path}` changed type `{o['type']}` → `{n['type']}`", old=o["type"], new=n["type"])
            if _norm_default(o.get("default")) != _norm_default(n.get("default")):
                self.add(WARNING, "default-changed", path,
                         f"default of {what} `{path}` changed from {o.get('default') or 'none'} "
                         f"to {n.get('default') or 'none'}", old=o.get("default"), new=n.get("default"))
            self.deprecation(path, o, n, what)


@dataclass
class GraphQLDiff:
    changes: list[Change]
    field_renames: list[FieldRename]
    arg_renames: list[ArgRename]


def diff_graphql(old: dict, new: dict, location: str = "graphql schema") -> GraphQLDiff:
    d = _Differ(old, new, location)
    d.run()
    return GraphQLDiff(d.changes, d.field_renames, d.arg_renames)
