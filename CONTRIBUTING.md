# Contributing to apimend

Thanks for helping keep integrations alive.

## Setup

```bash
git clone https://github.com/milan6190/self-maintaining-apis.git
cd self-maintaining-apis
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
./examples/petstore/demo.sh
./examples/graphql-shop/demo.sh
```

## Layout

| Path | Purpose |
| --- | --- |
| `src/apimend/scan.py` | `apimend scan`: finding APIs in a codebase, noise filtering, writing the config |
| `src/apimend/registry.py` | Well-known APIs and their published spec URLs |
| `src/apimend/specs.py` | Loading any spec or schema; bundling multi-file OpenAPI |
| `src/apimend/gql/` | GraphQL: parser, schema model, schema diff, operation validation and fixes |
| `src/apimend/schema.py` | Paths, schema inference from samples, flattening specs |
| `src/apimend/openapi.py` | Reading OpenAPI 3 / Swagger 2 operations and shared schemas |
| `src/apimend/diff.py` | Change detection, severity rules, rename and coercion detection |
| `src/apimend/heal.py` | Repair plans and the runtime `Healer` |
| `src/apimend/verify.py` | Validating healed payloads against the baseline |
| `src/apimend/cli.py` | The `apimend` command |
| `action.yml` | The GitHub Action |

## Most useful contributions

- **Real-world drift cases.** If apimend misses or misgrades a change you've seen, open an issue with a minimal before/after spec or payload. These become tests.
- False-positive renames, with the two field names and sample values.
- **Scan false positives or misses:** the URL, the file and line, and what it should have been.
- **Registry entries:** a provider, its API hosts, its SDK package names, and a spec URL it publishes.
- Support for more spec features (discriminators, `additionalProperties`, callbacks).

## Pull requests

- Add a test for every behavior change; `pytest -q` must pass.
- Keep runtime dependencies at PyYAML only.
- Keep messages short and specific: a reader should know what changed and where from one line.
