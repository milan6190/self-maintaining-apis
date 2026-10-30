# Changelog

## 0.2.0 (2026-10-30)

**Scan a codebase**
- `apimend scan` finds the APIs a repository depends on and writes `apimend.yaml`: specs and GraphQL schemas in the repo, GraphQL operations and tooling configs, HTTP calls in code (Python, JS/TS, Go, Java/Kotlin, Ruby, PHP, C# and more), base URLs in config files, Postman collections, and dependency manifests
- A registry of well-known APIs whose providers publish their descriptions (Stripe, GitHub REST and GraphQL, Twilio, OpenAI, Slack, Box), so those are ready to watch without setup
- Noise filtering for tests, fixtures, CI, docs, websites, placeholders, local and dev hosts
- GraphQL operations are matched to the local schema they validate against
- `--format json` inventory for org-wide scans; `--enable-live`, `--print`, `--snapshot`

**GraphQL**
- Schemas from SDL, introspection JSON, or a live endpoint (`graphql:`), parsed without dependencies
- Schema diffs with the reference implementation's breaking/dangerous rules, plus renames and deprecations
- `operations:` validates the queries your code sends (files and `gql` templates) and marks which schema changes touch them; `only_used` hides the rest
- `apimend heal` fixes renamed fields with aliases and renamed arguments in place; `--apply`; `verify` re-validates
- Live GraphQL endpoints (`query:`), with responses that start returning `errors` reported as breaking

**Also**
- Multi-file OpenAPI specs: `$ref`s to other files and URLs are bundled
- `oneOf`/`anyOf` variants added or removed are graded by direction instead of always breaking
- Word-aware rename matching (fewer false renames on long names)
- Specs over 1 MB are stored gzipped (Stripe: 8 MB to about 0.5 MB); the C YAML parser is used when available (about 5x faster on big specs)
- `diff` compares GraphQL schemas too (`--operations` to check queries)

## 0.1.0 (2026-10-28)

First release.

- `apimend` CLI: `init`, `snapshot`, `check`, `heal`, `verify`, `accept`, `diff`, `infer`, `propose`
- Drift detection for OpenAPI 3.x and Swagger 2.0 specs and for live or recorded JSON responses
- Direction-aware severity (requests vs responses); shared schemas diffed once and graded by usage
- Rename detection by name similarity and by matching values, including nested renames
- Repair plans (`rename`, `coerce`, `default`, `drop`) and a runtime `Healer`
- Verification by replaying recorded responses through plans
- GitHub Action with job summary and outputs
- Optional code-fix proposals via the Claude API
