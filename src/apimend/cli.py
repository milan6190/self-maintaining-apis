"""Command-line interface: ``apimend <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULT_CONFIG, STARTER, Api, Config, ConfigError, Endpoint, load_config
from .diff import RESPONSE, FieldDiff, diff_fields, diff_specs
from .fetch import FetchError, fetch_json, load_document
from .heal import build_plan, merge_manual_ops
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
    if api.spec:
        try:
            doc = load_document(cfg.resolve(api.spec), headers=api.headers)
            store.save_spec(kind, api.name, doc)
        except FetchError as e:
            errors.append(f"spec: {e}")
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
        if api.spec:
            old, new = store.load_spec(BASELINE, api.name), store.load_spec(CURRENT, api.name)
            if old is None:
                res.errors.append("baseline has no spec; run `apimend snapshot`")
            elif new is not None:
                res.changes.extend(diff_specs(old, new))
        for ep in api.endpoints:
            if not store.load_samples(BASELINE, api.name, ep.name):
                res.errors.append(f"{ep.name}: baseline has no samples; run `apimend snapshot`")
                continue
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
        if api.spec and not api.endpoints:
            _out(f"- {api.name}: spec-only; plans need live samples (add `endpoints`)")
    if wrote:
        _out("\nReview the plans, then run `apimend verify`. Apply them in code with "
             "`apimend.Healer.from_file(...)`.")
    return EXIT_OK


def cmd_verify(args) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.store)
    plans = store.plans()
    if args.api:
        plans = [p for p in plans if p.parent.name in args.api]
    if not plans:
        _out("No plans to verify (run `apimend heal` first).")
        return EXIT_OK
    failed = False
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
        _out(f"✓ {api.name}: current accepted as the new baseline")
    return EXIT_OK


def cmd_diff(args) -> int:
    try:
        old, new = load_document(args.old), load_document(args.new)
    except FetchError as e:
        _err(str(e))
        return EXIT_ERROR
    report = Report([ApiResult(f"{args.old} → {args.new}", diff_specs(old, new))])
    if args.format == "json":
        _out(json.dumps(report.to_dict(), indent=2))
    else:
        _out(report.to_markdown(show_info=not args.quiet))
    return EXIT_DRIFT if report.exceeds(args.fail_on) else EXIT_OK


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
    s.set_defaults(func=cmd_heal)

    s = sub.add_parser("verify", help="replay current samples through the plans")
    s.add_argument("api", nargs="*")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("accept", help="adopt the current API as the new baseline")
    s.add_argument("api", nargs="*")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("diff", help="compare two OpenAPI/Swagger documents")
    s.add_argument("old")
    s.add_argument("new")
    s.add_argument("--format", choices=["md", "json"], default="md")
    s.add_argument("--fail-on", choices=["breaking", "warning", "never"], default="breaking")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_diff)

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
