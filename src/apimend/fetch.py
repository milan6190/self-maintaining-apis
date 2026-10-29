"""Loading specs and responses from URLs or files (standard library only)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from . import __version__

USER_AGENT = f"apimend/{__version__} (+https://github.com/milan6190/self-maintaining-apis)"
MAX_BYTES = 50 * 1024 * 1024


class FetchError(RuntimeError):
    pass


def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", source))


def http(url: str, *, method: str = "GET", headers: dict | None = None, body: Any = None,
         timeout: float = 30.0) -> str:
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, application/yaml;q=0.9, */*;q=0.5"}
    hdrs.update(headers or {})
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        raise FetchError(f"{method} {url} returned HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"{method} {url} failed: {getattr(e, 'reason', e)}") from e
    if len(raw) > MAX_BYTES:
        raise FetchError(f"{url} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return raw.decode(charset, errors="replace")


def parse_document(text: str, source: str = "") -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise FetchError(f"{source or 'document'} is neither JSON nor YAML: {e}") from e


def load_document(source: str, headers: dict | None = None, timeout: float = 30.0) -> Any:
    if is_url(source):
        text = http(source, headers=headers, timeout=timeout)
    else:
        p = Path(source)
        if not p.exists():
            raise FetchError(f"{source} not found")
        text = p.read_text(encoding="utf-8")
    doc = parse_document(text, source)
    if not isinstance(doc, dict):
        raise FetchError(f"{source} is not an OpenAPI document")
    return doc


def fetch_json(url: str, *, method: str = "GET", headers: dict | None = None, body: Any = None,
               timeout: float = 30.0) -> Any:
    text = http(url, method=method, headers=headers, body=body, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"{method} {url} did not return JSON") from e
