"""Waivers: accept a known change for a while, with a reason and an owner.

Teams adopt a drift gate only if they can say "yes, we know, we're on it"
without switching the gate off. A waiver matches changes by API, kind, location
and path (shell-style globs), records why, and stops applying on its expiry
date, so nothing is silenced forever::

    waivers:
      - api: billing
        kind: field-removed
        path: "$.data[].legacy_id"
        reason: "We stopped reading legacy_id (PR #412)"
        owner: billing-team
        expires: 2026-12-31
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from fnmatch import fnmatchcase
from typing import Any

from .diff import Change

MATCH_KEYS = ("api", "kind", "where", "path", "severity")


class WaiverError(ValueError):
    pass


@dataclass
class Waiver:
    reason: str
    api: str | None = None
    kind: str | None = None
    where: str | None = None  # glob on Change.location
    path: str | None = None
    severity: str | None = None
    owner: str | None = None
    expires: date | None = None
    index: int = 0
    matched: int = field(default=0, compare=False)

    def label(self) -> str:
        bits = [f"{k}={getattr(self, k)}" for k in MATCH_KEYS if getattr(self, k)]
        return f"waiver #{self.index + 1} ({', '.join(bits) or 'everything'})"

    def expired(self, today: date) -> bool:
        return self.expires is not None and self.expires < today

    def matches(self, api: str, c: Change) -> bool:
        for key, value in (("api", api), ("kind", c.kind), ("where", c.location), ("path", c.path),
                           ("severity", c.severity)):
            pattern = getattr(self, key)
            if pattern and not fnmatchcase(value or "", pattern):
                return False
        return True


def parse_waivers(raw: Any) -> list[Waiver]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WaiverError("`waivers` must be a list")
    out = []
    for i, w in enumerate(raw):
        if not isinstance(w, dict):
            raise WaiverError(f"waivers[{i}] must be a mapping")
        unknown = set(w) - {*MATCH_KEYS, "reason", "owner", "expires"}
        if unknown:
            raise WaiverError(f"waivers[{i}]: unknown key(s) {', '.join(sorted(unknown))}")
        reason = w.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise WaiverError(f"waivers[{i}]: a `reason` is required")
        if not any(w.get(k) for k in MATCH_KEYS):
            raise WaiverError(f"waivers[{i}]: set at least one of {', '.join(MATCH_KEYS)}")
        expires = w.get("expires")
        if expires is not None and not isinstance(expires, date):
            try:
                expires = date.fromisoformat(str(expires))
            except ValueError as e:
                raise WaiverError(f"waivers[{i}]: `expires` must be a date like 2026-12-31") from e
        out.append(Waiver(
            reason=reason.strip(), expires=expires, index=i,
            owner=str(w["owner"]) if w.get("owner") else None,
            **{k: str(w[k]) for k in MATCH_KEYS if w.get(k)},
        ))
    return out


def apply_waivers(waivers: list[Waiver], api: str, changes: list[Change], today: date | None = None) -> None:
    """Mark matching changes as waived (in place). Expired waivers don't apply."""
    today = today or date.today()
    for c in changes:
        for w in waivers:
            if w.expired(today) or not w.matches(api, c):
                continue
            w.matched += 1
            until = f" until {w.expires.isoformat()}" if w.expires else ""
            who = f", {w.owner}" if w.owner else ""
            c.waived = f"{w.reason}{who}{until}"
            break


def waiver_notes(waivers: list[Waiver], checked_apis: set[str], all_checked: bool,
                 today: date | None = None, soon_days: int = 14) -> list[str]:
    """Things a reviewer should know: expired, expiring soon, or no longer matching anything."""
    today = today or date.today()
    notes = []
    for w in waivers:
        if w.api and not any(fnmatchcase(a, w.api) for a in checked_apis):
            continue
        if w.expired(today):
            notes.append(f"{w.label()} expired on {w.expires.isoformat()}; the changes it covered count again. "
                         f"Reason was: {w.reason}")
        elif w.expires and (w.expires - today).days <= soon_days:
            notes.append(f"{w.label()} expires on {w.expires.isoformat()} ({(w.expires - today).days} day(s)).")
        elif not w.matched and all_checked:
            notes.append(f"{w.label()} no longer matches any change; it can be removed.")
    return notes
