"""Combine many ``report.json`` files into one view for a platform team.

Each repository runs ``apimend check`` on its own (usually the nightly job) and
uploads ``.apimend/report.json``. ``apimend rollup`` reads any number of them
and answers the questions a platform or integration lead asks every week:
which APIs are breaking whom, who owns the fix, and how much is waived.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .diff import BREAKING, INFO, WARNING


@dataclass
class Row:
    repo: str
    api: str
    owner: str
    breaking: int = 0
    warning: int = 0
    info: int = 0
    waived: int = 0
    errors: int = 0
    generated_at: str = ""
    top: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.breaking:
            return "breaking"
        if self.warning:
            return "warning"
        return "ok"


def _repo_name(path: Path) -> str:
    """``repos/billing/.apimend/report.json`` -> ``billing``."""
    parts = path.resolve().parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].startswith(".apimend"):
            return parts[i - 1]
    return path.parent.name if path.name == "report.json" else path.stem


def load_rows(paths: list[Path]) -> tuple[list[Row], list[str]]:
    rows, problems = [], []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{p}: {e}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("apis"), list):
            problems.append(f"{p}: not an apimend report")
            continue
        repo = _repo_name(p)
        for a in data["apis"]:
            row = Row(repo, a.get("name", "?"), a.get("owner") or "unowned",
                      errors=len(a.get("errors") or []), generated_at=data.get("generated_at", ""))
            for c in a.get("changes") or []:
                if c.get("waived"):
                    row.waived += 1
                    continue
                sev = c.get("severity")
                if sev == BREAKING:
                    row.breaking += 1
                    if len(row.top) < 3:
                        row.top.append(c.get("message", ""))
                elif sev == WARNING:
                    row.warning += 1
                elif sev == INFO:
                    row.info += 1
            rows.append(row)
    order = {"error": 0, "breaking": 1, "warning": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r.status], -r.breaking, -r.warning, r.repo, r.api))
    return rows, problems


def to_dict(rows: list[Row], problems: list[str]) -> dict:
    owners: dict[str, dict] = defaultdict(lambda: {"apis": 0, "breaking": 0, "warning": 0, "waived": 0})
    for r in rows:
        o = owners[r.owner]
        o["apis"] += 1
        o["breaking"] += r.breaking
        o["warning"] += r.warning
        o["waived"] += r.waived
    return {
        "summary": {
            "repos": len({r.repo for r in rows}), "apis": len(rows),
            "breaking_apis": sum(1 for r in rows if r.status == "breaking"),
            "healthy_apis": sum(1 for r in rows if r.status == "ok"),
            "breaking": sum(r.breaking for r in rows), "warning": sum(r.warning for r in rows),
            "waived": sum(r.waived for r in rows), "errors": sum(r.errors for r in rows),
        },
        "owners": dict(sorted(owners.items())),
        "apis": [{**r.__dict__, "status": r.status} for r in rows],
        "problems": problems,
    }


ICON = {"error": "❗", "breaking": "🔴", "warning": "🟡", "ok": "✅"}


def to_markdown(rows: list[Row], problems: list[str]) -> str:
    d = to_dict(rows, problems)
    s = d["summary"]
    healthy = f"{100 * s['healthy_apis'] // s['apis']}%" if s["apis"] else "n/a"
    lines = [
        "# API drift across the organization", "",
        f"**{s['apis']} API(s) in {s['repos']} repo(s)** · {healthy} healthy · "
        f"{s['breaking_apis']} with breaking drift · {s['waived']} change(s) waived"
        + (f" · {s['errors']} error(s)" if s["errors"] else ""), "",
        "## By owner", "",
        "| Owner | APIs | Breaking | Warnings | Waived |", "| --- | --- | --- | --- | --- |",
    ]
    for owner, o in d["owners"].items():
        lines.append(f"| {owner} | {o['apis']} | {o['breaking']} | {o['warning']} | {o['waived']} |")
    lines += ["", "## By API", "", "| | Repo | API | Owner | Breaking | Warnings | Waived | Top issue |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        top = (r.top[0] if r.top else "").replace("|", "\\|")
        if len(top) > 100:
            top = top[:99] + "…"
        lines.append(f"| {ICON[r.status]} | {r.repo} | {r.api} | {r.owner} | {r.breaking} | {r.warning} | "
                     f"{r.waived} | {top} |")
    if problems:
        lines += ["", "## Reports that could not be read", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"
