import json
import shutil
from pathlib import Path

import pytest

from apimend.cli import main
from apimend.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "petstore"


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("apimend.yaml", "specs", "samples"):
        src = EXAMPLE / name
        if src.is_dir():
            shutil.copytree(src, tmp_path / name)
        else:
            shutil.copy(src, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PETSTORE_VERSION", raising=False)
    return tmp_path


def test_full_loop(project, monkeypatch, capsys):
    assert main(["snapshot"]) == 0
    assert (project / ".apimend/baseline/petstore/spec.json").exists()

    # unchanged API: no drift
    assert main(["check"]) == 0

    monkeypatch.setenv("PETSTORE_VERSION", "v2")
    assert main(["check", "--format", "json"]) == 1
    report = json.loads((project / ".apimend/report.json").read_text())
    assert report["summary"]["breaking"] > 0
    kinds = {c["kind"] for c in report["apis"][0]["changes"]}
    assert {"field-renamed", "operation-removed", "required-param-added"} <= kinds

    assert main(["heal"]) == 0
    plan = json.loads((project / ".apimend/plans/petstore/list-pets.json").read_text())
    assert {op["op"] for op in plan["ops"]} == {"rename", "coerce"}
    assert [u["path"] for u in plan["unresolved"]] == ["$[].tag"]

    assert main(["verify"]) == 1  # the removed field still needs a human
    plan["ops"].append({"op": "default", "path": "$[].tag", "value": "unknown"})
    (project / ".apimend/plans/petstore/list-pets.json").write_text(json.dumps(plan))
    assert main(["heal"]) == 0
    assert main(["verify"]) == 0

    assert main(["accept"]) == 0
    assert main(["check"]) == 0
    assert not (project / ".apimend/plans/petstore/list-pets.json").exists()


def test_check_without_baseline_is_an_error(project, capsys):
    assert main(["check"]) == 2
    assert "no baseline" in capsys.readouterr().out


def test_fail_on_never(project, monkeypatch):
    main(["snapshot"])
    monkeypatch.setenv("PETSTORE_VERSION", "v2")
    assert main(["check", "--fail-on", "never"]) == 0


def test_diff_command(project, capsys):
    rc = main(["diff", "specs/petstore-v1.yaml", "specs/petstore-v2.yaml", "--format", "json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["breaking"] > 0


def test_diff_missing_file(project):
    assert main(["diff", "nope.yaml", "specs/petstore-v2.yaml"]) == 2


def test_infer_command(project, capsys):
    assert main(["infer", "samples/v1/list-pets/page-1.json"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["type"] == "array"


def test_init_writes_valid_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["init"]) == 2  # refuses to overwrite
    cfg = load_config(tmp_path / "apimend.yaml")
    assert cfg.apis[0].name == "petstore"


def test_config_validation(tmp_path):
    p = tmp_path / "apimend.yaml"
    p.write_text("apis:\n  - name: bad name\n    spec: x.yaml\n")
    with pytest.raises(ConfigError):
        load_config(p)
    p.write_text("apis:\n  - name: ok\n    endpoints:\n      - name: e\n")
    with pytest.raises(ConfigError, match="url"):
        load_config(p)
    p.write_text("apis:\n  - name: ok\n    spec: ${MISSING_VAR_FOR_TEST}\n")
    with pytest.raises(ConfigError, match="MISSING_VAR_FOR_TEST"):
        load_config(p)


def test_propose_requires_api_key(project, monkeypatch, capsys):
    main(["snapshot"])
    monkeypatch.setenv("PETSTORE_VERSION", "v2")
    main(["check"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    src = project / "client.py"
    src.write_text("print('hi')\n")
    assert main(["propose", "petstore", "list-pets", "--source", str(src)]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_graphql_query_endpoint_and_failing_responses(tmp_path, monkeypatch):
    import json as _json

    from apimend.config import load_config

    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "ok/1.json").write_text(_json.dumps({"data": {"featured": [{"id": 1}]}}))
    (tmp_path / "bad/1.json").write_text(_json.dumps({"data": None, "errors": [{"message": "Cannot query field 'featured'"}]}))
    (tmp_path / "apimend.yaml").write_text("""
apis:
  - name: shop
    graphql: https://api.shop.example/graphql
    headers: {Authorization: "Bearer t"}
    endpoints:
      - name: featured
        query: "{ featured { id } }"
        variables: {first: 2}
      - name: recorded
        samples: ./${WHICH:-ok}/*.json
""")
    cfg = load_config(tmp_path / "apimend.yaml")
    ep = cfg.api("shop").endpoints[0]
    assert ep.method == "POST" and ep.url == "https://api.shop.example/graphql"
    assert ep.body == {"query": "{ featured { id } }", "variables": {"first": 2}}
    assert ep.headers == {"Authorization": "Bearer t"}

    # only the recorded endpoint can run offline; check that new `errors` are reported as breaking
    (tmp_path / "apimend.yaml").write_text("""
apis:
  - name: shop
    endpoints:
      - name: recorded
        samples: ./${WHICH:-ok}/*.json
""")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WHICH", raising=False)
    assert main(["snapshot"]) == 0
    monkeypatch.setenv("WHICH", "bad")
    assert main(["check", "--format", "json"]) == 1
    report = _json.loads((tmp_path / ".apimend/report.json").read_text())
    failing = [c for c in report["apis"][0]["changes"] if c["kind"] == "request-failing"]
    assert failing and "Cannot query field" in failing[0]["message"]


def test_diff_graphql_with_operations(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.graphql").write_text("type Query { user: User } type User { email: String }")
    (tmp_path / "b.graphql").write_text("type Query { user: User } type User { emailAddress: String }")
    (tmp_path / "q.graphql").write_text("query Me { user { email } }")
    monkeypatch.chdir(tmp_path)
    assert main(["diff", "a.graphql", "b.graphql", "--operations", "*.graphql", "--format", "json"]) == 1
    out = capsys.readouterr().out
    assert "operation-broken" in out and "query Me" in out
