"""Detect and classify contract drift between two versions of an API."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .openapi import is_success, make_resolver, named_schemas, operations, schema_usage
from .schema import ROOT, Field, FieldMap, flatten, last_segment, parent, rebase

BREAKING, WARNING, INFO = "breaking", "warning", "info"
SEVERITY_RANK = {INFO: 0, WARNING: 1, BREAKING: 2}

RESPONSE, REQUEST = "response", "request"
PRESENCE_KINDS = {"required-field-added": "field-added", "now-optional": "required-changed",
                  "now-required": "required-changed", "type-widened": "type-changed",
                  "type-narrowed": "type-changed", "now-nullable": "type-changed"}
COERCIBLE = {"string", "integer", "number", "boolean"}


@dataclass
class Change:
    severity: str
    kind: str
    location: str
    path: str
    message: str
    healable: bool = False
    old: Any = None
    new: Any = None
    used_by: list[str] | None = None  # GraphQL operations affected by this change

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Rename:
    parent: str  # parent path, in the OLD shape
    old_name: str
    new_name: str
    score: float

    @property
    def old_path(self) -> str:
        return f"{self.parent}.{self.old_name}"

    @property
    def new_path(self) -> str:
        return f"{self.parent}.{self.new_name}"


@dataclass
class Coercion:
    path: str  # in the OLD shape
    from_type: str
    to_type: str


@dataclass
class FieldDiff:
    changes: list[Change] = field(default_factory=list)
    renames: list[Rename] = field(default_factory=list)
    coercions: list[Coercion] = field(default_factory=list)


# ------------------------------------------------------------------ name matching

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_name(name: str) -> str:
    """``userId``, ``user_id`` and ``User-ID`` all become ``userid``."""
    spaced = _CAMEL.sub("_", name)
    return re.sub(r"[^a-z0-9]", "", spaced.lower())


def name_words(name: str) -> list[str]:
    """``teamDiscussionsSetting`` -> ``['team', 'discussions', 'setting']``."""
    return [w for w in re.split(r"[^a-z0-9]+", _CAMEL.sub("_", name).lower()) if w]


def name_similarity(a: str, b: str) -> float:
    """How likely ``b`` is a rename of ``a`` (0..1).

    Same name modulo case and separators scores 1.0, one name containing the
    other 0.8+. Otherwise multi-word names must share words as well as letters,
    so long names with a common suffix are not mistaken for renames.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # one name containing the other ("email" -> "email_address") is a strong hint
    if (na in nb or nb in na) and min(len(na), len(nb)) >= 3:
        return max(ratio, 0.8)
    wa, wb = set(name_words(a)), set(name_words(b))
    if len(wa) <= 1 or len(wb) <= 1:
        return ratio
    return (ratio + len(wa & wb) / len(wa | wb)) / 2


def _hashable(values: list) -> set:
    return {str(v).lower() if isinstance(v, bool) else str(v) for v in values}


def value_similarity(old_vals: list | None, new_vals: list | None) -> tuple[float, int]:
    if not old_vals or not new_vals:
        return 0.0, 0
    a, b = _hashable(old_vals), _hashable(new_vals)
    overlap = len(a & b)
    return overlap / min(len(a), len(b)), min(len(a), len(b))


def _types_compatible(a: Field, b: Field) -> bool:
    at, bt = a.types - {"null"}, b.types - {"null"}
    if not at or not bt:
        return True
    if at & bt:
        return True
    return bool(at <= COERCIBLE and bt <= COERCIBLE)


def _can_coerce(value: Any, to: str) -> bool:
    from .heal import coerce_value  # local import to avoid a cycle

    try:
        coerce_value(value, to, strict=True)
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------- core diff


def _top_level(paths: set[str], other: FieldMap) -> list[str]:
    """Paths whose parent exists on the other side (so the whole subtree is the change)."""
    out = []
    for p in sorted(paths):
        par = parent(p)
        if par is None or par in other:
            out.append(p)
    return out


def _match_renames(
    removed: list[str],
    added: list[str],
    old: FieldMap,
    new: FieldMap,
    old_values: dict[str, list],
    new_values: dict[str, list],
) -> list[Rename]:
    candidates = []
    for r in removed:
        if last_segment(r) == "[]":
            continue
        for a in added:
            if last_segment(a) == "[]" or parent(r) != parent(a):
                continue
            if not _types_compatible(old[r], new[a]):
                continue
            ns = name_similarity(last_segment(r), last_segment(a))
            vs, distinct = value_similarity(old_values.get(r), new_values.get(a))
            strong_values = vs >= 0.8 and distinct >= 3
            if ns < 0.6 and not strong_values:
                continue
            score = max(ns, vs * 0.95 if distinct >= 3 else 0.0)
            candidates.append((score, r, a))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_r, used_a, out = set(), set(), []
    for score, r, a in candidates:
        if r in used_r or a in used_a:
            continue
        used_r.add(r)
        used_a.add(a)
        out.append(Rename(parent(r) or ROOT, last_segment(r), last_segment(a), round(score, 2)))
    return out


def diff_fields(
    old: FieldMap,
    new: FieldMap,
    *,
    direction: str = RESPONSE,
    location: str = "",
    old_values: dict[str, list] | None = None,
    new_values: dict[str, list] | None = None,
    detect_renames: bool = True,
    from_samples: bool = False,
) -> FieldDiff:
    """Compare two flattened shapes.

    ``from_samples`` softens findings that sampling alone cannot prove
    (for example a field looking optional because one sample omitted it).
    """
    old_values = old_values or {}
    cur_new = dict(new)
    cur_vals = dict(new_values or {})
    result = FieldDiff()

    # 1. Find renames, re-expressing the new shape in old names after each round
    #    so nested renames under a renamed parent are found too.
    for _ in range(10):
        if not detect_renames:
            break
        removed = _top_level(set(old) - set(cur_new), cur_new)
        added = _top_level(set(cur_new) - set(old), old)
        found = _match_renames(removed, added, old, cur_new, old_values, cur_vals)
        if not found:
            break
        for rn in found:
            result.renames.append(rn)
            cur_new = {rebase(p, rn.new_path, rn.old_path): f for p, f in cur_new.items()}
            cur_vals = {rebase(p, rn.new_path, rn.old_path): v for p, v in cur_vals.items()}

    response = direction == RESPONSE

    # Renames are reported with the path the consumer knows (old) and the new name.
    for rn in result.renames:
        result.changes.append(Change(
            BREAKING, "field-renamed", location, rn.old_path,
            f"`{rn.old_path}` appears to be renamed to `{rn.new_name}` (confidence {rn.score:.2f})",
            healable=response and from_samples, old=rn.old_name, new=rn.new_name,
        ))

    # 2. Removed and added fields (array items "x[]" are explained by the parent's type change)
    for p in _top_level(set(old) - set(cur_new), cur_new):
        if p.endswith("[]"):
            continue
        if response:
            result.changes.append(Change(BREAKING, "field-removed", location, p,
                                         f"`{p}` is no longer returned"))
        else:
            result.changes.append(Change(WARNING, "field-removed", location, p,
                                         f"`{p}` is no longer accepted in the request"))
    for p in _top_level(set(cur_new) - set(old), old):
        if p.endswith("[]"):
            continue
        f = cur_new[p]
        if not response and f.required:
            result.changes.append(Change(BREAKING, "required-field-added", location, p,
                                         f"new required request field `{p}`"))
        else:
            result.changes.append(Change(INFO, "field-added", location, p, f"new field `{p}`"))

    # 3. Fields present on both sides
    for p in sorted(set(old) & set(cur_new)):
        o, n = old[p], cur_new[p]
        result.changes.extend(_compare_field(p, o, n, response, location, from_samples,
                                             old_values.get(p), cur_vals.get(p), result))
    return result


def _compare_field(path, o: Field, n: Field, response: bool, location: str, from_samples: bool,
                   old_vals, new_vals, result: FieldDiff) -> list[Change]:
    out: list[Change] = []

    if o.ref != n.ref and o.ref and n.ref:
        out.extend(_compare_refs(path, o.ref, n.ref, response, location))

    if o.types and n.types and o.types != n.types:
        added_t, removed_t = n.types - o.types, o.types - n.types
        accepts = lambda ts, t: t in ts or (t == "integer" and "number" in ts)  # noqa: E731
        added_t = {t for t in added_t if not accepts(o.types, t)}
        removed_t = {t for t in removed_t if not accepts(n.types, t)}
        if response:
            if added_t == {"null"} and not removed_t:
                out.append(Change(WARNING, "now-nullable", location, path,
                                  f"`{path}` can now be null", old=sorted(o.types), new=sorted(n.types)))
            elif added_t:
                scalar_from = sorted(o.types - {"null"})
                scalar_to = sorted(n.types - {"null"})
                healable = False
                if (len(scalar_from) == 1 and len(scalar_to) == 1
                        and scalar_from[0] in COERCIBLE and scalar_to[0] in COERCIBLE
                        and new_vals and all(_can_coerce(v, scalar_from[0]) for v in new_vals)):
                    healable = True
                    result.coercions.append(Coercion(path, scalar_to[0], scalar_from[0]))
                sev = BREAKING if (removed_t or not (o.types & n.types)) else WARNING
                out.append(Change(sev, "type-changed", location, path,
                                  f"`{path}` changed type {_fmt(o.types)} -> {_fmt(n.types)}",
                                  healable=healable, old=sorted(o.types), new=sorted(n.types)))
            elif removed_t:
                out.append(Change(INFO, "type-narrowed", location, path,
                                  f"`{path}` narrowed type {_fmt(o.types)} -> {_fmt(n.types)}",
                                  old=sorted(o.types), new=sorted(n.types)))
        else:
            if removed_t:
                out.append(Change(BREAKING, "type-changed", location, path,
                                  f"`{path}` no longer accepts {_fmt(removed_t)} ({_fmt(o.types)} -> {_fmt(n.types)})",
                                  old=sorted(o.types), new=sorted(n.types)))
            elif added_t:
                out.append(Change(INFO, "type-widened", location, path,
                                  f"`{path}` now also accepts {_fmt(added_t)}",
                                  old=sorted(o.types), new=sorted(n.types)))

    if o.required != n.required:
        if response and o.required and not n.required:
            sev = INFO if from_samples else WARNING
            out.append(Change(sev, "now-optional", location, path,
                              f"`{path}` may now be absent" + (" (seen in samples)" if from_samples else "")))
        elif not response and not o.required and n.required:
            out.append(Change(BREAKING, "now-required", location, path, f"`{path}` is now required"))
        elif response:
            out.append(Change(INFO, "now-required", location, path, f"`{path}` is now always present"))
        else:
            out.append(Change(INFO, "now-optional", location, path, f"`{path}` is now optional"))

    if o.enum is not None or n.enum is not None:
        oe = {json.dumps(v) for v in (o.enum or ())}
        ne = {json.dumps(v) for v in (n.enum or ())}
        if o.enum is not None and n.enum is None:
            if response:
                out.append(Change(WARNING, "enum-removed", location, path,
                                  f"`{path}` is no longer limited to a fixed set of values"))
        elif o.enum is None and n.enum is not None:
            if not response:
                out.append(Change(BREAKING, "enum-added", location, path,
                                  f"`{path}` now only accepts {_values([json.dumps(v) for v in n.enum])}"))
        else:
            added_v, removed_v = ne - oe, oe - ne
            if added_v:
                sev = WARNING if response else INFO
                out.append(Change(sev, "enum-values-added", location, path,
                                  f"`{path}` has new values {_values(sorted(added_v))}",
                                  new=[json.loads(v) for v in sorted(added_v)]))
            if removed_v:
                sev = INFO if response else BREAKING
                out.append(Change(sev, "enum-values-removed", location, path,
                                  f"`{path}` dropped values {_values(sorted(removed_v))}",
                                  old=[json.loads(v) for v in sorted(removed_v)]))
    return out


def _compare_refs(path: str, old: str, new: str, response: bool, location: str) -> list[Change]:
    """A field pointing at another shared schema, or a oneOf/anyOf union gaining or losing members."""
    o, n = set(old.split("|")), set(new.split("|"))
    added, removed = sorted(n - o), sorted(o - n)
    if len(o) == 1 and len(n) == 1:
        return [Change(BREAKING if response else WARNING, "schema-changed", location, path,
                       f"`{path}` now uses schema `{new}` instead of `{old}`", old=old, new=new)]
    out = []
    if added:
        sev = WARNING if response else INFO
        out.append(Change(sev, "union-variant-added", location, path,
                          f"`{path}` can now also be {_values([f'`{a}`' for a in added])}", new=added))
    if removed:
        sev = INFO if response else BREAKING
        out.append(Change(sev, "union-variant-removed", location, path,
                          f"`{path}` can no longer be {_values([f'`{r}`' for r in removed])}", old=removed))
    return out


def _values(vals: list[str], limit: int = 6) -> str:
    shown = ", ".join(vals[:limit])
    return shown + (f" and {len(vals) - limit} more" if len(vals) > limit else "")


def _fmt(types) -> str:
    return "|".join(sorted(types)) or "any"


# ---------------------------------------------------------------------- spec diff


def diff_specs(old_doc: dict, new_doc: dict, api: str = "") -> list[Change]:
    """Compare two OpenAPI / Swagger documents."""
    old_ops, new_ops = operations(old_doc), operations(new_doc)
    prefix = f"{api} " if api else ""
    changes: list[Change] = []

    for key in sorted(set(old_ops) - set(new_ops)):
        changes.append(Change(BREAKING, "operation-removed", f"{prefix}{key}", ROOT,
                              f"operation `{key}` was removed"))
    for key in sorted(set(new_ops) - set(old_ops)):
        changes.append(Change(INFO, "operation-added", f"{prefix}{key}", ROOT,
                              f"new operation `{key}`"))

    for key in sorted(set(old_ops) & set(new_ops)):
        o, n = old_ops[key], new_ops[key]
        loc = f"{prefix}{key}"

        for pk in sorted(set(o.params) - set(n.params)):
            changes.append(Change(WARNING, "param-removed", loc, ROOT,
                                  f"{pk[0]} parameter `{pk[1]}` was removed"))
        for pk in sorted(set(n.params) - set(o.params)):
            p = n.params[pk]
            sev = BREAKING if p.required else INFO
            kind = "required-param-added" if p.required else "param-added"
            changes.append(Change(sev, kind, loc, ROOT,
                                  f"new {'required ' if p.required else ''}{pk[0]} parameter `{pk[1]}`"))
        for pk in sorted(set(o.params) & set(n.params)):
            op_, np_ = o.params[pk], n.params[pk]
            ploc = f"{loc} {pk[0]} param `{pk[1]}`"
            if not op_.required and np_.required:
                changes.append(Change(BREAKING, "now-required", ploc, ROOT,
                                      f"{pk[0]} parameter `{pk[1]}` is now required"))
            fd = diff_fields(op_.fields, np_.fields, direction=REQUEST, location=ploc, detect_renames=False)
            changes.extend(fd.changes)

        if o.request is not None or n.request is not None:
            rloc = f"{loc} request body"
            if o.request is None:
                sev = BREAKING if n.request_required else INFO
                changes.append(Change(sev, "request-body-added", rloc, ROOT, "a request body was added"))
            elif n.request is None:
                changes.append(Change(WARNING, "request-body-removed", rloc, ROOT, "the request body was removed"))
            else:
                if not o.request_required and n.request_required:
                    changes.append(Change(BREAKING, "now-required", rloc, ROOT, "the request body is now required"))
                changes.extend(diff_fields(o.request, n.request, direction=REQUEST, location=rloc).changes)

        for status in sorted(o.responses):
            rloc = f"{loc} response {status}"
            if status not in n.responses:
                if is_success(status):
                    changes.append(Change(BREAKING, "response-removed", rloc, ROOT,
                                          f"response `{status}` is no longer documented"))
                continue
            changes.extend(diff_fields(o.responses[status], n.responses[status],
                                       direction=RESPONSE, location=rloc).changes)
        for status in sorted(set(n.responses) - set(o.responses)):
            if is_success(status):
                changes.append(Change(INFO, "response-added", f"{loc} response {status}", ROOT,
                                      f"new response `{status}`"))

    changes.extend(diff_schemas(old_doc, new_doc, prefix))
    return changes


def diff_schemas(old_doc: dict, new_doc: dict, prefix: str = "") -> list[Change]:
    """Diff each shared schema once, graded by where it is used.

    A schema used only in requests follows request rules, only in responses
    follows response rules, and one used in both keeps the stricter finding.
    """
    old_s, new_s = named_schemas(old_doc), named_schemas(new_doc)
    old_req, old_resp = schema_usage(old_doc)
    new_req, new_resp = schema_usage(new_doc)
    old_res, new_res = make_resolver(old_doc), make_resolver(new_doc)
    changes: list[Change] = []
    for name in sorted(set(old_s) & set(new_s)):
        used_req = name in old_req or name in new_req
        used_resp = name in old_resp or name in new_resp
        if not (used_req or used_resp):
            continue
        of = flatten(old_s[name], old_res, expand_refs=False)
        nf = flatten(new_s[name], new_res, expand_refs=False)
        if of == nf:
            continue
        loc = f"{prefix}schema {name}"
        best: dict[tuple[str, str], Change] = {}
        for direction, used in ((RESPONSE, used_resp), (REQUEST, used_req)):
            if not used:
                continue
            for c in diff_fields(of, nf, direction=direction, location=loc).changes:
                # request and response rules name the same event differently
                kind = PRESENCE_KINDS.get(c.kind, c.kind)
                key = (c.path, kind)
                if key not in best or SEVERITY_RANK[c.severity] > SEVERITY_RANK[best[key].severity]:
                    best[key] = c
        changes.extend(sorted(best.values(), key=lambda c: (-SEVERITY_RANK[c.severity], c.path)))
    for name in sorted(set(old_s) - set(new_s)):
        if name in old_req or name in old_resp:
            changes.append(Change(WARNING, "schema-removed", f"{prefix}schema {name}", ROOT,
                                  f"shared schema `{name}` was removed or renamed"))
    return changes


def worst(changes: list[Change]) -> str | None:
    if not changes:
        return None
    return max((c.severity for c in changes), key=SEVERITY_RANK.__getitem__)
