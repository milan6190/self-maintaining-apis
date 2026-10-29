"""JSON shape handling: infer schemas from samples and flatten schemas to field paths.

Paths use a small JSONPath-like notation rooted at ``$``:

    $                 the payload itself
    $.data.user_id    a property
    $.items[]         every element of an array
    $.items[].id      a property of every element

Keys that contain ``.`` or ``[`` are not supported in paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

ROOT = "$"
SCALARS = frozenset({"string", "integer", "number", "boolean", "null"})

Resolver = Callable[[str], Any]


@dataclass(frozen=True)
class Field:
    """One field of a flattened schema."""

    types: frozenset[str] = field(default_factory=frozenset)  # empty = any type
    required: bool = True
    enum: tuple | None = None
    format: str | None = None
    ref: str | None = None  # named schema this field points to (when refs are not expanded)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"types": sorted(self.types), "required": self.required}
        if self.ref:
            out["ref"] = self.ref
        if self.enum is not None:
            out["enum"] = list(self.enum)
        if self.format:
            out["format"] = self.format
        return out


FieldMap = dict[str, Field]


# --------------------------------------------------------------------------- paths

_SEGMENT = re.compile(r"\.([^.\[]+)|(\[\])")


def split_path(path: str) -> list[str]:
    """``$.a.b[].c`` -> ``['a', 'b', '[]', 'c']``."""
    if not path.startswith(ROOT):
        raise ValueError(f"path must start with '$': {path!r}")
    rest = path[1:]
    segments: list[str] = []
    pos = 0
    while pos < len(rest):
        m = _SEGMENT.match(rest, pos)
        if not m:
            raise ValueError(f"cannot parse path {path!r} at {rest[pos:]!r}")
        segments.append(m.group(1) if m.group(1) is not None else "[]")
        pos = m.end()
    return segments


def join_path(segments: Iterable[str]) -> str:
    out = ROOT
    for seg in segments:
        out += "[]" if seg == "[]" else f".{seg}"
    return out


def parent(path: str) -> str | None:
    segs = split_path(path)
    if not segs:
        return None
    return join_path(segs[:-1])


def last_segment(path: str) -> str:
    segs = split_path(path)
    return segs[-1] if segs else ROOT


def rebase(path: str, from_prefix: str, to_prefix: str) -> str:
    """Swap a leading path prefix, respecting segment boundaries."""
    if path == from_prefix:
        return to_prefix
    for sep in (".", "["):
        if path.startswith(from_prefix + sep):
            return to_prefix + path[len(from_prefix):]
    return path


# ---------------------------------------------------------------------- inference


def type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _normalize_types(types: Iterable[str]) -> list[str]:
    ts = set(types)
    if "integer" in ts and "number" in ts:
        ts.discard("integer")
    return sorted(ts)


def _schema_of(value: Any) -> dict:
    t = type_of(value)
    if t == "object":
        return {
            "type": "object",
            "properties": {k: _schema_of(v) for k, v in value.items()},
            "required": sorted(value.keys()),
        }
    if t == "array":
        items: dict | None = None
        for item in value:
            s = _schema_of(item)
            items = s if items is None else merge(items, s)
        out: dict = {"type": "array"}
        if items is not None:
            out["items"] = items
        return out
    return {"type": t}


def _type_list(schema: dict) -> list[str]:
    t = schema.get("type", [])
    return [t] if isinstance(t, str) else list(t)


def merge(a: dict, b: dict) -> dict:
    """Merge two inferred schemas into one that accepts both."""
    types = _normalize_types(_type_list(a) + _type_list(b))
    out: dict = {"type": types[0] if len(types) == 1 else types}
    if "properties" in a or "properties" in b:
        pa, pb = a.get("properties", {}), b.get("properties", {})
        props = {}
        for key in list(pa) + [k for k in pb if k not in pa]:
            if key in pa and key in pb:
                props[key] = merge(pa[key], pb[key])
            else:
                props[key] = pa.get(key) or pb[key]
        out["properties"] = props
        a_obj = "object" in _type_list(a)
        b_obj = "object" in _type_list(b)
        if a_obj and b_obj:
            req = sorted(set(a.get("required", [])) & set(b.get("required", [])))
        else:
            req = sorted(set(a.get("required", [])) | set(b.get("required", [])))
        out["required"] = req
    if "items" in a or "items" in b:
        if "items" in a and "items" in b:
            out["items"] = merge(a["items"], b["items"])
        else:
            out["items"] = a.get("items") or b["items"]
    return out


def infer(samples: Iterable[Any]) -> dict:
    """Infer a JSON Schema that accepts every sample."""
    schema: dict | None = None
    for sample in samples:
        s = _schema_of(sample)
        schema = s if schema is None else merge(schema, s)
    return schema if schema is not None else {}


def collect_values(samples: Iterable[Any], limit: int = 200) -> dict[str, list]:
    """Scalar values seen at each path, used to match renamed fields."""
    out: dict[str, list] = {}

    def walk(node: Any, path: str) -> None:
        t = type_of(node)
        if t == "object":
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif t == "array":
            for item in node:
                walk(item, f"{path}[]")
        elif t != "null":
            bucket = out.setdefault(path, [])
            if len(bucket) < limit:
                bucket.append(node)

    for sample in samples:
        walk(sample, ROOT)
    return out


# ------------------------------------------------------------------- flattening


def _deref(schema: Any, resolve: Resolver | None, stack: tuple[str, ...]):
    """Follow ``$ref`` chains. Returns (schema, stack) or (None, stack) on a cycle."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if ref in stack or resolve is None or seen > 50:
            return None, stack
        stack = stack + (ref,)
        schema = resolve(ref)
        seen += 1
    return schema, stack


def _combine(parts: list[dict], mode: str) -> dict:
    """Merge already-dereferenced subschemas for allOf (mode='all') or anyOf/oneOf."""
    types: list[str] = []
    props: dict[str, Any] = {}
    required: set[str] | None = None
    enum: list | None = None
    items: list = []
    nullable = False
    unconstrained = False
    for p in parts:
        types += _schema_types(p)
        nullable = nullable or bool(p.get("nullable"))
        for k, v in p.get("properties", {}).items():
            if k in props:
                key = "allOf" if mode == "all" else "anyOf"
                props[k] = {key: [props[k], v]}
            else:
                props[k] = v
        r = set(p.get("required", []))
        if mode == "all":
            required = (required or set()) | r
        else:
            required = r if required is None else required & r
        if "enum" in p:
            enum = (enum or []) + [e for e in p["enum"] if e not in (enum or [])]
        elif mode == "any" and _schema_types(p):
            unconstrained = True  # one open branch means the union is open too
        if "items" in p:
            items.append(p["items"])
    out: dict = {}
    if mode == "all":
        # allOf: types intersect where declared; empty means "any"
        declared = [set(_schema_types(p)) for p in parts if _schema_types(p)]
        if declared:
            common = set.intersection(*declared)
            types = list(common or declared[0])
    if types:
        out["type"] = _normalize_types(types)
    if nullable:
        out["nullable"] = True
    if props:
        out["properties"] = props
    if required:
        out["required"] = sorted(required)
    if enum is not None and not unconstrained:
        out["enum"] = enum
    if items:
        out["items"] = items[0] if len(items) == 1 else {"anyOf": items}
    return out


def _schema_types(schema: dict) -> list[str]:
    t = schema.get("type")
    if t is None:
        if "properties" in schema or "additionalProperties" in schema:
            return ["object"]
        if "items" in schema:
            return ["array"]
        if "enum" in schema:
            return _normalize_types(type_of(v) for v in schema["enum"])
        return []
    return [t] if isinstance(t, str) else list(t)


MAX_PATHS = 20000


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def flatten(schema: Any, resolve: Resolver | None = None, root: str = ROOT,
            expand_refs: bool = True, max_paths: int = MAX_PATHS) -> FieldMap:
    """Flatten a JSON Schema / OpenAPI schema into ``{path: Field}``.

    With ``expand_refs=False`` every ``$ref`` becomes a leaf that names its target
    (``Field.ref``), so large interlinked specs stay linear in size. ``allOf``
    members are still merged in, since they describe the same object.
    """
    out: FieldMap = {}

    def boundary(s: dict, path: str, required: bool, extra_types: Iterable[str] = ()) -> None:
        target, _ = _deref(s, resolve, ())
        types = set(_schema_types(target)) if isinstance(target, dict) else set()
        types |= set(extra_types)
        _put(path, Field(frozenset(_normalize_types(types)), required, ref=ref_name(s["$ref"])))

    def _put(path: str, f: Field) -> None:
        if path in out:  # same path reached twice (anyOf items): widen
            prev = out[path]
            f = Field(
                frozenset(_normalize_types(prev.types | f.types)) if prev.types and f.types else frozenset(),
                prev.required and f.required,
                None if prev.enum is None or f.enum is None else tuple(dict.fromkeys(prev.enum + f.enum)),
                prev.format or f.format,
                prev.ref or f.ref,
            )
        out[path] = f

    def walk(s: Any, path: str, required: bool, stack: tuple[str, ...], depth: int) -> None:
        if depth > 64 or len(out) >= max_paths:
            return
        if not expand_refs and isinstance(s, dict) and "$ref" in s:
            boundary(s, path, required)
            return
        s, stack = _deref(s, resolve, stack)
        if not isinstance(s, dict):
            _put(path, Field(frozenset(), required))
            return
        ref_label: str | None = None
        for key, mode in (("allOf", "all"), ("anyOf", "any"), ("oneOf", "any")):
            if key in s:
                parts = []
                base = {k: v for k, v in s.items() if k != key}
                if base:
                    parts.append(base)
                refs: list[str] = []
                for sub in s[key]:
                    if not expand_refs and mode == "any" and isinstance(sub, dict) and "$ref" in sub:
                        # keep the reference as a boundary; borrow only its type
                        target, _ = _deref(sub, resolve, ())
                        refs.append(ref_name(sub["$ref"]))
                        if isinstance(target, dict):
                            parts.append({"type": _schema_types(target) or []})
                        continue
                    d, _ = _deref(sub, resolve, stack)
                    if isinstance(d, dict):
                        parts.append(d)
                s = _combine(parts, mode)
                if refs:
                    ref_label = "|".join(sorted(set(refs)))
        types = set(_schema_types(s))
        if s.get("nullable") and types:
            types.add("null")
        enum = tuple(s["enum"]) if "enum" in s else None
        _put(path, Field(frozenset(_normalize_types(types)), required, enum, s.get("format"), ref_label))
        if "object" in types or (not types and "properties" in s):
            req = set(s.get("required", []))
            for name, sub in s.get("properties", {}).items():
                walk(sub, f"{path}.{name}", name in req, stack, depth + 1)
        if ("array" in types or not types) and "items" in s:
            walk(s["items"], f"{path}[]", True, stack, depth + 1)

    walk(schema, root, True, (), 0)
    return out


def flatten_samples(samples: list[Any]) -> FieldMap:
    return flatten(infer(samples))
