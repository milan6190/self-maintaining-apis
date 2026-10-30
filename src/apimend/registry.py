"""Well-known public APIs and where their official machine-readable descriptions live.

`apimend scan` uses this to turn "the code calls api.stripe.com" or "the project
depends on `stripe`" into a watchable spec without any setup. Every spec URL
here is published by the API provider in a public repository and needs no
credentials. Contributions welcome: add an entry and a test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KnownApi:
    name: str
    title: str
    spec: str
    hosts: tuple[str, ...]
    packages: frozenset[str] = field(default_factory=frozenset)
    path_prefix: str = ""  # only URLs under this path (e.g. Slack's /api)
    exclude_paths: tuple[str, ...] = ()  # URLs under these paths belong to another entry
    kind: str = "openapi"  # or "graphql"

    def matches_url(self, host: str, path: str) -> bool:
        if host not in self.hosts:
            return False
        if self.path_prefix and not path.startswith(self.path_prefix):
            return False
        return not any(path.startswith(p) for p in self.exclude_paths)


def _raw(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


KNOWN_APIS: tuple[KnownApi, ...] = (
    KnownApi(
        "stripe", "Stripe", _raw("stripe/openapi", "master", "openapi/spec3.json"),
        hosts=("api.stripe.com",),
        packages=frozenset({"stripe", "stripe/stripe-php", "github.com/stripe/stripe-go", "stripe-java", "stripe.net"}),
    ),
    KnownApi(
        "github", "GitHub REST", _raw("github/rest-api-description", "main",
                                      "descriptions/api.github.com/api.github.com.json"),
        hosts=("api.github.com",), exclude_paths=("/graphql",),
        packages=frozenset({"@octokit/rest", "@octokit/core", "octokit", "pygithub", "github.com/google/go-github",
                            "@octokit/plugin-rest-endpoint-methods", "ghapi"}),
    ),
    KnownApi(
        "github-graphql", "GitHub GraphQL", _raw("octokit/graphql-schema", "main", "schema.graphql"),
        hosts=("api.github.com",), path_prefix="/graphql", kind="graphql",
        packages=frozenset({"@octokit/graphql"}),
    ),
    KnownApi(
        "twilio", "Twilio", _raw("twilio/twilio-oai", "main", "spec/json/twilio_api_v2010.json"),
        hosts=("api.twilio.com",),
        packages=frozenset({"twilio", "github.com/twilio/twilio-go", "twilio/sdk", "twilio-java"}),
    ),
    KnownApi(
        "openai", "OpenAI", _raw("openai/openai-openapi", "main", "openapi.yaml"),
        hosts=("api.openai.com",),
        packages=frozenset({"openai", "github.com/openai/openai-go", "openai-java", "openai-php/client"}),
    ),
    KnownApi(
        "slack", "Slack Web API", _raw("slackapi/slack-api-specs", "master", "web-api/slack_web_openapi_v2.json"),
        hosts=("slack.com",), path_prefix="/api",
        packages=frozenset({"slack-sdk", "slackclient", "@slack/web-api", "@slack/bolt", "slack-bolt",
                            "github.com/slack-go/slack"}),
    ),
    KnownApi(
        "box", "Box", _raw("box/box-openapi", "main", "openapi.json"),
        hosts=("api.box.com",),
        packages=frozenset({"boxsdk", "box-sdk-gen", "box-node-sdk", "box-typescript-sdk-gen", "box-java-sdk",
                            "box.v2"}),
    ),
)


def normalize_package(name: str) -> str:
    """Lowercase; Python-style ``-``/``_``/``.`` are equivalent; Go major-version suffixes dropped."""
    n = name.strip().lower()
    n = re.sub(r"/v\d+$", "", n)
    if "/" not in n and not n.startswith("@"):
        n = re.sub(r"[-_.]+", "-", n)
    return n


_PACKAGE_INDEX: dict[str, KnownApi] = {}
for _api in KNOWN_APIS:
    for _pkg in _api.packages:
        _PACKAGE_INDEX[normalize_package(_pkg)] = _api


def by_package(name: str) -> KnownApi | None:
    n = normalize_package(name)
    if n in _PACKAGE_INDEX:
        return _PACKAGE_INDEX[n]
    for prefix, api in _PACKAGE_INDEX.items():  # Go modules: github.com/stripe/stripe-go/v76/...
        if "/" in prefix and n.startswith(prefix + "/"):
            return api
    return None


def by_url(host: str, path: str) -> KnownApi | None:
    for api in KNOWN_APIS:
        if api.matches_url(host, path):
            return api
    return None
