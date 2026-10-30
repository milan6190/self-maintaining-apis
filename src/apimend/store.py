"""On-disk layout under the store directory (default ``.apimend``)::

    baseline/<api>/spec.json       (spec.json.gz when larger than 1 MB)
    baseline/<api>/samples/<endpoint>/0001.json
    current/<api>/...              same layout, refreshed by `apimend check`
    plans/<api>/<endpoint>.json    repair plans written by `apimend heal`
    graphql-fixes/<api>.json       GraphQL operation edits (+ a .patch to review)
    report.json, report.md         the last check

Commit ``baseline/`` and ``plans/`` so drift is reviewed like code.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path
from typing import Any

BASELINE, CURRENT = "baseline", "current"
GZIP_ABOVE = 1024 * 1024


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, kind: str, api: str) -> Path:
        return self.root / kind / api

    # specs
    def spec_path(self, kind: str, api: str) -> Path:
        return self._dir(kind, api) / "spec.json"

    def save_spec(self, kind: str, api: str, doc: dict) -> Path:
        """Readable JSON for small specs; gzipped compact JSON above 1 MB (Stripe's 8 MB spec -> ~0.5 MB)."""
        p = self.spec_path(kind, api)
        gz = p.with_name(p.name + ".gz")
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
        if len(text) <= GZIP_ABOVE:
            p.write_text(text, encoding="utf-8")
            gz.unlink(missing_ok=True)
            return p
        compact = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()
        gz.write_bytes(gzip.compress(compact, compresslevel=6, mtime=0))
        p.unlink(missing_ok=True)
        return gz

    def load_spec(self, kind: str, api: str) -> dict | None:
        p = self.spec_path(kind, api)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        gz = p.with_name(p.name + ".gz")
        if gz.exists():
            return json.loads(gzip.decompress(gz.read_bytes()))
        return None

    # samples
    def samples_dir(self, kind: str, api: str, endpoint: str) -> Path:
        return self._dir(kind, api) / "samples" / endpoint

    def save_samples(self, kind: str, api: str, endpoint: str, samples: list[Any], append: bool = False) -> int:
        d = self.samples_dir(kind, api, endpoint)
        if d.exists() and not append:
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
        start = len(list(d.glob("*.json")))
        for i, s in enumerate(samples, start=start + 1):
            (d / f"{i:04d}.json").write_text(json.dumps(s, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return start + len(samples)

    def load_samples(self, kind: str, api: str, endpoint: str) -> list[Any]:
        d = self.samples_dir(kind, api, endpoint)
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.exists() else []

    def has_baseline(self, api: str) -> bool:
        return self._dir(BASELINE, api).exists()

    def accept(self, api: str) -> None:
        """Promote current to baseline."""
        cur, base = self._dir(CURRENT, api), self._dir(BASELINE, api)
        if not cur.exists():
            raise FileNotFoundError(f"no current data for {api}; run `apimend check` first")
        if base.exists():
            shutil.rmtree(base)
        shutil.copytree(cur, base)

    # plans
    def plan_path(self, api: str, endpoint: str) -> Path:
        return self.root / "plans" / api / f"{endpoint}.json"

    def save_plan(self, api: str, endpoint: str, plan: dict) -> Path:
        p = self.plan_path(api, endpoint)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        return p

    def plans(self) -> list[Path]:
        d = self.root / "plans"
        return sorted(d.glob("*/*.json")) if d.exists() else []

    def remove_plan(self, api: str, endpoint: str) -> None:
        p = self.plan_path(api, endpoint)
        if p.exists():
            p.unlink()

    # GraphQL operation fixes
    def gql_fix_path(self, api: str) -> Path:
        return self.root / "graphql-fixes" / f"{api}.json"

    def save_gql_fix(self, api: str, plan: dict, patch: str) -> Path:
        p = self.gql_fix_path(api)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        p.with_suffix(".patch").write_text(patch, encoding="utf-8")
        return p

    def load_gql_fix(self, api: str) -> dict | None:
        p = self.gql_fix_path(api)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def remove_gql_fix(self, api: str) -> None:
        for p in (self.gql_fix_path(api), self.gql_fix_path(api).with_suffix(".patch")):
            if p.exists():
                p.unlink()

    # report
    def write_report(self, data: dict, markdown: str) -> tuple[Path, Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        j, m = self.root / "report.json", self.root / "report.md"
        j.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        m.write_text(markdown, encoding="utf-8")
        return j, m

    def load_report(self) -> dict | None:
        p = self.root / "report.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
