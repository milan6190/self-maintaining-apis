"""A normalized, JSON-serializable GraphQL schema model.

The same model is built from SDL, from an introspection result, or from a live
endpoint, so any two can be compared::

    {
      "x-apimend": "graphql", "version": 1,
      "roots": {"query": "Query", "mutation": "Mutation"},
      "types": {
        "User":   {"kind": "OBJECT", "interfaces": ["Node"],
                   "fields": {"repos": {"type": "[Repo!]!",
                                        "args": {"first": {"type": "Int", "default": "10"}},
                                        "deprecated": "Use repositories"}}},
        "Role":   {"kind": "ENUM", "values": {"ADMIN": {}, "GUEST": {"deprecated": "No longer supported"}}},
        "Search": {"kind": "UNION", "possible": ["User", "Repo"]},
        "NewUser":{"kind": "INPUT_OBJECT", "inputs": {"name": {"type": "String!"}}},
        "Date":   {"kind": "SCALAR"}
      }
    }

Empty ``args``, ``deprecated`` and ``default`` keys are left out.
"""

from __future__ import annotations

import json
from typing import Any

from .parser import Directive, TypeSystemDocument, parse_schema, print_value

MARKER = "graphql"
BUILTIN_SCALARS = ("String", "Int", "Float", "Boolean", "ID")
DEFAULT_DEPRECATION = "No longer supported"


def is_model(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("x-apimend") == MARKER


def is_introspection(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    if "__schema" in doc:
        return True
    data = doc.get("data")
    return isinstance(data, dict) and "__schema" in data


def named(type_str: str) -> str:
    return type_str.strip("[]!")


def _deprecation(directives: list[Directive]) -> str | None:
    for d in directives:
        if d.name == "deprecated":
            reason = d.args.get("reason")
            return reason[1] if reason and reason[0] == "string" else DEFAULT_DEPRECATION
    return None


def _input_value(iv) -> dict:
    out: dict = {"type": iv.type}
    if iv.default is not None:
        out["default"] = print_value(iv.default)
    dep = _deprecation(iv.directives)
    if dep:
        out["deprecated"] = dep
    return out


def _empty_model() -> dict:
    return {"x-apimend": MARKER, "version": 1, "roots": {}, "types": {}}


def _finish(model: dict) -> dict:
    types = model["types"]
    for s in BUILTIN_SCALARS:
        types.setdefault(s, {"kind": "SCALAR"})
    roots = model["roots"]
    if not roots:
        for role, default in (("query", "Query"), ("mutation", "Mutation"), ("subscription", "Subscription")):
            if default in types:
                roots[role] = default
    # interfaces list the object (and interface) types that implement them
    for t in types.values():
        if t["kind"] == "INTERFACE":
            t["possible"] = []
    for name, t in sorted(types.items()):
        for iface in t.get("interfaces", []):
            target = types.get(iface)
            if target is not None and target["kind"] == "INTERFACE" and t["kind"] == "OBJECT":
                target["possible"].append(name)
    model["types"] = dict(sorted(types.items()))
    return model


def from_sdl_document(doc: TypeSystemDocument) -> dict:
    model = _empty_model()
    types: dict[str, dict] = model["types"]
    # definitions first, then extensions, so "extend type" before "type" still works
    for td in sorted(doc.types, key=lambda d: d.extend):
        t = types.get(td.name)
        if t is None:
            t = {"kind": td.kind}
            types[td.name] = t
        if td.kind in ("OBJECT", "INTERFACE"):
            fields = t.setdefault("fields", {})
            for f in td.fields:
                entry: dict = {"type": f.type}
                if f.args:
                    entry["args"] = {a.name: _input_value(a) for a in f.args}
                dep = _deprecation(f.directives)
                if dep:
                    entry["deprecated"] = dep
                fields[f.name] = entry
            if td.interfaces:
                existing = t.setdefault("interfaces", [])
                existing.extend(i for i in td.interfaces if i not in existing)
        elif td.kind == "INPUT_OBJECT":
            t.setdefault("inputs", {}).update({iv.name: _input_value(iv) for iv in td.inputs})
        elif td.kind == "ENUM":
            values = t.setdefault("values", {})
            for name, dirs in td.values:
                dep = _deprecation(dirs)
                values[name] = {"deprecated": dep} if dep else {}
        elif td.kind == "UNION":
            members = t.setdefault("possible", [])
            members.extend(m for m in td.members if m not in members)
    model["roots"] = dict(doc.roots)
    return _finish(model)


def from_sdl(source: str) -> dict:
    return from_sdl_document(parse_schema(source))


def _type_str(ref: dict | None) -> str:
    if not ref:
        return "Unknown"
    kind = ref.get("kind")
    if kind == "NON_NULL":
        return _type_str(ref.get("ofType")) + "!"
    if kind == "LIST":
        return "[" + _type_str(ref.get("ofType")) + "]"
    return ref.get("name") or "Unknown"


def _intro_input(iv: dict) -> dict:
    out: dict = {"type": _type_str(iv.get("type"))}
    if iv.get("defaultValue") is not None:
        out["default"] = iv["defaultValue"]
    if iv.get("isDeprecated"):
        out["deprecated"] = iv.get("deprecationReason") or DEFAULT_DEPRECATION
    return out


def from_introspection(result: dict) -> dict:
    schema = result.get("__schema") or (result.get("data") or {}).get("__schema")
    if not isinstance(schema, dict):
        raise ValueError("not a GraphQL introspection result (no __schema)")
    model = _empty_model()
    for role, key in (("query", "queryType"), ("mutation", "mutationType"), ("subscription", "subscriptionType")):
        ref = schema.get(key)
        if ref and ref.get("name"):
            model["roots"][role] = ref["name"]
    for t in schema.get("types") or []:
        name = t.get("name") or ""
        if name.startswith("__"):
            continue
        kind = t.get("kind")
        entry: dict = {"kind": kind}
        if kind in ("OBJECT", "INTERFACE"):
            fields = {}
            for f in t.get("fields") or []:
                fe: dict = {"type": _type_str(f.get("type"))}
                if f.get("args"):
                    fe["args"] = {a["name"]: _intro_input(a) for a in f["args"]}
                if f.get("isDeprecated"):
                    fe["deprecated"] = f.get("deprecationReason") or DEFAULT_DEPRECATION
                fields[f["name"]] = fe
            entry["fields"] = fields
            ifaces = [i["name"] for i in t.get("interfaces") or [] if i.get("name")]
            if ifaces:
                entry["interfaces"] = ifaces
        elif kind == "INPUT_OBJECT":
            entry["inputs"] = {iv["name"]: _intro_input(iv) for iv in t.get("inputFields") or []}
        elif kind == "ENUM":
            entry["values"] = {
                v["name"]: ({"deprecated": v.get("deprecationReason") or DEFAULT_DEPRECATION}
                            if v.get("isDeprecated") else {})
                for v in t.get("enumValues") or []
            }
        elif kind == "UNION":
            entry["possible"] = [p["name"] for p in t.get("possibleTypes") or []]
        model["types"][name] = entry
    return _finish(model)


# ------------------------------------------------------------- introspection

_TYPE_REF = "kind name ofType { kind name ofType { kind name ofType { kind name ofType { kind name " \
            "ofType { kind name ofType { kind name ofType { kind name } } } } } } }"


def introspection_query(input_deprecations: bool = True) -> str:
    dep = "(includeDeprecated: true)" if input_deprecations else ""
    input_value = f"name type {{ {_TYPE_REF} }} defaultValue"
    if input_deprecations:
        input_value += " isDeprecated deprecationReason"
    return (
        "query ApimendIntrospection { __schema { "
        "queryType { name } mutationType { name } subscriptionType { name } "
        "types { kind name "
        f"fields(includeDeprecated: true) {{ name args{dep} {{ {input_value} }} type {{ {_TYPE_REF} }} "
        "isDeprecated deprecationReason } "
        f"inputFields{dep} {{ {input_value} }} "
        "interfaces { name } "
        "enumValues(includeDeprecated: true) { name isDeprecated deprecationReason } "
        "possibleTypes { name } } } }"
    )


def introspect(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """Run an introspection query against a live endpoint and return the model."""
    from ..fetch import FetchError, http

    last_error = ""
    for full in (True, False):  # older servers reject includeDeprecated on args/inputs
        body = {"query": introspection_query(full), "operationName": "ApimendIntrospection"}
        text = http(url, method="POST", headers={"Content-Type": "application/json", **(headers or {})},
                    body=body, timeout=timeout)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise FetchError(f"POST {url} did not return JSON") from e
        if isinstance(data, dict) and is_introspection(data) and (data.get("data") or {}).get("__schema"):
            return from_introspection(data)
        errors = data.get("errors") if isinstance(data, dict) else None
        last_error = "; ".join(str(e.get("message", e)) for e in errors or [] if isinstance(e, dict))[:300]
    raise FetchError(f"introspection failed at {url}: {last_error or 'no schema returned'} "
                     "(introspection may be disabled; point `spec:` at a schema file instead)")
