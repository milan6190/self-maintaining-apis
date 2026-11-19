"""MCP servers: read the tools an agent can call, and grade how they change.

An AI agent consumes an MCP server the way an app consumes an API, except the
contract is read by a model at run time. A tool that disappears, gains a
required argument, or changes its output breaks the agent the same way a REST
change breaks code. Two changes matter only to agents:

* **Descriptions** are instructions to the model. A changed description changes
  agent behavior, and is also how a compromised server slips in new
  instructions after it was approved ("rug pull"). They are graded as warnings.
* **Annotations** (``readOnlyHint``, ``destructiveHint``, ``openWorldHint``,
  ``idempotentHint``) tell the host which tools are safe to call without asking.
  A tool that stops being read-only, or becomes destructive, is a warning.

Tool lists come from a saved ``tools/list`` result (a file or URL) or from a
running server: a command started over stdio, or a Streamable HTTP endpoint.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .diff import BREAKING, INFO, REQUEST, RESPONSE, WARNING, Change, diff_fields, name_similarity
from .fetch import USER_AGENT, FetchError, is_url
from .openapi import make_resolver
from .schema import ROOT, flatten

MARKER = "mcp-tools/1"
PROTOCOL_VERSION = "2025-06-18"
MAX_PAGES = 100
ANNOTATION_RISK = {  # annotation -> the value that means "riskier"
    "readOnlyHint": False,
    "destructiveHint": True,
    "openWorldHint": True,
    "idempotentHint": False,
}
# MCP's documented defaults when a hint is absent
ANNOTATION_DEFAULTS = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True, "idempotentHint": False}


def is_tools(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("x-apimend") == MARKER


def looks_like_tool_list(doc: Any) -> bool:
    """A ``tools/list`` result, a JSON-RPC response wrapping one, or a bare list of tools."""
    tools = _raw_tools(doc)
    return tools is not None and all(isinstance(t, dict) and isinstance(t.get("name"), str) for t in tools)


def _raw_tools(doc: Any) -> list | None:
    if isinstance(doc, dict) and isinstance(doc.get("result"), dict):
        doc = doc["result"]
    if isinstance(doc, dict) and isinstance(doc.get("tools"), list):
        return doc["tools"]
    if isinstance(doc, list) and doc and all(isinstance(t, dict) and "inputSchema" in t for t in doc):
        return doc
    return None


def from_tool_list(doc: Any, server: dict | None = None) -> dict:
    """Normalize a tool list into the stored model, keyed by tool name."""
    if is_tools(doc):
        return doc
    raw = _raw_tools(doc)
    if raw is None:
        raise FetchError("not an MCP tool list (expected {\"tools\": [...]})")
    tools: dict[str, dict] = {}
    for t in raw:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str):
            continue
        entry: dict[str, Any] = {"inputSchema": t.get("inputSchema") or {"type": "object"}}
        for key in ("title", "description", "outputSchema", "annotations"):
            if t.get(key) is not None:
                entry[key] = t[key]
        tools[t["name"]] = entry
    out: dict[str, Any] = {"x-apimend": MARKER, "tools": tools}
    if server:
        out["server"] = server
    return out


# ------------------------------------------------------------------ live servers


def _request(id_: int, method: str, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _initialize(id_: int) -> dict:
    return _request(id_, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "apimend", "version": __version__},
    })


def _result(msg: dict, what: str) -> dict:
    if "error" in msg:
        err = msg["error"]
        raise FetchError(f"{what} failed: {err.get('message', err) if isinstance(err, dict) else err}")
    res = msg.get("result")
    if not isinstance(res, dict):
        raise FetchError(f"{what}: unexpected response")
    return res


def _list_all(call) -> list:
    tools: list = []
    cursor = None
    for _ in range(MAX_PAGES):
        res = call("tools/list", {"cursor": cursor} if cursor else {})
        tools.extend(res.get("tools") or [])
        cursor = res.get("nextCursor")
        if not cursor:
            break
    return tools


def _server_info(init: dict) -> dict:
    info = init.get("serverInfo") or {}
    out = {k: info[k] for k in ("name", "version") if isinstance(info.get(k), str)}
    if isinstance(init.get("protocolVersion"), str):
        out["protocolVersion"] = init["protocolVersion"]
    return out


def list_tools_stdio(command: str | list[str], env: dict | None = None, cwd: str | None = None,
                     timeout: float = 30.0) -> dict:
    """Start an MCP server over stdio, list its tools, and stop it."""
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv:
        raise FetchError("empty MCP command")
    full_env = {**os.environ, **(env or {})}
    stderr = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                env=full_env, cwd=cwd)
    except OSError as e:
        stderr.close()
        raise FetchError(f"could not start `{argv[0]}`: {e}") from e

    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        assert proc.stdout
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(msg: dict) -> None:
        assert proc.stdin
        try:
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise FetchError(f"the server exited early{_tail(stderr)}") from e

    def wait_for(id_: int, what: str) -> dict:
        while True:
            try:
                raw = lines.get(timeout=timeout)
            except queue.Empty:
                raise FetchError(f"{what}: no answer within {timeout:g}s{_tail(stderr)}") from None
            if raw is None:
                raise FetchError(f"{what}: the server exited (code {proc.poll()}){_tail(stderr)}")
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # servers sometimes print banners to stdout
            if isinstance(msg, dict) and msg.get("id") == id_:
                return _result(msg, what)

    counter = iter(range(1, 10_000))
    try:
        n = next(counter)
        send(_initialize(n))
        init = wait_for(n, "initialize")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        def call(method: str, params: dict) -> dict:
            i = next(counter)
            send(_request(i, method, params))
            return wait_for(i, method)

        return from_tool_list({"tools": _list_all(call)}, _server_info(init))
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        stderr.close()


def _tail(f, limit: int = 400) -> str:
    try:
        f.seek(0)
        text = f.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""
    if not text:
        return ""
    return ": " + text[-limit:].replace("\n", " | ")


def _parse_http_body(body: str, content_type: str, id_: int) -> dict | None:
    if "text/event-stream" in content_type:
        for event in body.split("\n\n"):
            data = "\n".join(ln[5:].lstrip() for ln in event.splitlines() if ln.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == id_:
                return msg
        return None
    if not body.strip():
        return None
    try:
        msg = json.loads(body)
    except json.JSONDecodeError as e:
        raise FetchError("the MCP endpoint did not return JSON") from e
    if isinstance(msg, list):
        msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == id_), None)
    return msg if isinstance(msg, dict) else None


def list_tools_http(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    """List the tools of a Streamable HTTP MCP server."""
    session: dict[str, str] = {}

    def post(msg: dict) -> dict | None:
        hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", **(headers or {}), **session}
        req = urllib.request.Request(url, data=json.dumps(msg).encode(), method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    session["Mcp-Session-Id"] = sid
                body = resp.read(50 * 1024 * 1024).decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise FetchError(f"POST {url} returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"POST {url} failed: {getattr(e, 'reason', e)}") from e
        if "id" not in msg:
            return None
        return _parse_http_body(body, ctype, msg["id"])

    counter = iter(range(1, 10_000))
    n = next(counter)
    msg = post(_initialize(n))
    if msg is None:
        raise FetchError(f"{url}: no answer to initialize")
    init = _result(msg, "initialize")
    if isinstance(init.get("protocolVersion"), str):
        session["MCP-Protocol-Version"] = init["protocolVersion"]
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(method: str, params: dict) -> dict:
        i = next(counter)
        m = post(_request(i, method, params))
        if m is None:
            raise FetchError(f"{url}: no answer to {method}")
        return _result(m, method)

    return from_tool_list({"tools": _list_all(call)}, _server_info(init))


def load_server(source: str | list[str], headers: dict | None = None, env: dict | None = None,
                cwd: str | None = None, timeout: float = 30.0) -> dict:
    """``source`` is an ``http(s)://`` MCP endpoint or a command that starts a stdio server."""
    if isinstance(source, str) and is_url(source):
        return list_tools_http(source, headers=headers, timeout=timeout)
    return list_tools_stdio(source, env=env, cwd=cwd, timeout=timeout)


# ------------------------------------------------------------------------- diff


def _schema_fields(schema: Any):
    schema = schema if isinstance(schema, dict) else {}
    return flatten(schema, make_resolver(schema))


def _shape_key(tool: dict) -> str:
    return json.dumps(tool.get("inputSchema"), sort_keys=True)


def _annotations(tool: dict) -> dict:
    ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    return {k: ann.get(k, ANNOTATION_DEFAULTS[k]) for k in ANNOTATION_RISK}


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _match_tool_renames(removed: list[str], added: list[str], old: dict, new: dict) -> list[tuple[str, str, float]]:
    pairs = []
    for o in removed:
        for n in added:
            score = name_similarity(o, n)
            # an identical, non-trivial argument schema or description is strong evidence
            props = (old[o].get("inputSchema") or {}).get("properties")
            if props and _shape_key(old[o]) == _shape_key(new[n]):
                score = max(score, 0.5) + 0.3
            desc = (old[o].get("description") or "").strip()
            if desc and desc == (new[n].get("description") or "").strip():
                score = max(score, 0.5) + 0.3
            if score >= 0.75:
                pairs.append((min(score, 1.0), o, n))
    pairs.sort(reverse=True)
    used_o, used_n, out = set(), set(), []
    for score, o, n in pairs:
        if o not in used_o and n not in used_n:
            used_o.add(o)
            used_n.add(n)
            out.append((o, n, score))
    return out


def diff_tools(old_doc: dict, new_doc: dict, prefix: str = "") -> list[Change]:
    """Compare two tool lists, graded from the agent's side.

    Tool arguments are what the agent *sends* (request rules); structured
    output is what it *reads* (response rules).
    """
    old, new = old_doc.get("tools") or {}, new_doc.get("tools") or {}
    changes: list[Change] = []

    removed = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    renames = _match_tool_renames(removed, added, old, new)
    renamed_old = {o for o, _, _ in renames}
    renamed_new = {n for _, n, _ in renames}

    for o, n, score in renames:
        changes.append(Change(BREAKING, "tool-renamed", f"{prefix}tool {o}", ROOT,
                              f"tool `{o}` appears to be renamed to `{n}` (confidence {score:.2f}); "
                              "prompts, allowlists and agent configs that name it need updating",
                              old=o, new=n))
    for name in removed:
        if name not in renamed_old:
            changes.append(Change(BREAKING, "tool-removed", f"{prefix}tool {name}", ROOT,
                                  f"tool `{name}` was removed"))
    for name in added:
        if name not in renamed_new:
            changes.append(Change(INFO, "tool-added", f"{prefix}tool {name}", ROOT,
                                  f"new tool `{name}`: "
                                  + _short(new[name].get("description") or "no description")))

    pairs = [(n, n) for n in sorted(set(old) & set(new))] + [(o, n) for o, n, _ in renames]
    for o_name, n_name in pairs:
        o, n = old[o_name], new[n_name]
        loc = f"{prefix}tool {o_name}"

        changes.extend(diff_fields(_schema_fields(o.get("inputSchema")), _schema_fields(n.get("inputSchema")),
                                   direction=REQUEST, location=f"{loc} input").changes)

        oo, no = o.get("outputSchema"), n.get("outputSchema")
        if oo and no:
            changes.extend(diff_fields(_schema_fields(oo), _schema_fields(no),
                                       direction=RESPONSE, location=f"{loc} output").changes)
        elif oo and not no:
            changes.append(Change(WARNING, "output-schema-removed", f"{loc} output", ROOT,
                                  "the tool no longer declares an output schema; structured results may stop"))
        elif no and not oo:
            changes.append(Change(INFO, "output-schema-added", f"{loc} output", ROOT,
                                  "the tool now declares an output schema"))

        od, nd = (o.get("description") or "").strip(), (n.get("description") or "").strip()
        if od != nd:
            changes.append(Change(WARNING, "tool-description-changed", loc, ROOT,
                                  "the description the model reads changed; review it before agents use it: "
                                  f"“{_short(od, 60)}” → “{_short(nd, 60)}”", old=od, new=nd))

        oa, na = _annotations(o), _annotations(n)
        for key, risky in ANNOTATION_RISK.items():
            if oa[key] == na[key]:
                continue
            if na[key] == risky:
                changes.append(Change(WARNING, "tool-permissions-widened", loc, ROOT,
                                      f"`{key}` changed {_b(oa[key])} → {_b(na[key])}: the tool now declares it is "
                                      f"{_RISK_WORDS[key]}; hosts that auto-approve it should re-check",
                                      old=oa[key], new=na[key]))
            else:
                changes.append(Change(INFO, "tool-permissions-narrowed", loc, ROOT,
                                      f"`{key}` changed {_b(oa[key])} → {_b(na[key])}", old=oa[key], new=na[key]))

    os_, ns = old_doc.get("server") or {}, new_doc.get("server") or {}
    if os_.get("version") and ns.get("version") and os_["version"] != ns["version"]:
        changes.append(Change(INFO, "server-version-changed", f"{prefix}server", ROOT,
                              f"server version {os_['version']} → {ns['version']}"))
    if os_.get("protocolVersion") and ns.get("protocolVersion") and os_["protocolVersion"] != ns["protocolVersion"]:
        changes.append(Change(WARNING, "protocol-version-changed", f"{prefix}server", ROOT,
                              f"MCP protocol version {os_['protocolVersion']} → {ns['protocolVersion']}"))
    return changes


def _b(v: Any) -> str:
    return json.dumps(v) if isinstance(v, bool) else str(v)


_RISK_WORDS = {
    "readOnlyHint": "able to modify its environment",
    "destructiveHint": "possibly destructive",
    "openWorldHint": "reaching outside systems",
    "idempotentHint": "not safe to retry",
}
