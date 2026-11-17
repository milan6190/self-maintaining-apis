"""Waivers, SARIF output and org-wide rollups: the features teams need to adopt a drift gate."""

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from apimend.cli import main
from apimend.config import ConfigError, load_config
from apimend.diff import Change
from apimend.waivers import apply_waivers, parse_waivers, waiver_notes

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "petstore"


@pytest.fixture
def drifted(tmp_path, monkeypatch):
    for name in ("apimend.yaml", "specs", "samples"):
        src = EXAMPLE / name
        (shutil.copytree if src.is_dir() else shutil.copy)(src, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PETSTORE_VERSION", raising=False)
    assert main(["snapshot"]) == 0
    monkeypatch.setenv("PETSTORE_VERSION", "v2")
    return tmp_path


def add_waivers(project: Path, text: str) -> None:
    cfg = project / "apimend.yaml"
    cfg.write_text(cfg.read_text() + "\n" + text)


def test_waiver_requires_a_reason_and_a_matcher():
    with pytest.raises(ValueError, match="reason"):
        parse_waivers([{"kind": "field-removed"}])
    with pytest.raises(ValueError, match="at least one"):
        parse_waivers([{"reason": "x"}])
    with pytest.raises(ValueError, match="unknown"):
        parse_waivers([{"reason": "x", "kind": "a", "typo": 1}])
    with pytest.raises(ValueError, match="date"):
        parse_waivers([{"reason": "x", "kind": "a", "expires": "soon"}])


def test_waivers_match_by_glob_and_expire():
    today = date(2026, 11, 17)
    ws = parse_waivers([
        {"api": "pet*", "kind": "field-removed", "reason": "known", "owner": "team-a", "expires": "2026-12-01"},
        {"kind": "operation-removed", "reason": "old", "expires": "2026-11-01"},
    ])
    ch = [Change("breaking", "field-removed", "list-pets (live)", "$[].tag", "gone"),
          Change("breaking", "operation-removed", "GET /x", "$", "gone")]
    apply_waivers(ws, "petstore", ch, today=today)
    assert ch[0].waived == "known, team-a until 2026-12-01"
    assert ch[1].waived is None  # expired
    notes = waiver_notes(ws, {"petstore"}, True, today=today)
    assert any("expires on 2026-12-01" in n for n in notes)
    assert any("expired on 2026-11-01" in n for n in notes)


def test_waived_changes_do_not_fail_the_check(drifted, capsys):
    assert main(["check"]) == 1
    soon = (date.today() + timedelta(days=60)).isoformat()
    add_waivers(drifted, f"""waivers:
  - api: petstore
    severity: breaking
    reason: "Migrating to v2 in PR #88"
    owner: pets-team
    expires: {soon}
""")
    capsys.readouterr()
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "waived change(s)" in out and "Migrating to v2" in out
    report = json.loads((drifted / ".apimend/report.json").read_text())
    assert report["summary"]["breaking"] == 0 and report["summary"]["waived"] > 0
    # warnings still fail a stricter gate
    assert main(["check", "--fail-on", "warning"]) == (1 if report["summary"]["warning"] else 0)


def test_unused_waiver_is_reported(drifted, capsys):
    add_waivers(drifted, """waivers:
  - kind: tool-removed
    reason: "nothing matches this"
""")
    main(["check"])
    assert "no longer matches any change" in capsys.readouterr().out


def test_bad_waiver_is_a_config_error(drifted):
    add_waivers(drifted, "waivers:\n  - kind: x\n")
    with pytest.raises(ConfigError, match="reason"):
        load_config(drifted / "apimend.yaml")


def test_sarif_output(drifted, capsys):
    add_waivers(drifted, """waivers:
  - kind: operation-removed
    reason: "We never called it"
""")
    assert main(["check", "--format", "sarif", "-o", "out.sarif"]) == 1
    sarif = json.loads((drifted / "out.sarif").read_text())
    assert sarif["version"] == "2.1.0"
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "apimend"
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    results = run["results"]
    assert {r["ruleId"] for r in results} <= rule_ids
    assert {r["level"] for r in results} <= {"error", "warning", "note"}
    loc = results[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "apimend.yaml" and loc["region"]["startLine"] >= 1
    suppressed = [r for r in results if r.get("suppressions")]
    assert suppressed and all(r["ruleId"] == "operation-removed" for r in suppressed)
    fps = [r["partialFingerprints"]["apimendChange/v1"] for r in results]
    assert len(fps) == len(set(fps))


def test_sarif_points_graphql_findings_at_the_operation(tmp_path, monkeypatch):
    ex = Path(__file__).resolve().parent.parent / "examples" / "graphql-shop"
    shutil.copytree(ex, tmp_path / "shop", ignore=shutil.ignore_patterns(".apimend"))
    monkeypatch.chdir(tmp_path / "shop")
    monkeypatch.delenv("SHOP_SCHEMA", raising=False)
    cfg = (tmp_path / "shop" / "apimend.yaml").read_text()
    var = "SHOP_SCHEMA" if "SHOP_SCHEMA" in cfg else cfg.split("${")[1].split(":")[0].split("}")[0]
    assert main(["snapshot"]) == 0
    monkeypatch.setenv(var, "v2")
    main(["check", "--format", "sarif", "-o", "out.sarif"])
    results = json.loads(Path("out.sarif").read_text())["runs"][0]["results"]
    uris = {r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] for r in results}
    assert any(u.startswith("src/") for u in uris)


def test_rollup_across_repos(tmp_path, capsys):
    def report(repo, apis):
        p = tmp_path / repo / ".apimend" / "report.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"generated_at": "2026-11-17T00:00:00+00:00", "apis": apis}))

    report("billing", [{"name": "stripe", "owner": "payments", "errors": [], "changes": [
        {"severity": "breaking", "kind": "field-removed", "message": "`$.id` is no longer returned"},
        {"severity": "breaking", "kind": "field-removed", "message": "waived one", "waived": "known"}]}])
    report("web", [{"name": "github", "errors": [], "changes": []},
                   {"name": "carrier", "owner": "logistics", "errors": [],
                    "changes": [{"severity": "warning", "kind": "enum-values-added", "message": "new values"}]}])
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "report.json").write_text("{nope")

    assert main(["rollup", str(tmp_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"] == {"repos": 2, "apis": 3, "breaking_apis": 1, "healthy_apis": 1, "breaking": 1,
                               "warning": 1, "waived": 1, "errors": 0}
    assert data["apis"][0]["repo"] == "billing" and data["apis"][0]["status"] == "breaking"
    assert data["owners"]["unowned"]["apis"] == 1
    assert main(["rollup", str(tmp_path), "--fail-on", "breaking"]) == 1
    md = capsys.readouterr().out
    assert "| payments | 1 | 1 | 0 | 1 |" in md


def test_owner_is_carried_into_the_report(drifted):
    cfg = drifted / "apimend.yaml"
    cfg.write_text(cfg.read_text().replace("  - name: petstore\n", "  - name: petstore\n    owner: pets-team\n"))
    main(["check"])
    report = json.loads((drifted / ".apimend/report.json").read_text())
    assert report["apis"][0]["owner"] == "pets-team"
