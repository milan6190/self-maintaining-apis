"""``apimend.yaml``: which APIs to watch and how to sample them."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = "apimend.yaml"
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(ValueError):
    pass


def expand_env(value: Any) -> Any:
    """Replace ``${VAR}`` and ``${VAR:-default}`` in every string."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")
        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


@dataclass
class Endpoint:
    name: str
    url: str | None = None
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    samples: str | None = None  # glob of local JSON files, used instead of url
    timeout: float = 30.0

    def sample_files(self, base: Path) -> list[Path]:
        if not self.samples:
            return []
        pattern = self.samples if os.path.isabs(self.samples) else str(base / self.samples)
        return [Path(p) for p in sorted(glob.glob(pattern))]


@dataclass
class Api:
    name: str
    spec: str | None = None
    headers: dict[str, str] = field(default_factory=dict)  # for fetching the spec
    endpoints: list[Endpoint] = field(default_factory=list)


@dataclass
class Config:
    path: Path
    store: Path
    apis: list[Api]
    fail_on: str = "breaking"

    @property
    def base(self) -> Path:
        return self.path.parent

    def resolve(self, source: str) -> str:
        """URLs pass through; relative files are relative to the config file."""
        if re.match(r"^https?://", source) or os.path.isabs(source):
            return source
        return str(self.base / source)

    def api(self, name: str) -> Api:
        for a in self.apis:
            if a.name == name:
                return a
        raise ConfigError(f"no api named {name!r} in {self.path}")


def _check_name(kind: str, name: Any) -> str:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ConfigError(f"{kind} name {name!r} must use letters, digits, '.', '_' or '-'")
    return name


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"{path} not found (run `apimend init` to create one)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = expand_env(raw)
    if not isinstance(raw, dict) or not isinstance(raw.get("apis"), list) or not raw["apis"]:
        raise ConfigError(f"{path}: expected a non-empty `apis:` list")

    apis: list[Api] = []
    seen: set[str] = set()
    for i, a in enumerate(raw["apis"]):
        if not isinstance(a, dict):
            raise ConfigError(f"apis[{i}] must be a mapping")
        name = _check_name("api", a.get("name"))
        if name in seen:
            raise ConfigError(f"duplicate api name {name!r}")
        seen.add(name)
        eps: list[Endpoint] = []
        ep_seen: set[str] = set()
        for j, e in enumerate(a.get("endpoints") or []):
            if not isinstance(e, dict):
                raise ConfigError(f"{name}.endpoints[{j}] must be a mapping")
            ename = _check_name("endpoint", e.get("name"))
            if ename in ep_seen:
                raise ConfigError(f"duplicate endpoint {ename!r} in {name}")
            ep_seen.add(ename)
            if not e.get("url") and not e.get("samples"):
                raise ConfigError(f"{name}.{ename}: set `url` or `samples`")
            eps.append(Endpoint(
                name=ename,
                url=e.get("url"),
                method=str(e.get("method", "GET")).upper(),
                headers={str(k): str(v) for k, v in (e.get("headers") or {}).items()},
                body=e.get("body"),
                samples=e.get("samples"),
                timeout=float(e.get("timeout", 30)),
            ))
        if not a.get("spec") and not eps:
            raise ConfigError(f"{name}: set a `spec`, `endpoints`, or both")
        apis.append(Api(
            name=name,
            spec=a.get("spec"),
            headers={str(k): str(v) for k, v in (a.get("headers") or {}).items()},
            endpoints=eps,
        ))

    fail_on = raw.get("fail_on", "breaking")
    if fail_on not in ("breaking", "warning", "never"):
        raise ConfigError("fail_on must be breaking, warning or never")
    store = Path(raw.get("store", ".apimend"))
    if not store.is_absolute():
        store = path.parent / store
    return Config(path=path, store=store, apis=apis, fail_on=fail_on)


STARTER = """\
# apimend configuration. Docs: https://github.com/milan6190/self-maintaining-apis
store: .apimend          # where baselines, current samples, plans and reports live
fail_on: breaking        # exit non-zero on: breaking | warning | never

apis:
  - name: petstore
    # Optional: an OpenAPI 3.x or Swagger 2.0 document (URL or file, JSON or YAML)
    spec: https://petstore3.swagger.io/api/v3/openapi.json

    # Optional: live calls whose real responses are compared over time.
    endpoints:
      - name: find-available-pets
        url: https://petstore3.swagger.io/api/v3/pet/findByStatus?status=available
        headers:
          Accept: application/json
          # Authorization: "Bearer ${PETSTORE_TOKEN}"   # ${VAR} and ${VAR:-default} are expanded

      # Or point at recorded responses instead of calling the API:
      # - name: recorded
      #   samples: ./samples/*.json
"""
