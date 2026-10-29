"""Check payloads against the shape consumers were built for."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .heal import apply_plan
from .schema import FieldMap, flatten, infer, parent


def _accepts(expected: frozenset[str], actual: str) -> bool:
    return actual in expected or (actual == "integer" and "number" in expected)


def validate(payload: Any, baseline: FieldMap) -> list[str]:
    """Return human-readable violations of ``baseline`` found in ``payload``."""
    observed = flatten(infer([payload]))
    errors: list[str] = []
    for path, want in sorted(baseline.items()):
        got = observed.get(path)
        par = parent(path)
        if got is None:
            # An empty list has no item paths, and a missing parent is reported on its own.
            if (want.required and par is not None and not path.endswith("[]")
                    and par in observed and "object" in observed[par].types):
                errors.append(f"missing `{path}`")
            continue
        if want.required and not got.required:
            errors.append(f"`{path}` is missing from some items")
        if want.types:
            bad = [t for t in got.types if not _accepts(want.types, t)]
            if bad:
                errors.append(f"`{path}` is {'|'.join(sorted(got.types))}, expected {'|'.join(sorted(want.types))}")
    return errors


@dataclass
class VerifyResult:
    api: str
    endpoint: str
    samples: int = 0
    passed: int = 0
    problems: dict[str, int] = field(default_factory=dict)  # problem -> samples affected

    @property
    def ok(self) -> bool:
        return self.samples > 0 and self.passed == self.samples

    @property
    def errors(self) -> list[str]:
        return [f"{p} ({n}/{self.samples} samples)" for p, n in self.problems.items()]


def verify_plan(plan: dict, samples: list[Any], baseline: FieldMap) -> VerifyResult:
    """Replay recorded samples through ``plan`` and validate the result."""
    res = VerifyResult(plan.get("api", ""), plan.get("endpoint", ""))
    for sample in samples:
        res.samples += 1
        healed = apply_plan(plan, sample)
        problems = validate(healed, baseline)
        for p in problems:
            res.problems[p] = res.problems.get(p, 0) + 1
        if not problems:
            res.passed += 1
    return res
