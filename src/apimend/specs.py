"""Load any supported API description into a comparable document.

* OpenAPI 3.x / Swagger 2.0 (JSON or YAML, one file or many): returned as a
  single document; ``$ref``s to other files or URLs are bundled in under
  ``x-apimend-bundled`` so later diffs work offline.
* GraphQL SDL (``.graphql``/``.gql``/``.graphqls``), an introspection result
  (JSON), or a live endpoint: returned as the model in :mod:`apimend.gql.model`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin


from .fetch import FetchError, http, is_url, parse_document
from .gql.model import from_introspection, from_sdl, introspect, is_introspection, is_model
from .gql.parser import GraphQLSyntaxError, document_kind

GRAPHQL_FILE_EXTS = (".graphql", ".gql", ".graphqls", ".sdl")
BUNDLE_KEY = "x-apimend-bundled"
MAX_DOCUMENTS = 10000


def kind_of(doc: Any) -> str:
    """'graphql', 'openapi' or 'unknown'."""
    if is_model(doc):
        return "graphql"
    if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc):
        return "openapi"
    return "unknown"


def _read(source: str, headers: dict | None, timeout: float) -> str:
    if is_url(source):
        return http(source, headers=headers, timeout=timeout)
    p = Path(source)
    if not p.exists():
        raise FetchError(f"{source} not found")
    return p.read_text(encoding="utf-8")


def load_spec(source: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """Load an OpenAPI, Swagger or GraphQL description from a file or URL."""
    text = _read(source, headers, timeout)
    path_part = source.split("?", 1)[0].lower()
    if path_part.endswith(GRAPHQL_FILE_EXTS) or (not text.lstrip().startswith(("{", "[")) and
                                                  document_kind(text) == "schema"):
        try:
            return from_sdl(text)
        except GraphQLSyntaxError as e:
            raise FetchError(f"{source}: invalid GraphQL schema: {e}") from e
    doc = parse_document(text, source)
    if is_introspection(doc):
        return from_introspection(doc)
    if is_model(doc):
        return doc
    if not isinstance(doc, dict) or kind_of(doc) != "openapi":
        raise FetchError(f"{source} is not an OpenAPI, Swagger or GraphQL document")
    return bundle(doc, source, headers=headers, timeout=timeout)


def load_graphql_endpoint(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    return introspect(url, headers=headers, timeout=timeout)


# ------------------------------------------------------------------- bundling


def _join(base: str, ref_loc: str) -> str:
    if is_url(ref_loc):
        return ref_loc
    if is_url(base):
        return urljoin(base, ref_loc)
    return os.path.normpath(os.path.join(os.path.dirname(base), unquote(ref_loc)))


def _pointer(doc: Any, fragment: str, where: str) -> Any:
    if not fragment or fragment == "/":
        return doc
    node = doc
    for part in fragment.lstrip("/").split("/"):
        part = unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise FetchError(f"cannot resolve #{fragment} in {where}")
    return node


def bundle(doc: dict, base: str, headers: dict | None = None, timeout: float = 30.0,
           loader: Callable[[str], Any] | None = None) -> dict:
    """Inline every ``$ref`` that points outside ``doc``.

    External targets are copied once into ``x-apimend-bundled`` and the refs are
    rewritten to point there. A target's own refs are resolved relative to the
    file it came from, including ``#/...`` refs inside that file.
    """
    root = base if is_url(base) else os.path.normpath(base)
    cache: dict[str, Any] = {root: doc}
    store: dict[str, Any] = {}
    keys: dict[tuple[str, str], str] = {}

    def load(loc: str) -> Any:
        if loc not in cache:
            if len(cache) >= MAX_DOCUMENTS:
                raise FetchError(f"more than {MAX_DOCUMENTS} referenced documents")
            if loader is not None:
                cache[loc] = loader(loc)
            else:
                cache[loc] = parse_document(_read(loc, headers if is_url(loc) else None, timeout), loc)
        return cache[loc]

    def key_for(loc: str, frag: str) -> str:
        last = frag.rstrip("/").rsplit("/", 1)[-1] if frag else Path(loc.split("?")[0]).stem
        base_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", unquote(last)) or "ref"
        key, n = base_key, 2
        while key in store:
            key = f"{base_key}_{n}"
            n += 1
        return key

    def visit(node: Any, cur: str) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                loc, _, frag = ref.partition("#")
                loc = _join(cur, loc) if loc else cur
                if loc == root:
                    return {"$ref": "#" + frag} if len(node) == 1 else {**node, "$ref": "#" + frag}
                k = keys.get((loc, frag))
                if k is None:
                    k = key_for(loc, frag)
                    keys[(loc, frag)] = k
                    store[k] = {}
                    store[k] = visit(_pointer(load(loc), frag, loc), loc)
                return {"$ref": f"#/{BUNDLE_KEY}/{k}"}
            return {k: visit(v, cur) for k, v in node.items()}
        if isinstance(node, list):
            return [visit(v, cur) for v in node]
        return node

    out = visit(doc, root)
    if store:
        out[BUNDLE_KEY] = store
    return out

