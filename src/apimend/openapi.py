"""Read OpenAPI 3.x and Swagger 2.0 documents into comparable operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import unquote

from .schema import FieldMap, flatten

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


@dataclass
class Param:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    fields: FieldMap


@dataclass
class Operation:
    method: str
    path: str
    params: dict[tuple[str, str], Param] = field(default_factory=dict)
    request: FieldMap | None = None
    request_required: bool = False
    responses: dict[str, FieldMap] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"


def make_resolver(doc: dict):
    """Resolve local JSON pointers such as ``#/components/schemas/Pet``."""

    def resolve(ref: str) -> Any:
        if not ref.startswith("#/"):
            return {}  # external refs are treated as "any"
        node: Any = doc
        for part in ref[2:].split("/"):
            part = unquote(part).replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node

    return resolve


def _deref_object(obj: Any, resolve, limit: int = 20) -> Any:
    while isinstance(obj, dict) and "$ref" in obj and limit > 0:
        obj = resolve(obj["$ref"])
        limit -= 1
    return obj


def _json_media(content: dict | None) -> dict | None:
    """Pick the JSON-ish media type from an OpenAPI 3 ``content`` map."""
    if not content:
        return None
    for media, body in content.items():
        if "json" in media:
            return body
    return next(iter(content.values()))


def operations(doc: dict) -> dict[str, Operation]:
    resolve = make_resolver(doc)
    swagger2 = str(doc.get("swagger", "")).startswith("2")
    ops: dict[str, Operation] = {}

    for path, item in (doc.get("paths") or {}).items():
        item = _deref_object(item, resolve) or {}
        shared = item.get("parameters", [])
        for method in HTTP_METHODS:
            if method not in item:
                continue
            raw = _deref_object(item[method], resolve) or {}
            op = Operation(method=method, path=path)

            for p in list(shared) + list(raw.get("parameters", [])):
                p = _deref_object(p, resolve) or {}
                loc = p.get("in", "query")
                name = p.get("name", "?")
                if swagger2 and loc == "body":
                    op.request = flatten(p.get("schema", {}), resolve, expand_refs=False)
                    op.request_required = bool(p.get("required"))
                    continue
                if swagger2 and loc == "formData":
                    continue
                schema = p.get("schema") if "schema" in p else {k: v for k, v in p.items() if k in ("type", "enum", "format", "items")}
                op.params[(loc, name)] = Param(
                    name=name,
                    location=loc,
                    required=bool(p.get("required")) or loc == "path",
                    fields=flatten(schema or {}, resolve, expand_refs=False),
                )

            if not swagger2 and "requestBody" in raw:
                body = _deref_object(raw["requestBody"], resolve) or {}
                media = _json_media(body.get("content"))
                if media is not None:
                    op.request = flatten(media.get("schema", {}), resolve, expand_refs=False)
                    op.request_required = bool(body.get("required"))

            for status, resp in (raw.get("responses") or {}).items():
                resp = _deref_object(resp, resolve) or {}
                if swagger2:
                    schema = resp.get("schema")
                else:
                    media = _json_media(resp.get("content"))
                    schema = media.get("schema") if media else None
                if schema is not None:
                    op.responses[str(status)] = flatten(schema, resolve, expand_refs=False)
                else:
                    op.responses[str(status)] = {}

            ops[op.key] = op
    return ops


def is_success(status: str) -> bool:
    return status.startswith("2") or status.upper() == "2XX" or status == "default"


SCHEMA_PREFIXES = ("#/components/schemas/", "#/definitions/", "#/x-apimend-bundled/")
_SCHEMA_KEYS = {"type", "properties", "items", "allOf", "anyOf", "oneOf", "enum", "additionalProperties", "$ref"}
_NOT_SCHEMA_KEYS = {"content", "in", "responses", "parameters", "requestBody", "get", "post", "put", "patch", "delete"}


def named_schemas(doc: dict) -> dict[str, Any]:
    """Shared schemas: ``components.schemas``, Swagger 2 ``definitions``, and schemas
    bundled in from other files."""
    out = dict((doc.get("components") or {}).get("schemas") or {})
    out.update(doc.get("definitions") or {})
    for name, node in (doc.get("x-apimend-bundled") or {}).items():
        if isinstance(node, dict) and node.keys() & _SCHEMA_KEYS and not node.keys() & _NOT_SCHEMA_KEYS:
            out.setdefault(name, node)
    return out


def _refs_in(node: Any) -> Iterator[str]:
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            ref = n.get("$ref")
            if isinstance(ref, str):
                yield ref
            stack.extend(v for k, v in n.items() if k != "$ref")
        elif isinstance(n, list):
            stack.extend(n)


def schema_usage(doc: dict) -> tuple[set[str], set[str]]:
    """Names of shared schemas reachable from requests and from responses."""
    resolve = make_resolver(doc)
    swagger2 = str(doc.get("swagger", "")).startswith("2")
    req_roots: list[Any] = []
    resp_roots: list[Any] = []
    for item in (doc.get("paths") or {}).values():
        item = _deref_object(item, resolve) or {}
        for method in HTTP_METHODS:
            raw = item.get(method)
            if not isinstance(raw, dict):
                continue
            req_roots.append(list(item.get("parameters", [])) + list(raw.get("parameters", [])))
            if not swagger2:
                req_roots.append(raw.get("requestBody"))
            resp_roots.append(raw.get("responses"))

    def reach(roots: list[Any]) -> set[str]:
        names: set[str] = set()
        seen: set[str] = set()
        queue = [r for root in roots for r in _refs_in(root)]
        while queue:
            ref = queue.pop()
            if ref in seen:
                continue
            seen.add(ref)
            for prefix in SCHEMA_PREFIXES:
                if ref.startswith(prefix):
                    names.add(ref[len(prefix):])
            queue.extend(_refs_in(resolve(ref)))
        return names

    return reach(req_roots), reach(resp_roots)
