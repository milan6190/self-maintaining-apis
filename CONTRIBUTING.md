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
```

## Layout

| Path | Purpose |
| --- | --- |
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
- Support for more spec features (discriminators, `additionalProperties`, callbacks).

## Pull requests

- Add a test for every behavior change; `pytest -q` must pass.
- Keep runtime dependencies at PyYAML only.
- Keep messages short and specific: a reader should know what changed and where from one line.
