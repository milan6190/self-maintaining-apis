"""Command-line interface: ``apimend <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULT_CONFIG, STARTER, Api, Config, ConfigError, Endpoint, load_config
from .diff import BREAKING, RESPONSE, WARNING, Change, FieldDiff, diff_fields, diff_specs
from .fetch import FetchError, fetch_json
from .gql import check as gql_check
from .gql.operations import expand_patterns
from .heal import build_plan, merge_manual_ops
from .specs import kind_of, load_graphql_endpoint, load_spec
from .report import ApiResult, Report
from .schema import collect_values, flatten_samples, infer
from .store import BASELINE, CURRENT, Store
from .verify import verify_plan

EXIT_OK, EXIT_DRIFT, EXIT_ERROR = 0, 1, 2


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(f"apimend: {msg}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ data access


def collect_samples(cfg: Config, ep: Endpoint) -> list[Any]:
    if ep.samples:
        files = ep.sample_files(cfg.base)
        if not files:
            raise FetchError(f"{ep.name}: no files match {ep.samples}")
        out = []
        for f in files:
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except json.JSONDecodeError as e:
                raise FetchError(f"{f} is not valid JSON: {e}") from e
        return out
    assert ep.url
    return [fetch_json(ep.url, method=ep.method, headers=ep.headers, body=ep.body, timeout=ep.timeout)]


def pull(cfg: Config, store: Store, api: Api, kind: str, append: bool = False) -> list[str]:
    """Fetch the spec and samples for ``api`` into ``kind`` (baseline/current)."""
    errors: list[str] = []
    if api.has_schema:
        try:
            if api.graphql:
                doc = load_graphql_endpoint(api.graphql, headers=api.headers)
            else:
                doc = load_spec(cfg.resolve(api.spec), headers=api.headers)
            store.save_spec(kind, api.name, doc)
        except FetchError as e:
            errors.append(f"{'graphql' if api.graphql else 'spec'}: {e}")
    for ep in api.endpoints:
        try:
            samples = collect_samples(cfg, ep)
            store.save_samples(kind, api.name, ep.name, samples, append=append)
        except FetchError as e:
            errors.append(f"{ep.name}: {e}")
    return errors


def endpoint_diff(store: Store, api: str, endpoint: str) -> FieldDiff | None:
    base = store.load_samples(BASELINE, api, endpoint)
    cur = store.load_samples(CURRENT, api, endpoint)
    if not base or not cur:
        return None
    return diff_fields(
        flatten_samples(base), flatten_samples(cur),
        direction=RESPONSE, location=f"{endpoint} (live)",
        old_values=collect_values(base), new_values=collect_values(cur),
        from_samples=True,
    )


def _selected(cfg: Config, names: list[str] | None) -> list[Api]:
    return [cfg.api(n) for n in names] if names else cfg.apis


def operation_files(cfg: Config, api: Api) -> list[str]:
    return expand_patterns(api.operations, cfg.base) if api.operations else []


def _graphql_errors(samples: list[Any]) -> list[str]:
    """Messages from GraphQL-style ``{"errors": [...]}`` response bodies."""
    out = []
    for s in samples:
        if isinstance(s, dict) and isinstance(s.get("errors"), list) and s["errors"]:
            for e in s["errors"]:
                msg = e.get("message") if isinstance(e, dict) else str(e)
                if msg and msg not in out:
                    out.append(str(msg))
    return out


def schema_changes(cfg: Config, api: Api, old: dict, new: dict) -> list[Change]:
    if kind_of(old) != kind_of(new):
        return [Change(BREAKING, "format-changed", "spec", "",
                       f"the description changed format ({kind_of(old)} → {kind_of(new)}); re-run `apimend snapshot`")]
    if kind_of(new) == "graphql":
        return gql_check.check(old, new, operation_files(cfg, api), cfg.base, api.only_used).changes
    return diff_specs(old, new)


# --------------------------------------------------------------------- commands


def cmd_init(args) -> int:
    p = Path(args.config)
    if p.exists() and not args.force:
        _err(f"{p} already exists (use --force to overwrite)")
        return EXIT_ERROR
    p.write_text(STARTER, encoding="utf-8")
    _out(f"Wrote {p}. Edit it, then run `apimend snapshot` to record a baseline.")
    return EXIT_OK


def cmd_snapshot(args) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.store)
    failed = False
    for api in _selected(cfg, args.api):
        errors = pull(cfg, store, api, BASELINE, append=args.append)
        n = sum(len(store.load_samples(BASELINE, api.name, e.name)) for e in api.endpoints)
        parts = []
        if api.spec and store.load_spec(BASELINE, api.name) is not None:
            parts.append("spec")
        if api.endpoints:
            parts.append(f"{n} sample(s)")
        _out(f"✓ {api.name}: baseline saved ({', '.join(parts) or 'nothing'})")
        for e in errors:
            failed = True
            _err(f"{api.name}: {e}")
    _out(f"Baseline stored in {cfg.store}. Commit it so changes are reviewable.")
    return EXIT_ERROR if failed else EXIT_OK


def run_check(cfg: Config, apis: list[Api], refresh: bool = True) -> Report:
    store = Store(cfg.store)
    report = Report()
    for api in apis:
        res = ApiResult(api.name)
        report.results.append(res)
        if not store.has_baseline(api.name):
            res.errors.append("no baseline yet; run `apimend snapshot`")
            continue
        if refresh:
            res.errors.extend(pull(cfg, store, api, CURRENT))
        if api.has_schema:
            old, new = store.load_spec(BASELINE, api.name), store.load_spec(CURRENT, api.name)
            if old is None:
                res.errors.append("baseline has no spec; run `apimend snapshot`")
            elif new is not None:
                res.changes.extend(schema_changes(cfg, api, old, new))
        for ep in api.endpoints:
            base = store.load_samples(BASELINE, api.name, ep.name)
            if not base:
                res.errors.append(f"{ep.name}: baseline has no samples; run `apimend snapshot`")
                continue
            failing = [m for m in _graphql_errors(store.load_samples(CURRENT, api.name, ep.name))
                       if m not in _graphql_errors(base)]
            if failing:
                res.changes.append(Change(BREAKING, "request-failing", f"{ep.name} (live)", "$.errors",
                                          f"`{ep.name}` now returns errors: {'; '.join(failing[:3])}"))
            fd = endpoint_diff(store, api.name, ep.name)
            if fd is not None:
                res.changes.extend(fd.changes)
    return report


def cmd_check(args) -> int:
    cfg = load_config(args.config)
    report = run_check(cfg, _selected(cfg, args.api), refresh=not args.offline)
    md = report.to_markdown(show_info=not args.quiet)
    data = report.to_dict()
    Store(cfg.store).write_report(data, md)
    if args.format == "json":
        _out(json.dumps(data, indent=2))
    else:
        _out(md)
    if args.output:
        Path(args.output).write_text(json.dumps(data, indent=2) if args.format == "json" else md, encoding="utf-8")
    if report.errors:
        return EXIT_ERROR
    fail_on = args.fail_on or cfg.fail_on
    return EXIT_DRIFT if report.exceeds(fail_on) else EXIT_OK


def cmd_heal(args) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.store)
    wrote = 0
    for api in _selected(cfg, args.api):
        for ep in api.endpoints:
            fd = endpoint_diff(store, api.name, ep.name)
            if fd is None:
                _out(f"- {api.name}/{ep.name}: no baseline or current samples (run `apimend check`)")
                continue
            plan = build_plan(fd, api=api.name, endpoint=ep.name)
            old_path = store.plan_path(api.name, ep.name)
            if old_path.exists():
                plan = merge_manual_ops(json.loads(old_path.read_text(encoding="utf-8")), plan)
            if not plan["ops"] and not plan["unresolved"]:
                store.remove_plan(api.name, ep.name)
                _out(f"✓ {api.name}/{ep.name}: no drift, nothing to heal")
                continue
            path = store.save_plan(api.name, ep.name, plan)
            wrote += 1
            _out(f"✓ {api.name}/{ep.name}: {len(plan['ops'])} fix(es), "
                 f"{len(plan['unresolved'])} need a human → {path}")
            for op in plan["ops"]:
                if op["op"] == "rename":
                    conf = f"  (confidence {op['confidence']})" if "confidence" in op else ""
                    _out(f"    rename {op['parent']}.{op['from']} → {op['to']}{conf}")
                elif op["op"] == "coerce":
                    _out(f"    coerce {op['path']} {op.get('from', '?')} → {op['to']}")
                else:
                    _out(f"    {op['op']} {op.get('path', '')} (kept from your edits)")
            for u in plan["unresolved"]:
                _out(f"    ! {u['message']}")
        if api.operations:
            wrote += heal_graphql(cfg, store, api, args.apply)
        elif api.has_schema and not api.endpoints:
            _out(f"- {api.name}: schema-only; repairs need live samples (`endpoints`) "
                 "or, for GraphQL, your `operations`")
    if wrote:
        _out("\nReview the plans, then run `apimend verify`. Apply response plans in code with "
             "`apimend.Healer.from_file(...)`.")
    return EXIT_OK


def heal_graphql(cfg: Config, store: Store, api: Api, apply: bool) -> int:
    old, new = store.load_spec(BASELINE, api.name), store.load_spec(CURRENT, api.name)
    if old is None or new is None or kind_of(new) != "graphql":
        _out(f"- {api.name}: no baseline or current GraphQL schema (run `apimend check`)")
        return 0
    plan, patch = gql_check.heal(old, new, operation_files(cfg, api), cfg.base, api.name)
    if not plan["edits"] and not plan["unresolved"]:
        store.remove_gql_fix(api.name)
        _out(f"✓ {api.name}: your operations are valid against the current schema")
        return 0
    path = store.save_gql_fix(api.name, plan, patch)
    _out(f"✓ {api.name}: {len(plan['edits'])} operation fix(es), {len(plan['unresolved'])} need a human → {path}")
    for e in plan["edits"]:
        _out(f"    {e['file']}: `{e['old']}` → `{e['new']}`  ({e['reason']})")
    for u in plan["unresolved"]:
        _out(f"    ! {u['where']}: {u['message']}")
    if plan["edits"]:
        if apply:
            try:
                changed = gql_check.apply(plan, cfg.base)
            except ValueError as e:
                _err(str(e))
                return 1
            plan["applied"], plan["edits"] = plan["edits"], []
            store.save_gql_fix(api.name, plan, patch)
            _out(f"    applied to {len(changed)} file(s)")
        else:
            _out(f"    review {path.with_suffix('.patch')}, then `apimend heal --apply` to edit your files")
    return 1


def cmd_verify(args) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.store)
    plans = store.plans()
    if args.api:
        plans = [p for p in plans if p.parent.name in args.api]
    gql_apis = [a for a in _selected(cfg, args.api) if a.operations]
    if not plans and not gql_apis:
        _out("No plans to verify (run `apimend heal` first).")
        return EXIT_OK
    failed = False
    for api in gql_apis:
        new = store.load_spec(CURRENT, api.name) or store.load_spec(BASELINE, api.name)
        if new is None or kind_of(new) != "graphql":
            _err(f"{api.name}: no GraphQL schema recorded (run `apimend snapshot`)")
            failed = True
            continue
        plan = store.load_gql_fix(api.name)
        try:
            res = gql_check.verify(new, operation_files(cfg, api), plan, cfg.base)
        except (ValueError, OSError) as e:
            _err(f"{api.name}: {e}")
            failed = True
            continue
        fixes = f" with {len(plan['edits'])} fix(es) from `apimend heal`" if plan and plan.get("edits") else ""
        if res.ok:
            _out(f"✓ {api.name}: {res.operations} operation(s) valid against the current schema{fixes}")
        else:
            failed = True
            _out(f"✗ {api.name}: {len(res.errors)} problem(s) in {res.operations} operation(s){fixes}")
            for e in res.errors[:20]:
                _out(f"    {e}")
    for p in plans:
        plan = json.loads(p.read_text(encoding="utf-8"))
        api, ep = p.parent.name, p.stem
        base = store.load_samples(BASELINE, api, ep)
        cur = store.load_samples(CURRENT, api, ep)
        if not base or not cur:
            _err(f"{api}/{ep}: missing baseline or current samples")
            failed = True
            continue
        res = verify_plan(plan, cur, flatten_samples(base))
        if res.ok:
            _out(f"✓ {api}/{ep}: {res.passed}/{res.samples} healed sample(s) match the baseline shape")
        else:
            failed = True
            _out(f"✗ {api}/{ep}: {res.passed}/{res.samples} passed")
            for e in res.errors[:20]:
                _out(f"    {e}")
            if plan.get("unresolved"):
                _out(f"    {len(plan['unresolved'])} change(s) can't be healed automatically: fix the consumer, "
                     "or add a `default` op to the plan (see docs/plans.md)")
    return EXIT_DRIFT if failed else EXIT_OK


def cmd_accept(args) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.store)
    for api in _selected(cfg, args.api):
        try:
            store.accept(api.name)
        except FileNotFoundError as e:
            _err(str(e))
            return EXIT_ERROR
        for ep in api.endpoints:
            store.remove_plan(api.name, ep.name)
        store.remove_gql_fix(api.name)
        _out(f"✓ {api.name}: current accepted as the new baseline")
    return EXIT_OK


def cmd_diff(args) -> int:
    try:
        old, new = load_spec(args.old), load_spec(args.new)
    except FetchError as e:
        _err(str(e))
        return EXIT_ERROR
    if kind_of(old) != kind_of(new):
        _err(f"cannot compare {kind_of(old)} with {kind_of(new)}")
        return EXIT_ERROR
    if kind_of(new) == "graphql":
        files = expand_patterns(args.operations, Path.cwd()) if args.operations else []
        changes = gql_check.check(old, new, files, Path.cwd()).changes
    else:
        changes = diff_specs(old, new)
    report = Report([ApiResult(f"{args.old} → {args.new}", changes)])
    if args.format == "json":
        _out(json.dumps(report.to_dict(), indent=2))
    else:
        _out(report.to_markdown(show_info=not args.quiet))
    return EXIT_DRIFT if report.exceeds(args.fail_on) else EXIT_OK


def cmd_scan(args) -> int:
    from .scan import scan, summary_table, to_yaml

    roots = [Path(p) for p in (args.paths or ["."])]
    for r in roots:
        if not r.is_dir():
            _err(f"{r} is not a directory")
            return EXIT_ERROR
    out = Path(args.out or args.config)
    result = scan(roots, out.resolve().parent, include_local=args.include_local)
    if args.format == "json":
        _out(json.dumps(result.to_dict(), indent=2))
        return EXIT_OK
    text = to_yaml(result, enable_live=args.enable_live)
    if args.print:
        _out(text)
        return EXIT_OK
    _out(f"Scanned {result.files_scanned} file(s) in {', '.join(str(r) for r in roots)}.")
    if not result.apis:
        _out("No external APIs found. Add one by hand: `apimend init` writes a commented example.")
        return EXIT_OK
    _out("")
    _out(summary_table(result))
    for w in result.warnings:
        _out(f"! {w}")
    if result.skipped_local:
        _out(f"(skipped {result.skipped_local} localhost URL(s); use --include-local to keep them)")
    if out.exists() and not args.force:
        _err(f"{out} already exists; use --force to overwrite, --out to write elsewhere, or --print")
        return EXIT_ERROR
    out.write_text(text, encoding="utf-8")
    ready = sum(1 for a in result.apis if a.ready)
    _out(f"\nWrote {out}: {ready} API(s) ready, {len(result.apis) - ready} need a step noted in the file.")
    if args.snapshot and ready:
        _out("")
        args.config = str(out)
        args.api, args.append = [], False
        return cmd_snapshot(args)
    if ready:
        _out("Next: `apimend snapshot` to record a baseline, then `apimend check` (locally or in CI).")
    return EXIT_OK


def cmd_infer(args) -> int:
    samples = []
    for f in args.files:
        try:
            samples.append(json.loads(Path(f).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            _err(f"{f}: {e}")
            return EXIT_ERROR
    _out(json.dumps(infer(samples), indent=2))
    return EXIT_OK


def cmd_propose(args) -> int:
    from .llm import build_prompt, propose_fix

    cfg = load_config(args.config)
    store = Store(cfg.store)
    cfg.api(args.api)
    fd = endpoint_diff(store, args.api, args.endpoint)
    if fd is None or not fd.changes:
        _out("No drift recorded for that endpoint (run `apimend check` first).")
        return EXIT_OK
    plan_path = store.plan_path(args.api, args.endpoint)
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else None
    src = Path(args.source)
    prompt = build_prompt([c.to_dict() for c in fd.changes], plan, src.name, src.read_text(encoding="utf-8"))
    try:
        answer = propose_fix(prompt, model=args.model)
    except RuntimeError as e:
        _err(str(e))
        return EXIT_ERROR
    if args.output:
        Path(args.output).write_text(answer, encoding="utf-8")
        _out(f"Proposal written to {args.output}")
    else:
        _out(answer)
    return EXIT_OK


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apimend",
        description="Detect, diagnose, repair and verify drift in the APIs you consume.",
    )
    p.add_argument("--version", action="version", version=f"apimend {__version__}")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="config file (default: apimend.yaml)")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    s = sub.add_parser("init", help="write a starter apimend.yaml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("snapshot", help="record the current API as the baseline")
    s.add_argument("api", nargs="*", help="only these APIs")
    s.add_argument("--append", action="store_true", help="add samples to the baseline instead of replacing it")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("check", help="compare the live API with the baseline")
    s.add_argument("api", nargs="*")
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.add_argument("-o", "--output", help="also write the report to this file")
    s.add_argument("--fail-on", choices=["breaking", "warning", "never"])
    s.add_argument("--offline", action="store_true", help="reuse stored current data instead of fetching")
    s.add_argument("-q", "--quiet", action="store_true", help="hide info-level changes")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("heal", help="generate repair plans from the last check")
    s.add_argument("api", nargs="*")
    s.add_argument("--apply", action="store_true", help="also apply GraphQL operation fixes to your files")
    s.set_defaults(func=cmd_heal)

    s = sub.add_parser("verify", help="replay current samples through the plans")
    s.add_argument("api", nargs="*")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("accept", help="adopt the current API as the new baseline")
    s.add_argument("api", nargs="*")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("diff", help="compare two OpenAPI, Swagger or GraphQL schemas")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--operations", action="append", metavar="GLOB",
                   help="GraphQL only: also check these operation files against the new schema")
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.add_argument("--fail-on", choices=["breaking", "warning", "never"], default="breaking")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("scan", help="find the APIs your code uses and write apimend.yaml")
    s.add_argument("paths", nargs="*", help="directories to scan (default: .)")
    s.add_argument("-o", "--out", help="config file to write (default: the -c path, apimend.yaml)")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")
    s.add_argument("--print", action="store_true", help="print the config instead of writing it")
    s.add_argument("--format", choices=["yaml", "json"], default="yaml",
                   help="json prints an inventory of what was found (for dashboards and org-wide scans)")
    s.add_argument("--enable-live", action="store_true",
                   help="also enable live GET endpoints and GraphQL introspection found in code")
    s.add_argument("--include-local", action="store_true", help="keep localhost URLs")
    s.add_argument("--snapshot", action="store_true", help="record a baseline right after writing the config")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("infer", help="print the JSON Schema inferred from sample files")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cmd_infer)

    s = sub.add_parser("propose", help="ask Claude for a code fix (needs ANTHROPIC_API_KEY)")
    s.add_argument("api")
    s.add_argument("endpoint")
    s.add_argument("--source", required=True, help="the consumer source file to fix")
    s.add_argument("--model")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_propose)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        _err(str(e))
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
