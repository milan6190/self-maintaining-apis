"""Repair plans: turn a drifted payload back into the shape consumers expect.

A plan is plain JSON, so it can be reviewed in a pull request and applied at
runtime with :class:`Healer`::

    {
      "version": 1,
      "api": "petstore",
      "endpoint": "list-pets",
      "ops": [
        {"op": "rename", "parent": "$[]", "from": "petName", "to": "name"},
        {"op": "coerce", "path": "$[].age", "to": "integer"}
      ],
      "unresolved": [ ...changes that need a human... ]
    }

Ops run in order. Paths are written in the consumer's (old) shape, because
each rename has already been applied by the time later ops run.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .diff import BREAKING, WARNING, FieldDiff
from .schema import last_segment, parent, split_path

PLAN_VERSION = 1
_INT = re.compile(r"^[+-]?\d+$")
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def coerce_value(value: Any, to: str, strict: bool = False) -> Any:
    """Convert a scalar to ``to``. With ``strict`` a failed conversion raises."""
    if value is None:
        return None
    try:
        if to == "string":
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            if isinstance(value, (str, int, float)):
                return str(value)
        elif to == "integer":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, str) and _INT.match(value.strip()):
                return int(value.strip())
            if isinstance(value, str) and _NUM.match(value.strip()) and float(value).is_integer():
                return int(float(value))
        elif to == "number":
            if isinstance(value, bool):
                raise ValueError
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, str) and _NUM.match(value.strip()):
                f = float(value)
                return int(f) if _INT.match(value.strip()) else f
        elif to == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
                return value.strip().lower() in ("true", "1", "yes")
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
    except (ValueError, OverflowError):
        pass
    if strict:
        raise ValueError(f"cannot convert {value!r} to {to}")
    return value


def build_plan(fd: FieldDiff, *, api: str = "", endpoint: str = "") -> dict:
    ops: list[dict] = []
    for rn in fd.renames:
        ops.append({"op": "rename", "parent": rn.parent, "from": rn.new_name, "to": rn.old_name,
                    "confidence": rn.score})
    for co in fd.coercions:
        ops.append({"op": "coerce", "path": co.path, "from": co.from_type, "to": co.to_type})
    unresolved = [c.to_dict() for c in fd.changes
                  if c.severity in (BREAKING, WARNING) and not c.healable]
    return {
        "version": PLAN_VERSION,
        "api": api,
        "endpoint": endpoint,
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "ops": ops,
        "unresolved": unresolved,
    }


MANUAL_OPS = ("default", "drop")


def merge_manual_ops(previous: dict, fresh: dict) -> dict:
    """Keep hand-written ops (``default``/``drop`` or ``"manual": true``) when a plan is regenerated."""
    kept = [op for op in previous.get("ops", []) if op.get("op") in MANUAL_OPS or op.get("manual")]
    covered = {op.get("path") for op in kept}
    out = dict(fresh)
    out["ops"] = fresh["ops"] + [op for op in kept if op not in fresh["ops"]]
    out["unresolved"] = [u for u in fresh["unresolved"] if u.get("path") not in covered]
    return out


def _nodes(node: Any, segments: list[str]) -> Iterator[Any]:
    """Yield every value found at ``segments`` (``[]`` fans out over lists)."""
    if not segments:
        yield node
        return
    head, rest = segments[0], segments[1:]
    if head == "[]":
        if isinstance(node, list):
            for item in node:
                yield from _nodes(item, rest)
    elif isinstance(node, dict) and head in node:
        yield from _nodes(node[head], rest)


def apply_plan(plan: dict, payload: Any) -> Any:
    """Return a copy of ``payload`` rewritten into the shape the plan restores."""
    out = copy.deepcopy(payload)
    for op in plan.get("ops", []):
        kind = op.get("op")
        if kind == "rename":
            for container in _nodes(out, split_path(op["parent"])):
                if isinstance(container, dict) and op["from"] in container and op["to"] not in container:
                    container[op["to"]] = container.pop(op["from"])
        elif kind == "coerce":
            path, to = op["path"], op["to"]
            key = last_segment(path)
            par = parent(path)
            if par is None:
                out = coerce_value(out, to)
                continue
            for container in _nodes(out, split_path(par)):
                if key == "[]" and isinstance(container, list):
                    container[:] = [coerce_value(v, to) for v in container]
                elif isinstance(container, dict) and key in container:
                    container[key] = coerce_value(container[key], to)
        elif kind == "default":
            key = last_segment(op["path"])
            for container in _nodes(out, split_path(parent(op["path"]) or "$")):
                if isinstance(container, dict) and key not in container:
                    container[key] = copy.deepcopy(op.get("value"))
        elif kind == "drop":
            key = last_segment(op["path"])
            for container in _nodes(out, split_path(parent(op["path"]) or "$")):
                if isinstance(container, dict):
                    container.pop(key, None)
        else:
            raise ValueError(f"unknown plan op: {kind!r}")
    return out


class Healer:
    """Apply a saved plan to live responses.

    >>> healer = Healer.from_file(".apimend/plans/petstore/list-pets.json")
    >>> pets = healer(response.json())
    """

    def __init__(self, plan: dict):
        if plan.get("version", PLAN_VERSION) > PLAN_VERSION:
            raise ValueError("plan was written by a newer apimend; upgrade to apply it")
        self.plan = plan

    @classmethod
    def from_file(cls, path: str | Path) -> "Healer":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def ops(self) -> list[dict]:
        return self.plan.get("ops", [])

    def __call__(self, payload: Any) -> Any:
        return apply_plan(self.plan, payload)
