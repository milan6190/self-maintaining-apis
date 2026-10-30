"""`apimend scan`: find the APIs a codebase depends on and write ``apimend.yaml``.

What it looks at (no network access, nothing is executed):

* API descriptions in the repo: OpenAPI/Swagger files, GraphQL schemas
  (SDL or introspection JSON), GraphQL operations (``.graphql`` files and
  ``gql`` templates in code), and GraphQL tooling configs (``codegen.yml``,
  ``.graphqlrc``) that tie a schema to its operations
* HTTP calls in source code: URL literals next to ``requests``, ``httpx``,
  ``fetch``, ``axios``, ``http.Get``, ``RestTemplate`` and similar calls
* base URLs in config: ``.env`` files, ``*.properties``, ``application.yml``,
  ``appsettings.json``, docker-compose and other config files
* Postman collections
* dependency manifests, matched against well-known API SDKs (``stripe``,
  ``@octokit/rest``, ``twilio``...)

Each API it finds is either ready to watch (a public spec from the registry,
or a spec/schema file in the repo) or written as a commented-out entry that
says what is missing (usually auth headers for a live endpoint).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import yaml

from .fetch import YAML_LOADER
from .gql.model import is_introspection
from .gql.operations import CODE_EXTS as GQL_CODE_EXTS
from .gql.operations import GRAPHQL_EXTS, extract_snippets
from .gql.parser import document_kind
from .registry import KnownApi, by_package, by_url

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", "vendor", ".next",
    ".nuxt", ".svelte-kit", "coverage", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".idea",
    ".vscode", "bower_components", ".terraform", ".apimend", "site-packages", "Pods", "DerivedData", ".dart_tool",
    ".serverless", ".cache", ".parcel-cache", "out", "obj", "bin", ".yarn", ".pnpm-store", "third_party",
}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "composer.lock",
              "gemfile.lock", "go.sum", "cargo.lock", "npm-shrinkwrap.json", "bun.lockb"}
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".scala", ".go", ".rb",
             ".php", ".cs", ".swift", ".rs", ".vue", ".svelte", ".dart", ".ex", ".exs", ".clj", ".groovy", ".m"}
CONFIG_EXTS = {".env", ".properties", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ".json", ".tfvars"}
STATIC_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".css", ".js", ".mjs", ".html", ".htm",
               ".md", ".pdf", ".zip", ".gz", ".tgz", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".xsd", ".dtd",
               ".txt", ".xml", ".jar", ".whl", ".exe", ".dmg", ".deb", ".rpm", ".map")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SPEC_BYTES = 50 * 1024 * 1024

NOISE_HOSTS = {
    "example.com", "example.org", "example.net", "www.example.com", "www.w3.org", "w3.org", "json-schema.org",
    "schema.org", "schemas.xmlsoap.org", "schemas.microsoft.com", "purl.org", "xmlns.com", "ogp.me",
    "github.com", "www.github.com", "gist.github.com", "raw.githubusercontent.com", "githubusercontent.com",
    "gitlab.com", "bitbucket.org", "stackoverflow.com", "en.wikipedia.org", "wikipedia.org", "youtube.com",
    "www.youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com", "www.facebook.com", "linkedin.com",
    "www.linkedin.com", "instagram.com", "www.google.com", "google.com", "fonts.googleapis.com", "fonts.gstatic.com",
    "unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "code.jquery.com", "img.shields.io", "shields.io",
    "badge.fury.io", "travis-ci.org", "travis-ci.com", "circleci.com", "codecov.io", "opensource.org",
    "www.apache.org", "apache.org", "creativecommons.org", "www.gnu.org", "gnu.org", "mit-license.org",
    "spdx.org", "semver.org", "keepachangelog.com", "conventionalcommits.org", "npmjs.com", "www.npmjs.com",
    "registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "pkg.go.dev", "golang.org", "go.dev",
    "maven.apache.org", "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "rubygems.org",
    "nuget.org", "www.nuget.org", "crates.io", "docs.rs", "readthedocs.org", "gravatar.com", "www.gravatar.com",
    "placehold.co", "via.placeholder.com", "placekitten.com", "picsum.photos", "httpbin.org", "jsonplaceholder.typicode.com",
    "reqres.in", "mozilla.org", "developer.mozilla.org", "www.mozilla.org", "ietf.org", "tools.ietf.org",
    "datatracker.ietf.org", "www.rfc-editor.org", "nodejs.org", "python.org", "docs.python.org", "www.python.org",
    "reactjs.org", "react.dev", "vuejs.org", "angular.io", "getbootstrap.com", "fontawesome.com", "localhost",
    "swagger.io", "petstore.swagger.io", "editor.swagger.io", "spec.openapis.org", "openapis.org",
    "www.googleapis.com/auth", "accounts.google.com", "login.microsoftonline.com", "schemas.android.com",
    "maps.googleapis.com", "www.googletagmanager.com", "www.google-analytics.com", "connect.facebook.net",
}
NOISE_HOSTS |= {"codeload.github.com", "objects.githubusercontent.com", "ngrok.com", "ngrok.io", "localtunnel.me",
                "webhook.site", "requestbin.com", "pipedream.net", "postman-echo.com", "mockbin.org"}
NOISE_SUFFIXES = (".example", ".example.com", ".test", ".invalid", ".local", ".localhost", ".readthedocs.io",
                  ".github.io", ".netlify.app", ".vercel.app", ".wikipedia.org", ".medium.com", ".substack.com",
                  ".ngrok.io", ".ngrok-free.app", ".ngrok.app", ".ngrok.dev", ".loca.lt", ".trycloudflare.com",
                  ".gitpod.io", ".githubpreview.dev", ".app.github.dev", ".repl.co")
# generic and common country-code-like TLDs; any two-letter TLD is accepted too
COMMON_TLDS = set("""com net org io dev app ai co info biz me tv gg so sh run page link live network services systems
digital finance bank insurance health travel mobi pro aero gov edu mil int eu asia global company solutions agency zone
world today media news blog design studio software tools works team chat email host hosting cloud xyz online site tech
store shop space website fun club life group center express cool money cash exchange market markets capital fund
financial credit tax legal law consulting management partners ventures vc security codes build engineering technology
energy auto car cars delivery shipping logistics games game social one plus top best click land rocks ninja guru wiki
education academy school university college training careers jobs work events photos video music art red blue black
green pink city berlin london nyc paris tokyo amsterdam""".split())
INTERNAL_TLDS = {"internal", "corp", "lan", "intranet", "private", "svc", "cluster", "home", "intra", "priv",
                 "local", "localdomain"}
NON_API_LABELS = {"cdn", "static", "assets", "images", "img", "media", "fonts", "font", "js", "css", "docs", "doc",
                  "dashboard", "console", "status", "help", "support", "blog", "www", "community", "forum", "learn",
                  "careers", "jobs", "go", "links", "link", "click", "email", "mail", "news", "press", "dl",
                  "download", "downloads", "releases", "s3"}
_PLACEHOLDER_LABEL = re.compile(r"^(?:(?:my|your)[-_]?\w*|example\w*|sample\w*|demo\w*|test\w*|dummy\w*|"
                                r"placeholder\w*|changeme|xxx+|foo|bar|baz|fake\w*|mock\w*|stub\w*)$")
_DOC_PATH = re.compile(r"^/(docs?|documentation|guides?|blog|help|support|learn|tutorials?|changelog|pricing|terms|"
                       r"privacy|legal|about|careers)(/|$)", re.I)
# Directories and files whose URLs are fixtures, docs or CI rather than what the app calls
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures", "fixture", "__fixtures__", "__mocks__",
             "mocks", "mock", "testdata", "test-data", "test_data", "cypress", "playwright", "stories", ".storybook",
             ".github", ".circleci", ".gitlab", ".devcontainer", "docs", "doc", "benchmarks", "bench", "testing"}
_TEST_FILE = re.compile(r"([._-](test|spec|e2e|stories|story|mock|mocks|fixture|fixtures)\.[a-z]+$)|"
                        r"(^test_.*\.py$)|(^conftest\.py$)|(Tests?\.(java|kt|cs|scala|swift|groovy)$)|"
                        r"(_test\.(go|py|rb|exs?)$)|(_spec\.rb$)", re.I)


_TEST_DIR = re.compile(r"(^|[_-])(tests?|specs?|e2e|mocks?|fixtures?|testdata|testing)($|[_-])", re.I)


_FIXTURE_DIR = re.compile(r"(^|[_-])(tests?|mocks?|fixtures?|testdata|e2e)($|[_-])|^__(tests|mocks|fixtures)__$", re.I)


def is_fixture_path(rel: str) -> bool:
    """For spec and schema files: `specs/` and `docs/` often hold the real API description, tests don't."""
    return any(_FIXTURE_DIR.search(d) for d in Path(rel).parts[:-1])


def is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    dirs = parts[:-1]
    return any(d.lower() in TEST_DIRS or _TEST_DIR.search(d) for d in dirs) or \
        bool(_TEST_FILE.search(parts[-1] if parts else ""))
LOCAL_HOSTS = re.compile(r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|host\.docker\.internal)$")

_URL_LITERAL = re.compile(r"""(?P<q>["'`])(?P<url>https?://[^"'`\s<>\\]+)""")
_ENV_LINE = re.compile(
    r"""^\s*(?:export\s+)?["']?(?P<key>[A-Za-z0-9_.\-]*?(?:url|uri|endpoint|host|base|api|server|address|domain|origin)"""
    r"""[A-Za-z0-9_.\-]*)["']?\s*[:=]\s*["']?(?P<url>https?://[^\s"'#,;]+)""", re.I)
_COMMENT = re.compile(r"^\s*(//|#|\*|/\*|<!--|--|;|')")
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}|\{[^}/]*\}|%[sd]|<[^>/]+>|:(?=[a-z_]+\b)[a-z_]+|#\{[^}]*\}")
_METHOD = r"(get|post|put|patch|delete|head)"
_CALLS = [
    re.compile(rf"\.{_METHOD}(?:ForObject|ForEntity|Async|_async)?\s*[(<]", re.I),
    re.compile(r"\bfetch\s*\("),
    re.compile(r"\b(?:urlopen|urlretrieve|Request)\s*\("),
    re.compile(rf"\bhttp\.({_METHOD})\s*\(", re.I),
    re.compile(r"\bhttp\.NewRequest(?:WithContext)?\s*\("),
    re.compile(r"\b(?:axios|request|got|ky|superagent|needle|httpx|requests|HTTParty|Faraday|RestClient|"
               r"WebClient|RestTemplate|OkHttpClient|HttpClient|HttpRequest|URI\.create|URI\.open|Net::HTTP|"
               r"reqwest|ureq|Uri\.parse|new\s+URL|new\s+Uri|curl|wget|\$http|this\.http)\b"),
    re.compile(r"\.(url|uri|baseUrl|baseURL|base_url|endpoint)\s*\(", re.I),
]
_EXPLICIT_METHOD = re.compile(r"""\b(?:method\s*[:=]\s*|http\.Method|HttpMethod\.|Method\.|-X\s*)["']?"""
                              r"""(GET|POST|PUT|PATCH|DELETE|HEAD|Get|Post|Put|Patch|Delete|Head)\b""")
_GO_NEWREQUEST = re.compile(r'NewRequest(?:WithContext)?\s*\([^,]*?"(GET|POST|PUT|PATCH|DELETE|HEAD)"')
_BASE_NAME = re.compile(r"(url|uri|endpoint|host|base|api|server|domain|origin)", re.I)
_ASSIGN = re.compile(r"""^\s*(?:export\s+)?(?:const|let|var|final|static|private|public|val|def)?[\w\s.:<>\[\]]*?"""
                     r"""(?P<name>[A-Za-z_][\w.]*)\s*(?::\s*[\w.<>\[\]|]+)?\s*(?::=|=|:)\s*["'`f]""")


# --------------------------------------------------------------------- results


@dataclass
class Evidence:
    file: str
    line: int
    detail: str
    test: bool = False  # found in tests, fixtures, docs or CI rather than app code

    def __str__(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class Call:
    method: str | None  # None = not known
    url: str  # with placeholders as {}
    templated: bool
    where: Evidence


@dataclass
class FoundApi:
    key: str
    name: str = ""
    title: str = ""
    kind: str = "rest"  # rest | openapi | graphql
    known: KnownApi | None = None
    spec: str | None = None  # spec file path (relative to the output) or URL
    graphql_url: str | None = None
    calls: list[Call] = field(default_factory=list)
    bases: list[tuple[str, Evidence]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)  # globs
    operation_count: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        out = []
        if self.known:
            out.append("registry")
        if self.spec and not self.known:
            out.append("spec")
        if self.calls:
            out.append("code")
        if self.bases:
            out.append("config")
        if any(e.detail.startswith("dependency") for e in self.evidence):
            out.append("dependency")
        if any(e.detail.startswith("postman") for e in self.evidence):
            out.append("postman")
        if self.operations:
            out.append("operations")
        return out

    @property
    def ready(self) -> bool:
        """True when it can be watched without credentials or further edits."""
        return bool(self.spec)

    def get_endpoints(self) -> list[Call]:
        """Static URLs that are safe to call: GET (or unknown method), no placeholders."""
        seen, out = set(), []
        for c in self.calls:
            if c.method in (None, "GET") and not c.templated and c.url not in seen:
                seen.add(c.url)
                out.append(c)
        return out

    def to_dict(self) -> dict:
        methods = Counter(c.method or "?" for c in self.calls)
        return {
            "name": self.name, "title": self.title, "kind": self.kind, "ready": self.ready,
            "sources": self.sources, "spec": self.spec, "graphql": self.graphql_url,
            "calls": len(self.calls), "methods": dict(methods),
            "base_urls": sorted({u for u, _ in self.bases}),
            "operations": self.operations, "operation_count": self.operation_count,
            "evidence": [str(e) + f" ({e.detail})" for e in self.evidence[:20]],
            "notes": self.notes,
        }


@dataclass
class ScanResult:
    roots: list[str]
    apis: list[FoundApi]
    files_scanned: int
    skipped_local: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "roots": self.roots, "files_scanned": self.files_scanned,
            "apis": [a.to_dict() for a in self.apis], "warnings": self.warnings,
        }


# ------------------------------------------------------------------- file walk


def _git_files(root: Path) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]


def list_files(root: Path, max_files: int = 200_000) -> list[Path]:
    rels = _git_files(root)
    if rels is None:
        rels = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
            rel_dir = os.path.relpath(dirpath, root)
            for f in filenames:
                rels.append(f if rel_dir == "." else os.path.join(rel_dir, f))
                if len(rels) >= max_files:
                    break
    out = []
    for rel in rels:
        parts = Path(rel).parts
        if any(p in SKIP_DIRS for p in parts[:-1]) or parts[-1].lower() in SKIP_FILES:
            continue
        name = parts[-1].lower()
        if name.endswith((".min.js", ".bundle.js", ".chunk.js", ".map")):
            continue
        out.append(root / rel)
        if len(out) >= max_files:
            break
    return out


def _read(path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------- URL handling


def normalize_url(raw: str) -> tuple[str, str, str, bool] | None:
    """-> (host, path, display_url, templated) or None for unusable URLs."""
    raw = raw.rstrip(".,;)]'\"")
    while raw.endswith("}") and raw.count("}") > raw.count("{"):
        raw = raw[:-1].rstrip(".,;)]'\"")
    templated = bool(_PLACEHOLDER.search(raw.split("?", 1)[0].split("://", 1)[-1]))
    shown = _PLACEHOLDER.sub("{}", raw) if templated else raw
    try:
        parts = urlsplit(shown.replace("{}", "x"))
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or not re.match(r"^[a-z0-9.*-]+$", host):
        return None
    netloc = urlsplit(shown).netloc
    if "{}" in netloc:
        if re.match(r"^\{\}(:\d+)?$", netloc):
            return None  # the whole host is a variable: nothing to group by
        host = re.sub(r"^x\.", "*.", host)
    path = parts.path or "/"
    return host, path, shown, templated


def host_problem(host: str, path: str) -> str | None:
    """Why this URL is not an API worth watching, or None if it may be one."""
    if is_local(host):
        return "local"
    if host in NOISE_HOSTS or host.endswith(NOISE_SUFFIXES) or f"{host}{path}".startswith("www.googleapis.com/auth"):
        return "noise"
    labels = host.split(".")
    if any(label.startswith("example") for label in labels):
        return "example"
    if path.lower().endswith(STATIC_EXTS):
        return "static file"
    if len(labels) == 1:  # a container or service name: keep only clear API paths
        return None if api_path(path) else "service name"
    tld = labels[-1]
    if not ((len(tld) == 2 and tld.isalpha()) or tld in COMMON_TLDS or tld in INTERNAL_TLDS):
        return "not a real domain"
    if len(labels) >= 3 and _PLACEHOLDER_LABEL.match(labels[0]):
        return "placeholder"
    if _DOC_PATH.match(path):
        return "documentation"
    if labels[0] in NON_API_LABELS and not api_path(path):
        return "website or CDN"
    return None


def is_noise(host: str, path: str) -> bool:
    return host_problem(host, path) not in (None, "local")


def is_local(host: str) -> bool:
    return bool(LOCAL_HOSTS.match(host))


def api_path(path: str) -> bool:
    return bool(re.search(r"/(api|rest|graphql|gql|v\d+(\.\d+)?|services?|oauth2?|webhooks?|rpc)(/|$)", path, re.I))


def looks_like_api(host: str, path: str) -> bool:
    if host.startswith("api.") or ".api." in host or host.startswith("api-") or "-api." in host:
        return True
    return api_path(path)


def registered_domain(host: str) -> str:
    labels = host.lstrip("*.").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "net", "org", "gov", "ac", "edu"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_graphql_path(path: str) -> bool:
    return bool(re.search(r"/(graphql|gql)/?$", path, re.I))


# ---------------------------------------------------------------- the scanner


class Scanner:
    def __init__(self, roots: list[Path], out_dir: Path, include_local: bool = False):
        self.roots = [r.resolve() for r in roots]
        self.out_dir = out_dir.resolve()
        self.include_local = include_local
        self.apis: dict[str, FoundApi] = {}
        self.skipped_local = 0
        self.warnings: list[str] = []
        self.spec_files: list[tuple[str, dict, Path]] = []  # (kind, info, path)
        self.op_files: list[tuple[Path, int]] = []
        self.test_op_files: list[Path] = []  # operations in tests: left out of `operations:`
        self.codegen: list[tuple[Path, Any, list[str]]] = []  # (config path, schema source, documents)
        self.files = 0

    # -- naming
    def rel(self, path: Path) -> str:
        try:
            return os.path.relpath(path, self.out_dir)
        except ValueError:
            return str(path)

    def api_for(self, key: str) -> FoundApi:
        if key not in self.apis:
            self.apis[key] = FoundApi(key)
        return self.apis[key]

    def api_for_url(self, host: str, path: str, url: str = "") -> FoundApi:
        known = by_url(host, path)
        if known:
            api = self.api_for(f"known:{known.name}")
            api.known = known
            return api
        if is_graphql_path(path):
            api = self.api_for(f"graphql:{host}{path.rstrip('/')}")
            api.kind = "graphql"
            if url and (not api.graphql_url or "{}" in api.graphql_url and "{}" not in url):
                api.graphql_url = url.split("?", 1)[0]
            return api
        return self.api_for(f"host:{host}")

    # -- per-file work
    def run(self) -> ScanResult:
        for root in self.roots:
            self._root = root
            for path in list_files(root):
                self.files += 1
                self.scan_file(path)
        self.link_specs()
        self.link_graphql()
        return ScanResult([str(r) for r in self.roots], self.finalize(), self.files, self.skipped_local, self.warnings)

    def is_test(self, path: Path) -> bool:
        try:
            return is_test_path(str(path.relative_to(self._root)))
        except ValueError:
            return False

    def is_fixture(self, path: Path) -> bool:
        try:
            return is_fixture_path(str(path.relative_to(self._root)))
        except ValueError:
            return False

    def scan_file(self, path: Path) -> None:
        name = path.name.lower()
        ext = path.suffix.lower()
        if name in ("package.json", "composer.json") or name.startswith("requirements") and ext in (".txt", ".in") or \
                name in ("pyproject.toml", "pipfile", "setup.py", "setup.cfg", "go.mod", "gemfile", "pom.xml",
                         "build.gradle", "build.gradle.kts", "cargo.toml") or ext == ".csproj":
            self.scan_manifest(path, name)
        if ext in GRAPHQL_EXTS or ext == ".graphqls":
            self.scan_graphql_file(path)
            return
        if self.is_fixture(path) and ext in (".json", ".yaml", ".yml"):
            return  # spec and schema files under tests are fixtures
        if name in ("codegen.yml", "codegen.yaml", "codegen.json", ".graphqlrc", ".graphqlrc.yml", ".graphqlrc.yaml",
                    ".graphqlrc.json", "graphql.config.yml", "graphql.config.yaml", "graphql.config.json"):
            self.scan_codegen(path)
            return
        if ext in (".json", ".yaml", ".yml"):
            if self.scan_structured(path):
                return
        if ext in CODE_EXTS:
            self.scan_code(path)
        elif ext in CONFIG_EXTS or name.startswith(".env") or name.endswith(".env") or "docker-compose" in name:
            self.scan_config(path)

    def scan_manifest(self, path: Path, name: str) -> None:
        text = _read(path)
        if text is None:
            return
        deps: list[str] = []
        if name in ("package.json", "composer.json"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require",
                        "require-dev"):
                deps.extend((data.get(key) or {}).keys())
        elif name.startswith("requirements"):
            for line in text.splitlines():
                m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                if m and not line.lstrip().startswith(("-", "#")):
                    deps.append(m.group(1))
        elif name in ("pyproject.toml", "pipfile", "setup.py", "setup.cfg", "cargo.toml"):
            deps.extend(re.findall(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=~!;].*?)?["']""",
                                   text))
            deps.extend(re.findall(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", text, re.M))
        elif name == "go.mod":
            deps.extend(re.findall(r"^\s*(?:require\s+)?([a-z0-9.\-]+\.[a-z]+/[^\s]+)\s+v", text, re.M))
        elif name == "gemfile":
            deps.extend(re.findall(r"""^\s*gem\s+["']([^"']+)["']""", text, re.M))
        elif name == "pom.xml":
            deps.extend(re.findall(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", text))
        elif name.startswith("build.gradle"):
            deps.extend(re.findall(r"""["'][\w.\-]+:([\w.\-]+)(?::[^"']*)?["']""", text))
        elif path.suffix.lower() == ".csproj":
            deps.extend(re.findall(r"""<PackageReference\s+Include\s*=\s*["']([^"']+)["']""", text))
        for dep in dict.fromkeys(deps):
            known = by_package(dep)
            if known:
                api = self.api_for(f"known:{known.name}")
                api.known = known
                api.evidence.append(Evidence(self.rel(path), 0, f"dependency `{dep}`"))

    def scan_graphql_file(self, path: Path) -> None:
        text = _read(path, MAX_SPEC_BYTES)
        if text is None:
            return
        kind = document_kind(text)
        if kind == "schema" and not self.is_fixture(path):
            self.spec_files.append(("graphql", {}, path))
        elif kind == "operations" and not self.is_test(path):
            self.op_files.append((path, len(re.findall(r"^\s*(query|mutation|subscription)\b", text, re.M)) or 1))

    def scan_codegen(self, path: Path) -> None:
        text = _read(path)
        try:
            data = yaml.safe_load(text or "") if path.suffix != ".json" else json.loads(text or "{}")
        except (yaml.YAMLError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        projects = [data] + [p for p in (data.get("projects") or {}).values() if isinstance(p, dict)]
        for proj in projects:
            schema = proj.get("schema")
            docs = proj.get("documents") or []
            if isinstance(docs, str):
                docs = [docs]
            if schema:
                self.codegen.append((path, schema, [d for d in docs if isinstance(d, str)]))

    def scan_structured(self, path: Path) -> bool:
        """OpenAPI/Swagger, introspection results and Postman collections. True if handled."""
        head = _read_head(path)
        if head is None:
            return False
        if re.search(r"""(^|["'\s{,])(openapi|swagger)["']?\s*:\s*["']?[23]""", head[:4000], re.M):
            text = _read(path, MAX_SPEC_BYTES)
            doc = _parse(text, path)
            if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc) and ("paths" in doc or "webhooks" in doc):
                self.spec_files.append(("openapi", doc, path))
                return True
        if "__schema" in head[:4000]:
            doc = _parse(_read(path, MAX_SPEC_BYTES), path)
            if is_introspection(doc):
                self.spec_files.append(("graphql", {}, path))
                return True
        if "_postman_id" in head or "schema.getpostman.com" in head:
            self.scan_postman(path)
            return True
        return False

    def scan_postman(self, path: Path) -> None:
        doc = _parse(_read(path), path)
        if not isinstance(doc, dict):
            return
        variables = {v.get("key"): v.get("value") for v in doc.get("variable") or [] if isinstance(v, dict)}

        def walk(items: Iterable) -> None:
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                if "item" in it:
                    walk(it["item"])
                req = it.get("request")
                if not isinstance(req, dict):
                    continue
                url = req.get("url")
                raw = url.get("raw") if isinstance(url, dict) else url
                if not isinstance(raw, str):
                    continue
                for k, v in variables.items():
                    if isinstance(v, str) and v.startswith("http"):
                        raw = raw.replace("{{" + str(k) + "}}", v.rstrip("/"))
                if not raw.startswith("http"):
                    continue
                self.add_url(raw, (req.get("method") or "GET").upper(),
                             Evidence(self.rel(path), 0, f"postman `{it.get('name', '')}`", self.is_test(path)),
                             role="call")

        walk(doc.get("item"))

    def scan_code(self, path: Path) -> None:
        text = _read(path)
        if text is None:
            return
        lines = text.splitlines()
        rel = self.rel(path)
        test = self.is_test(path)
        for i, line in enumerate(lines):
            if "http" not in line or len(line) > 2000:
                continue
            if _COMMENT.match(line):
                continue
            for m in _URL_LITERAL.finditer(line):
                prev = lines[i - 1] if i > 0 else ""
                window = " ".join(lines[max(0, i - 2): i + 4])
                method, role = _classify(line, prev, window)
                self.add_url(m.group("url"), method, Evidence(rel, i + 1, role, test), role)
        if path.suffix.lower() in GQL_CODE_EXTS and ("gql" in text or "graphql" in text or "GraphQL" in text):
            count = 0
            for snip in extract_snippets(str(path), text):
                if document_kind(snip.text) == "operations":
                    count += len(re.findall(r"\b(query|mutation|subscription)\b\s*[A-Za-z_({]", snip.text)) or 1
            if count and not self.is_test(path):
                self.op_files.append((path, count))
            elif count:
                self.test_op_files.append(path)

    def scan_config(self, path: Path) -> None:
        text = _read(path)
        if text is None:
            return
        rel = self.rel(path)
        test = self.is_test(path)
        for i, line in enumerate(text.splitlines()):
            if "http" not in line or _COMMENT.match(line):
                continue
            m = _ENV_LINE.match(line)
            if m:
                self.add_url(m.group("url"), None, Evidence(rel, i + 1, f"config `{m.group('key')}`", test), "base")

    def add_url(self, raw: str, method: str | None, ev: Evidence, role: str) -> None:
        parsed = normalize_url(raw)
        if parsed is None:
            return
        host, path, shown, templated = parsed
        problem = host_problem(host, path)
        if problem == "local":
            self.skipped_local += 1
            if not self.include_local:
                return
        elif problem:
            return
        if role == "literal" and not looks_like_api(host, path):
            return
        if role == "base" and not looks_like_api(host, path) and \
                not re.search(r"api|endpoint|graphql|gql|service", ev.detail, re.I):
            return
        api = self.api_for_url(host, path, shown)
        if role == "call" or (role == "literal" and method):
            api.calls.append(Call(method, shown, templated, ev))
        else:
            api.bases.append((shown, ev))
        api.evidence.append(ev)

    # -- linking specs and GraphQL
    def link_specs(self) -> None:
        for kind, doc, path in self.spec_files:
            if kind != "openapi":
                continue
            host = _spec_host(doc)
            target = None
            if host:
                for key, api in self.apis.items():
                    if key == f"host:{host}" and not api.known:
                        target = api
                        break
            if target is None:
                title = (doc.get("info") or {}).get("title") or path.stem
                target = self.api_for(f"spec:{self.rel(path)}")
                target.title = title
            target.kind = "openapi"
            target.title = target.title or (doc.get("info") or {}).get("title") or ""
            target.spec = self.rel(path)
            target.evidence.append(Evidence(self.rel(path), 0, "OpenAPI/Swagger spec in the repo"))

    def link_graphql(self) -> None:
        schemas = [p for k, _, p in self.spec_files if k == "graphql"]
        graphql_apis = [a for a in self.apis.values() if a.kind == "graphql" or (a.known and a.known.kind == "graphql")]

        # 1. tooling configs map schemas to operations precisely
        claimed: set[str] = set()
        for cfg_path, schema, documents in self.codegen:
            sources = schema if isinstance(schema, list) else [schema]
            for src in sources:
                if isinstance(src, dict):  # {url: {headers: ...}}
                    src = next(iter(src), None)
                if not isinstance(src, str):
                    continue
                if src.startswith("http"):
                    parsed = normalize_url(src)
                    if not parsed:
                        continue
                    api = self.api_for_url(parsed[0], parsed[1])
                    api.kind = "graphql"
                    if not api.known:
                        api.graphql_url = src
                elif not any(ch in src for ch in "*?"):
                    local = (cfg_path.parent / src).resolve()
                    if not local.exists():
                        continue
                    api = self.api_for(f"spec:{self.rel(local)}")
                    api.kind = "graphql"
                    api.spec = self.rel(local)
                    claimed.add(str(local))
                else:
                    continue
                globs = [self.rel(cfg_path.parent / d) if not os.path.isabs(d) else d for d in documents
                         if not d.startswith("!")]
                api.operations.extend(g for g in globs if g not in api.operations)
                api.evidence.append(Evidence(self.rel(cfg_path), 0, "GraphQL tooling config"))
                if api not in graphql_apis:
                    graphql_apis.append(api)

        # 2. schema files not claimed by a tooling config (one per schema: x.graphql wins over x.json)
        by_stem: dict[tuple[str, str], Path] = {}
        for p in schemas:
            if str(p.resolve()) in claimed:
                continue
            stem = re.sub(r"[._-]?(schema|introspection)$", "", p.name.split(".")[0], flags=re.I) or p.stem
            k = (str(p.parent), stem.lower())
            if k not in by_stem or by_stem[k].suffix == ".json":
                by_stem[k] = p
        unschema = [a for a in graphql_apis if not a.spec and not (a.known and a.known.kind == "graphql")]
        for (_, stem), p in sorted(by_stem.items()):
            target = unschema[0] if len(unschema) == 1 and len(by_stem) == 1 else None
            if target is None:
                target = self.api_for(f"spec:{self.rel(p)}")
                target.title = f"GraphQL schema {stem}"
                target.name = _slug(stem) + ("" if "graphql" in stem.lower() else "-graphql")
                graphql_apis.append(target)
            target.kind = "graphql"
            target.spec = self.rel(p)
            target.evidence.append(Evidence(self.rel(p), 0, "GraphQL schema in the repo"))

        # 3. operations not covered by a tooling config
        if not self.op_files:
            return
        from .gql.operations import expand_patterns

        matched: dict[int, set[str]] = {}
        for api in graphql_apis:
            if api.operations:
                matched[id(api)] = {os.path.realpath(f) for f in expand_patterns(api.operations, self.out_dir)}
        covered = set().union(*matched.values()) if matched else set()
        ops = [(p, n, os.path.realpath(p)) for p, n in self.op_files]
        for api in graphql_apis:
            if api.operations:
                api.operation_count = sum(n for _, n, real in ops if real in matched[id(api)])
        remaining = [(p, n) for p, n, real in ops if real not in covered]
        if not remaining:
            return
        targets = [a for a in graphql_apis if not a.operations]
        if len(targets) == 1:
            targets[0].operations = _globs_for([p for p, _ in remaining], self.out_dir, self.test_op_files)
            targets[0].operation_count = sum(n for _, n in remaining)
            return
        if len(targets) > 1:
            remaining = self.assign_by_validation(targets, remaining)
        if remaining:
            total = sum(n for _, n in remaining)
            files = [self.rel(p) for p, _ in remaining]
            sample = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
            where = "but no GraphQL endpoint or schema" if not graphql_apis else "that match no single GraphQL API"
            self.warnings.append(f"{total} GraphQL operation(s) in {len(files)} file(s) ({sample}) {where}; "
                                 "add them to the right API's `operations:` in apimend.yaml")

    def assign_by_validation(self, targets: list[FoundApi], files: list[tuple[Path, int]]) -> list[tuple[Path, int]]:
        """Give each operation file to the local schema it validates against best. Returns the leftovers."""
        from .gql.operations import load_operations, validate
        from .specs import load_spec

        schemas = []
        for api in targets:
            if api.spec and not api.spec.startswith("http"):
                try:
                    schemas.append((api, load_spec(str(self.out_dir / api.spec))))
                except Exception as e:  # noqa: BLE001 - a broken schema file just takes no part
                    self.warnings.append(f"could not read GraphQL schema {api.spec}: {e}")
        if not schemas:
            return files
        assigned: dict[int, list[tuple[Path, int]]] = {id(a): [] for a, _ in schemas}
        leftover = []
        for p, n in files:
            opset = load_operations([str(p)])
            if not opset.docs:
                leftover.append((p, n))
                continue
            scores = []
            for api, model in schemas:
                issues, _ = validate(model, opset)
                scores.append((sum(1 for i in issues if i.severity == "error" and i.kind != "unknown-fragment"), api))
            scores.sort(key=lambda s: s[0])
            best = scores[0]
            if len(scores) == 1 or best[0] < scores[1][0]:
                assigned[id(best[1])].append((p, n))
            else:
                leftover.append((p, n))
        apis = {id(a): a for a, _ in schemas}
        all_assigned = {os.path.realpath(p): k for k, fs in assigned.items() for p, _ in fs}
        for k, fs in assigned.items():
            if not fs:
                continue
            api = apis[k]
            globs = _globs_for([p for p, _ in fs], self.out_dir, self.test_op_files)
            covered = {os.path.realpath(f) for f in _expand(globs, self.out_dir)}
            # if the globs would also pick up another API's files, use per-folder globs or file names
            if any(all_assigned.get(f, k) != k for f in covered):
                globs = []
                for p, _ in fs:
                    folder = f"{self.rel(p.parent)}/*{p.suffix}"
                    in_folder = {os.path.realpath(f) for f in _expand([folder], self.out_dir)}
                    if all(all_assigned.get(f, k) == k for f in in_folder if f in all_assigned):
                        g = folder
                    else:
                        g = self.rel(p)
                    if g not in globs:
                        globs.append(g)
                globs = _with_test_excludes(globs, self.out_dir, self.test_op_files)
            api.operations = globs
            api.operation_count = sum(n for _, n in fs)
        return leftover

    # -- final shape
    def finalize(self) -> list[FoundApi]:
        known_domains = {registered_domain(h): a for a in self.apis.values() if a.known for h in a.known.hosts}
        out = []
        used: set[str] = set()
        for key, api in self.apis.items():
            if not api.known and key.startswith("host:"):
                # other hosts of a provider we already watch (dashboard.stripe.com, files.stripe.com)
                owner = known_domains.get(registered_domain(key[5:]))
                if owner is not None:
                    owner.evidence.extend(api.evidence)
                    continue
            app_evidence = [e for e in api.evidence if not e.test]
            if not api.known and not api.spec and not api.operations and not app_evidence:
                continue  # only seen in tests, fixtures, docs or CI
            if api.known:
                api.title = api.known.title
                api.name = api.known.name
                api.kind = api.known.kind
                if not api.spec or api.spec.startswith("http"):
                    api.spec = api.known.spec
            else:
                if key.startswith("host:"):
                    host = key[5:]
                    api.title = api.title or host
                    api.name = _slug_host(host)
                elif key.startswith("graphql:"):
                    host = key[8:].split("/", 1)[0]
                    api.title = api.title or f"GraphQL at {key[8:]}"
                    api.name = _slug_host(host) + "-graphql"
                    api.graphql_url = api.graphql_url or "https://" + key[8:]
                elif not api.name:
                    api.name = _slug(api.title or Path(key.split(":", 1)[1]).stem)
                if api.kind == "graphql" and not api.graphql_url and not api.spec:
                    for c in api.calls:
                        if not c.templated:
                            api.graphql_url = c.url
                            break
            if not api.calls and not api.bases and not api.spec and not api.operations and not api.known:
                continue
            base, n = api.name or "api", 2
            api.name = base
            while api.name in used:
                api.name = f"{base}-{n}"
                n += 1
            used.add(api.name)
            out.append(api)
        out.sort(key=lambda a: (not a.ready, a.name))
        return out


def _expand(globs: list[str], base: Path) -> list[str]:
    from .gql.operations import expand_patterns

    return expand_patterns(globs, base)


def _read_head(path: Path, n: int = 8192) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read(n)
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", "replace")


def _parse(text: str | None, path: Path) -> Any:
    if text is None:
        return None
    try:
        if path.suffix.lower() == ".json":
            return json.loads(text)
        return yaml.load(text, Loader=YAML_LOADER)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None


_ENV_DEFAULT = re.compile(r"(\b(os\.)?environ\b[\w.]*\.get|\bgetenv|\bprocess\.env\b|\bconfig\w*\.get|"
                          r"\bsettings\.get|\bos\.Getenv|\bENV\.fetch|\bSystem\.getenv)", re.I)


def _classify(line: str, prev: str, window: str) -> tuple[str | None, str]:
    """Guess the HTTP method and whether the URL is called, configured, or just mentioned."""
    if _ENV_DEFAULT.search(line):
        return None, "base"  # a default value for a configured base URL
    method = None
    m = _GO_NEWREQUEST.search(line) or _EXPLICIT_METHOD.search(window)
    if m:
        method = m.group(1).upper()
    called = False
    for pat in _CALLS:
        cm = pat.search(line) or pat.search(prev)
        if cm:
            called = True
            if method is None and cm.groups() and cm.group(1) and cm.group(1).lower() in (
                    "get", "post", "put", "patch", "delete", "head"):
                method = cm.group(1).upper()
            break
    if called:
        if method is None and re.search(r"\b(fetch|urlopen|curl|wget|URI\.open)\b", line + prev):
            method = "GET"
        return method, "call"
    am = _ASSIGN.match(line)
    if am and _BASE_NAME.search(am.group("name")):
        return None, "base"
    return method, "literal"


def _spec_host(doc: dict) -> str | None:
    servers = doc.get("servers") or []
    for s in servers:
        url = s.get("url") if isinstance(s, dict) else None
        if isinstance(url, str) and url.startswith("http"):
            parsed = normalize_url(url)
            if parsed:
                return parsed[0]
    if doc.get("host"):
        return str(doc["host"]).split(":")[0].lower()
    return None


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:48] or "api"


def _slug_host(host: str) -> str:
    labels = [x for x in host.replace("*.", "").split(".") if x]
    if len(labels) > 1:
        labels = labels[:-1]  # drop the TLD
        if len(labels) > 1 and labels[-1] in ("co", "com", "net", "org", "gov", "ac"):
            labels = labels[:-1]
    while len(labels) > 1 and labels[0] in ("api", "www", "rest", "gateway"):
        labels = labels[1:]
    name = _slug("-".join(labels))
    if len(name) < 3:  # api.x.ai -> "x-ai", not "x"
        name = _slug("-".join(x for x in host.split(".") if x not in ("api", "www")))
    return name or "api"


TEST_EXCLUDES = ["!**/*.test.*", "!**/*.spec.*", "!**/__tests__/**", "!**/tests/**", "!**/e2e/**"]


def _globs_for(files: list[Path], out_dir: Path, test_files: list[Path] | None = None) -> list[str]:
    return _with_test_excludes(_plain_globs(files, out_dir), out_dir, test_files)


def _with_test_excludes(pats: list[str], out_dir: Path, test_files: list[Path] | None) -> list[str]:
    """Add exclusions when the globs would also pick up operations from test files."""
    if test_files:
        hit = {os.path.realpath(f) for f in _expand(pats, out_dir)}
        if any(os.path.realpath(t) in hit for t in test_files):
            return pats + [x for x in TEST_EXCLUDES if x not in pats]
    return pats


def _plain_globs(files: list[Path], out_dir: Path) -> list[str]:
    pats: list[str] = []
    for f in files:
        try:
            rel = Path(os.path.relpath(f, out_dir))
        except ValueError:
            rel = f
        ext = rel.suffix
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        pat = f"{top}/**/*{ext}" if top and top != ".." else (f"{rel.parent}/*{ext}" if len(rel.parts) > 1 else f"*{ext}")
        if pat not in pats:
            pats.append(pat)
    return pats


def scan(roots: list[Path], out_dir: Path, include_local: bool = False) -> ScanResult:
    return Scanner(roots, out_dir, include_local).run()


# --------------------------------------------------------------- config output


def _q(s: str) -> str:
    """Quote for YAML, keeping ${...} literal (apimend expands env vars at load time)."""
    return json.dumps(s)


def _endpoint_name(c: Call, used: set[str]) -> str:
    parts = [p for p in urlsplit(c.url).path.split("/") if p and not re.match(r"^v\d+(\.\d+)?$", p)]
    base = _slug("-".join(parts[-2:])) if parts else "root"
    name, n = base, 2
    while name in used:
        name = f"{base}-{n}"
        n += 1
    used.add(name)
    return name


def to_yaml(result: ScanResult, enable_live: bool = False, max_endpoints: int = 5) -> str:
    lines = [
        f"# Generated by `apimend scan` on {date.today().isoformat()}.",
        "# Entries marked ready need no further setup. Commented ones say what is missing.",
        "# Next: review this file, then run `apimend snapshot` to record a baseline.",
        "store: .apimend",
        "fail_on: breaking",
        "",
        "apis:",
    ]
    ready = 0
    for api in result.apis:
        summary = _summary(api)
        if api.ready:
            ready += 1
            lines.append(f"  # {api.title}: {summary}")
            lines.extend(_api_block(api, commented=False, enable_live=enable_live, max_endpoints=max_endpoints))
        else:
            live = api.kind == "graphql" and api.graphql_url or api.get_endpoints()
            if enable_live and live:
                ready += 1
                lines.append(f"  # {api.title}: {summary}")
                lines.append("  # Live checks enabled by --enable-live; add `headers:` if the API needs auth.")
                lines.extend(_api_block(api, commented=False, enable_live=True, max_endpoints=max_endpoints))
            else:
                lines.append(f"  # {api.title}: {summary}")
                lines.append(f"  # To watch it: {_todo(api)}")
                lines.extend("  # " + ln[2:] if ln.startswith("  ") else "# " + ln
                             for ln in _api_block(api, commented=True, enable_live=True, max_endpoints=max_endpoints))
        lines.append("")
    if not ready:
        lines.insert(lines.index("apis:"), "# No API could be enabled automatically; uncomment an entry below.")
        lines[lines.index("apis:")] = "apis: []"
    return "\n".join(lines).rstrip() + "\n"


def _summary(api: FoundApi) -> str:
    bits = []
    if api.calls:
        methods = Counter(c.method or "?" for c in api.calls)
        m = ", ".join(f"{k} {v}" for k, v in sorted(methods.items()))
        bits.append(f"{len(api.calls)} call(s) ({m}), e.g. {api.calls[0].where}")
    if api.bases:
        bits.append(f"base URL in {api.bases[0][1]}")
    deps = [e.detail.split("`")[1] for e in api.evidence if e.detail.startswith("dependency")]
    if deps:
        bits.append(f"depends on {', '.join(sorted(set(deps))[:3])}")
    if api.spec and not api.spec.startswith("http"):
        bits.append(f"spec {api.spec}")
    if api.operations:
        bits.append(f"{api.operation_count or 'some'} operation(s) in {', '.join(api.operations)}")
    return "; ".join(bits) or "found"


def _todo(api: FoundApi) -> str:
    if api.kind == "graphql" and api.graphql_url and "{}" in api.graphql_url:
        return "fill in the {} parts of the URL, add auth headers if needed, then uncomment"
    if api.kind == "graphql":
        return "uncomment; introspection must be enabled, add auth headers if needed (or set `spec:` to a schema file)"
    if api.get_endpoints():
        return "uncomment and add auth headers if needed, or set `spec:` to its OpenAPI file"
    return "set `spec:` to its OpenAPI file, or add a GET endpoint under `endpoints:`"


def _env_name(api: FoundApi) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", api.name.upper()).strip("_") + "_TOKEN"


def _api_block(api: FoundApi, commented: bool, enable_live: bool, max_endpoints: int) -> list[str]:
    out = [f"  - name: {api.name}"]
    if api.spec:
        out.append(f"    spec: {_q(api.spec)}")
    elif api.kind == "graphql" and api.graphql_url:
        out.append(f"    graphql: {_q(api.graphql_url)}")
    if not api.spec and (api.kind == "graphql" or api.get_endpoints()):
        # commented in enabled entries: an unset ${VAR} would stop the config from loading
        c = "" if commented else "# "
        out.append(f"    {c}headers:")
        out.append(f"    {c}  Authorization: \"Bearer ${{{_env_name(api)}}}\"")
    if api.operations:
        out.append("    operations:")
        out.extend(f"      - {_q(g)}" for g in api.operations)
    eps = api.get_endpoints() if api.kind != "graphql" and (commented or enable_live or not api.spec) else []
    if eps and (not api.spec or enable_live):
        out.append("    endpoints:")
        used: set[str] = set()
        for c in eps[:max_endpoints]:
            out.append(f"      - name: {_endpoint_name(c, used)}   # {c.where}")
            out.append(f"        url: {_q(c.url)}")
        if len(eps) > max_endpoints:
            out.append(f"      # ...{len(eps) - max_endpoints} more GET URL(s) found")
    if not api.spec and not eps and api.kind != "graphql":
        out.append("    spec: \"path/or/url/to/openapi.yaml\"")
        if api.bases:
            out.append(f"    # base URL found: {api.bases[0][0]}")
    return out


def summary_table(result: ScanResult) -> str:
    rows = [("API", "Type", "Found via", "Watch with", "Status")]
    for a in result.apis:
        watch = "spec" if a.spec else ("introspection" if a.kind == "graphql" and a.graphql_url else
                                       f"{len(a.get_endpoints())} live endpoint(s)" if a.get_endpoints() else "-")
        if a.operations:
            watch += f" + {a.operation_count or '?'} operation(s)"
        rows.append((a.name, a.kind, ", ".join(a.sources), watch, "ready" if a.ready else "needs setup"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)
