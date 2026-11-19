"""Deprecation signals that providers send before they break you.

Many APIs announce removals in the response itself, long before a spec
changes:

* ``Deprecation`` (RFC 9745): the resource is deprecated, as a structured date
  (``@1735689600``); older drafts send ``true`` or an HTTP date.
* ``Sunset`` (RFC 8594): the HTTP date after which it may stop working.
* ``Link`` with ``rel="deprecation"`` or ``rel="sunset"``: where to read more.
* ``Warning: 299 - "..."``: a free-text deprecation notice (common on older APIs).

apimend keeps these headers for every live endpoint it calls and reports them
on each check while they are present, because they describe a deadline, not a
diff. A sunset date that has passed is breaking; one within ``SOON_DAYS`` says
how many days are left.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

from .diff import BREAKING, WARNING, Change

KEEP = ("deprecation", "sunset", "link", "warning")
SOON_DAYS = 30
_LINK = re.compile(r'<([^>]+)>\s*;([^,]*)')


def keep_headers(headers) -> dict[str, str]:
    """The headers worth storing, lower-cased; ``Link`` only when it is about deprecation."""
    out: dict[str, str] = {}
    for k, v in (headers.items() if hasattr(headers, "items") else headers):
        key = k.lower()
        if key not in KEEP:
            continue
        if key == "link" and not re.search(r'rel\s*=\s*"?[^"]*\b(deprecation|sunset|successor-version)\b', v, re.I):
            continue
        if key == "warning" and not re.match(r"\s*299\b", v):
            continue
        out[key] = f"{out[key]}, {v}" if key in out else v
    return out


def _parse_date(value: str) -> date | None:
    value = value.strip()
    m = re.match(r"^@(-?\d+)$", value)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).date()
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _links(value: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for url, params in _LINK.findall(value or ""):
        m = re.search(r'rel\s*=\s*"?([^";]+)"?', params)
        if m:
            for rel in m.group(1).split():
                out.setdefault(rel.lower(), url)
    return out


def signal_changes(endpoint: str, current: dict[str, str], baseline: dict[str, str] | None = None,
                   today: date | None = None) -> list[Change]:
    today = today or date.today()
    baseline = baseline or {}
    loc = f"{endpoint} (live)"
    links = _links(current.get("link"))
    more = links.get("deprecation") or links.get("sunset")
    info = f"; see {more}" if more else ""
    succ = f"; successor: {links['successor-version']}" if links.get("successor-version") else ""
    changes: list[Change] = []

    dep = current.get("deprecation")
    if dep and dep.strip().lower() != "false":
        when = _parse_date(dep)
        new = " (new since the baseline)" if "deprecation" not in baseline else ""
        if when and when > today:
            text = f"`{endpoint}` will be deprecated on {when.isoformat()}{new}"
        elif when:
            text = f"`{endpoint}` is deprecated since {when.isoformat()}{new}"
        else:
            text = f"`{endpoint}` is deprecated{new}"
        changes.append(Change(WARNING, "endpoint-deprecated", loc, "", text + info + succ,
                              new=when.isoformat() if when else dep))

    sunset = current.get("sunset")
    if sunset:
        when = _parse_date(sunset)
        if when is None:
            changes.append(Change(WARNING, "sunset-scheduled", loc, "",
                                  f"`{endpoint}` sends a Sunset header: {sunset}{info}", new=sunset))
        else:
            left = (when - today).days
            if left < 0:
                changes.append(Change(BREAKING, "sunset-passed", loc, "",
                                      f"`{endpoint}` passed its sunset date {when.isoformat()} "
                                      f"({-left} day(s) ago) and may stop working at any time{info}{succ}",
                                      new=when.isoformat()))
            else:
                soon = " — soon" if left <= SOON_DAYS else ""
                changes.append(Change(WARNING, "sunset-scheduled", loc, "",
                                      f"`{endpoint}` will be removed on {when.isoformat()} "
                                      f"({left} day(s) left{soon}){info}{succ}", new=when.isoformat()))

    warn = current.get("warning")
    if warn and not dep and not sunset:
        text = re.sub(r'^\s*299\s+\S+\s+', "", warn).strip().strip('"')
        changes.append(Change(WARNING, "deprecation-notice", loc, "", f"`{endpoint}` warns: {text}{info}"))
    return changes
