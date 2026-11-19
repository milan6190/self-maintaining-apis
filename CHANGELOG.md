# Changelog

## 0.4.0 (2026-11-19)

**See removals coming**
- Spec deprecations: operations, parameters and schema properties that become `deprecated: true` are warnings, with the `x-sunset` date when there is one; removing an already-deprecated operation says so
- Live deprecation headers: `Deprecation` (RFC 9745, plus older `true`/date forms), `Sunset` (RFC 8594), `Link` (`deprecation`, `sunset`, `successor-version`) and `Warning: 299` are stored for each live endpoint and reported on every check while present. A sunset date shows the days left and becomes breaking once it has passed

**MCP**
- Prompts, resources and resource templates are listed when the server offers them, and compared: removed prompts and templates, new or newly required prompt arguments, removed resources and MIME type changes
- The example server gains a prompt and resource templates that change in v2

## 0.3.0 (2026-11-17)

**Agent tools (MCP)**
- `mcp:` watches an MCP server's tools: a stdio command (string or argument list, with `env:`) or a Streamable HTTP endpoint (JSON or SSE responses, sessions, `nextCursor` pagination). Saved `tools/list` results work as `spec:` and in `apimend diff`
- Tool changes graded from the agent's side: removed and renamed tools, argument changes by request rules, `outputSchema` changes by response rules, changed descriptions and riskier annotation hints (`readOnlyHint`, `destructiveHint`, `openWorldHint`, `idempotentHint`) as warnings, protocol and server version changes
- `apimend scan` finds MCP servers in `.mcp.json`, `.vscode/mcp.json`, `.cursor/mcp.json` and `claude_desktop_config.json`, keeping env var names but never their values
- `examples/agent-tools`: a stdio server whose update breaks an agent

**Rolling out across a company**
- `waivers:` accept known changes with a required reason, optional owner and expiry; waived changes don't fail the check but stay in every report; expired, expiring and unused waivers are called out
- `owner:` per API, carried into reports, SARIF and the rollup
- `apimend check --format sarif` (SARIF 2.1.0) for GitHub code scanning and other dashboards; the action's new `sarif` input and output
- `apimend rollup` combines `report.json` files from many repositories into one view by owner and API; an org rollup workflow example
- [docs/adoption.md](docs/adoption.md): an adoption playbook with phases, metrics, governance and notes by industry

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
