import json
import shutil
from pathlib import Path

import pytest
import yaml

from apimend.cli import main
from apimend.config import ConfigError, load_config
from apimend.registry import KNOWN_APIS, by_package, by_url, normalize_package
from apimend.scan import _classify, _slug_host, is_noise, looks_like_api, normalize_url, scan, to_yaml

DEMO = Path(__file__).resolve().parent.parent / "examples" / "scan-demo" / "acme-app"


@pytest.fixture
def demo(tmp_path):
    dst = tmp_path / "acme-app"
    shutil.copytree(DEMO, dst)
    return dst


@pytest.mark.parametrize("raw,expected", [
    ("https://api.acme.com/v1/users", ("api.acme.com", "/v1/users", "https://api.acme.com/v1/users", False)),
    ("https://api.acme.com/v1/users/{user_id}", ("api.acme.com", "/v1/users/x", "https://api.acme.com/v1/users/{}", True)),
    ("https://api.acme.com/v1/users/${id}/posts", ("api.acme.com", "/v1/users/x/posts",
                                                    "https://api.acme.com/v1/users/{}/posts", True)),
    ("https://api.acme.com/v1/items/:id", ("api.acme.com", "/v1/items/x", "https://api.acme.com/v1/items/{}", True)),
    ("https://{region}.api.acme.com/v1", ("*.api.acme.com", "/v1", "https://{}.api.acme.com/v1", True)),
    ("https://${HOST}/v1/users", None),
    ("https://billing-service:8080/v1/x", ("billing-service", "/v1/x", "https://billing-service:8080/v1/x", False)),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_noise_and_api_likeness():
    assert is_noise("github.com", "/acme/app")
    assert is_noise("cdn.acme.com", "/app.js")
    assert is_noise("api.acme.test", "/")
    assert not is_noise("api.github.com", "/repos")
    assert looks_like_api("api.acme.com", "/")
    assert looks_like_api("acme.com", "/v2/orders")
    assert looks_like_api("acme.com", "/graphql")
    assert not looks_like_api("acme.com", "/about")


@pytest.mark.parametrize("line,expected", [
    ('r = requests.get("https://x.io/a")', ("GET", "call")),
    ('requests.post("https://x.io/a", json=d)', ("POST", "call")),
    ('const r = await fetch("https://x.io/a")', ("GET", "call")),
    ('resp, err := http.Get("https://x.io/a")', ("GET", "call")),
    ('req, _ := http.NewRequest("DELETE", "https://x.io/a", nil)', ("DELETE", "call")),
    ('URL = os.environ.get("X_URL", "https://x.io")', (None, "base")),
    ('export const API_BASE = "https://x.io/v1";', (None, "base")),
    ('print("see https://x.io/a")', (None, "literal")),
])
def test_classify(line, expected):
    assert _classify(line, "", line) == expected


def test_slug_host():
    assert _slug_host("api.stripe.com") == "stripe"
    assert _slug_host("billing.internal.acme.com") == "billing-internal-acme"
    assert _slug_host("api.acme.co.uk") == "acme"
    assert _slug_host("billing-service") == "billing-service"
    assert _slug_host("api.x.ai") == "x-ai"


def test_registry():
    for api in KNOWN_APIS:
        assert api.spec.startswith("https://raw.githubusercontent.com/")
    assert by_package("slack_sdk").name == "slack"
    assert by_package("Slack-SDK").name == "slack"
    assert by_package("github.com/stripe/stripe-go/v76").name == "stripe"
    assert by_package("@octokit/graphql").name == "github-graphql"
    assert by_package("left-pad") is None
    assert by_url("api.github.com", "/graphql").name == "github-graphql"
    assert by_url("api.github.com", "/repos/a/b").name == "github"
    assert by_url("slack.com", "/api/chat.postMessage").name == "slack"
    assert by_url("hooks.slack.com", "/services/x") is None
    assert normalize_package("Foo_Bar.baz") == "foo-bar-baz"


def test_scan_demo_repo(demo):
    result = scan([demo], demo)
    apis = {a.name: a for a in result.apis}
    assert {name for name, a in apis.items() if a.ready} == {"acme", "github-graphql", "slack", "stripe"}
    assert apis["acme"].spec == "specs/orders-api.yaml" and apis["acme"].title == "Acme Orders API"
    assert apis["github-graphql"].operations == ["web/**/*.ts"]
    assert apis["acme-graphql"].operations == ["web/src/**/*.graphql"]  # from codegen.yml
    assert apis["acme-graphql"].graphql_url == "https://api.acme.com/graphql"
    billing = apis["billing-internal-acme"]
    assert [c.url for c in billing.get_endpoints()] == ["https://billing.internal.acme.com/v1/invoices"]
    assert {c.method for c in billing.calls} == {"GET", "POST"}
    assert "shipfast-logistics" in apis and "fxrates-provider" in apis
    # the Slack webhook call joins the Slack entry; noise stays out
    assert "hooks-slack" not in apis and any("alerts.py" in str(e) for e in apis["slack"].evidence)
    assert set(apis) == {"acme", "github-graphql", "slack", "stripe", "acme-graphql", "billing-internal-acme",
                         "shipfast-logistics", "fxrates-provider"}
    assert result.skipped_local == 1 and not result.warnings


def test_generated_config_loads_and_names_what_is_missing(demo):
    text = to_yaml(scan([demo], demo))
    (demo / "apimend.yaml").write_text(text)
    cfg = load_config(demo / "apimend.yaml")
    assert sorted(a.name for a in cfg.apis) == ["acme", "github-graphql", "slack", "stripe"]
    assert cfg.api("github-graphql").operations == ["web/**/*.ts"]
    assert "# - name: billing-internal-acme" in text
    assert "# To watch it: uncomment and add auth headers" in text
    raw = yaml.safe_load(text)
    assert all("headers" not in a for a in raw["apis"])  # nothing enabled needs a secret


def test_enable_live_turns_on_endpoints_without_requiring_secrets(demo):
    text = to_yaml(scan([demo], demo), enable_live=True)
    (demo / "apimend.yaml").write_text(text)
    cfg = load_config(demo / "apimend.yaml")
    names = {a.name for a in cfg.apis}
    assert {"billing-internal-acme", "acme-graphql", "shipfast-logistics"} <= names
    assert cfg.api("acme-graphql").graphql == "https://api.acme.com/graphql"
    assert "fxrates-provider" in names


@pytest.mark.parametrize("host,path,problem", [
    ("api.acme.com", "/v1", None),
    ("billing.internal.acme.com", "/", None),
    ("orders.internal", "/v1/orders", None),
    ("my-store.myshopify.com", "/api", "placeholder"),
    ("shop.example.co.uk", "/", "example"),
    ("store.example.comx", "/", "example"),
    ("hydrogen.cache", "/x", "not a real domain"),
    ("web", "/", "service name"),
    ("orders-service", "/v1/orders", None),
    ("dashboard.stripe.com", "/test/payments", "website or CDN"),
    ("stripe.com", "/docs/api", "documentation"),
    ("abc.ngrok.io", "/webhook", "noise"),
    ("127.0.0.1", "/", "local"),
])
def test_host_problem(host, path, problem):
    from apimend.scan import host_problem

    assert host_problem(host, path) == problem


def test_test_paths():
    from apimend.scan import is_test_path

    for p in ("src/__tests__/a.ts", "pkg/client.test.ts", "tests/test_api.py", "app/models_test.go",
              "spec/user_spec.rb", ".github/workflows/ci.yml", "src/main/java/FooTest.java", "e2e/login.ts",
              "libs/integration_tests/chat.py", "pkg/standard-tests/x.py"):
        assert is_test_path(p), p
    for p in ("src/api.ts", "app/billing.py", "contest/run.py", "latest/main.go"):
        assert not is_test_path(p), p


def test_fixture_paths_for_spec_files():
    from apimend.scan import is_fixture_path

    assert is_fixture_path("tests/unit_tests/data/openapi.json")
    assert is_fixture_path("src/__mocks__/schema.graphql")
    assert not is_fixture_path("specs/orders-api.yaml")
    assert not is_fixture_path("docs/api/openapi.yaml")


def test_urls_only_in_tests_do_not_create_apis(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_client.py").write_text('requests.get("https://api.partner.io/v1/x")\n')
    (tmp_path / "client.test.ts").write_text('fetch("https://api.other.io/v1/y")\n')
    assert scan([tmp_path], tmp_path).apis == []
    (tmp_path / "client.py").write_text('requests.get("https://api.partner.io/v1/x")\n')
    assert [a.name for a in scan([tmp_path], tmp_path).apis] == ["partner"]


def test_operations_are_matched_to_the_schema_they_validate_against(tmp_path):
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas/store.graphql").write_text("type Query { products: [Product] } type Product { title: String }")
    (tmp_path / "schemas/account.graphql").write_text("type Query { customer: Customer } type Customer { email: String }")
    (tmp_path / "schemas/account.schema.json").write_text(json.dumps({"data": {"__schema": {
        "queryType": {"name": "Query"}, "types": []}}}))  # same schema in a second format: ignored
    (tmp_path / "src").mkdir()
    (tmp_path / "src/products.graphql").write_text("query P { products { title } }")
    (tmp_path / "src/account.ts").write_text("const Q = gql`query C { customer { email } }`;")
    apis = {a.name: a for a in scan([tmp_path], tmp_path).apis}
    assert set(apis) == {"store-graphql", "account-graphql"}
    assert apis["store-graphql"].operations == ["src/**/*.graphql"]
    assert apis["account-graphql"].operations == ["src/**/*.ts"]
    assert apis["account-graphql"].spec == "schemas/account.graphql"


def test_empty_repo(tmp_path):
    (tmp_path / "main.py").write_text('print("https://github.com/acme")\n')
    result = scan([tmp_path], tmp_path)
    assert result.apis == []
    (tmp_path / "apimend.yaml").write_text(to_yaml(result))
    with pytest.raises(ConfigError, match="uncomment"):
        load_config(tmp_path / "apimend.yaml")


def test_scan_cli(demo, monkeypatch, capsys):
    monkeypatch.chdir(demo)
    assert main(["scan"]) == 0
    out = capsys.readouterr().out
    assert "4 API(s) ready" in out and (demo / "apimend.yaml").exists()
    assert main(["scan"]) == 2  # refuses to overwrite
    assert main(["scan", "--force"]) == 0
    capsys.readouterr()
    assert main(["scan", "--format", "json"]) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert {a["name"] for a in inventory["apis"]} >= {"stripe", "acme-graphql"}
    assert main(["scan", "nope-dir"]) == 2
