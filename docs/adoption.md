# Adopting apimend across an organization

This is a playbook for a platform, integration or AI-platform team that wants every service and agent in the company to find out about upstream API changes before production does. It assumes no buy-in at the start, and gets there in steps that each pay for themselves.

The short version:

1. **Inventory** (week 1): scan every repository and learn what you depend on.
2. **Observe** (weeks 2–4): baseline the critical integrations and run checks that report but never block.
3. **Gate** (month 2): fail pull requests and nightly jobs on breaking drift, with owners and expiring waivers.
4. **Repair** (month 3 on): turn drift into reviewed fixes: repair plans, GraphQL patches, code proposals.
5. **Govern agents**: apply the same contract discipline to the MCP servers your AI agents call.

Each step is useful even if you stop there.

## Why consumers need their own contracts

Contract testing tools (Pact and similar) need the provider to take part. Most of the APIs a company depends on are owned by someone who won't: Stripe, carriers, banks, a partner's ERP, another team's microservice, an MCP server installed with `npx`. The consumer is the only party that knows which fields, queries and tools it actually uses, so the consumer has to hold the contract.

apimend keeps that contract in the consumer's repository: `apimend.yaml` says what is watched, `.apimend/baseline/` records what it looked like when the code was written, and every change to either goes through code review. Nothing runs as a hosted service, and no traffic or data leaves your CI except the calls to the APIs themselves.

## Step 1: Inventory

Goal: a list of every external API, GraphQL schema and MCP server the company's code depends on, with evidence.

```bash
gh repo list YOUR-ORG --limit 1000 --json name -q '.[].name' |
  xargs -P8 -I{} gh repo clone YOUR-ORG/{} repos/{} -- --depth 1
apimend scan repos/* --format json > api-inventory.json
```

Load the JSON into a spreadsheet and sort by how many repositories use each API. Then pick the **first 10 to 20 integrations** to protect, using what you already know:

- integrations that caused an incident in the last year;
- integrations on a revenue or compliance path (payments, shipping labels, KYC, payroll);
- providers that change often or have announced a deprecation;
- agents with tools that write data or reach customers.

Exit criterion: a named owner for each of the first integrations.

## Step 2: Observe

Goal: real drift reports from production-like data, with zero risk of blocking anyone.

In each chosen repository:

```bash
apimend scan              # writes apimend.yaml; review it
apimend snapshot          # records the baseline
git add apimend.yaml .apimend/baseline && git commit -m "Track upstream API contracts"
```

Set `fail_on: never` and `owner:` on each API, and add the [nightly workflow](../examples/workflows/apimend-nightly.yml). For REST APIs, add one or two read-only `endpoints:` so you compare what the provider returns, not only what it documents. Live samples also make repairs possible later.

During these weeks, tune the signal:

- For GraphQL, list your `operations:` and set `only_used: true`, so only schema changes your queries touch are reported.
- For a change you already know about, add a waiver (below) instead of ignoring the report.
- Watch the warnings, not only the breaking changes: deprecations in specs and `Deprecation`/`Sunset` headers on live calls are the provider telling you the date. A sunset inside 30 days is your most urgent item even though nothing has broken yet.
- Record each finding that was real. These are the numbers for the step 3 decision.

Exit criterion: the team agrees the reports are worth acting on.

## Step 3: Gate

Goal: breaking drift can't reach production unnoticed, and accepting a change is a recorded decision.

- Set `fail_on: breaking` (or `warning` for agents that act on production systems).
- Run the [GitHub Action](../README.md#github-action) on pull requests that touch integration code and nightly. The nightly run catches provider changes; the PR run catches your code depending on something that has already changed.
- Send results to the tools people already watch: `--format sarif` for GitHub code scanning (or any SARIF-reading dashboard), the action's outputs to open issues or post to chat.
- Put `apimend.yaml` and `.apimend/` under `CODEOWNERS`, so accepting a new baseline needs the integration owner's review.

### Waivers

A gate that can only be switched off gets switched off. Waivers let a team accept a known change for a limited time, with a reason:

```yaml
waivers:
  - api: billing
    kind: field-removed
    path: "$.data[].legacy_id"
    reason: "We stopped reading legacy_id (PR #412)"
    owner: billing-team
    expires: 2026-12-31
```

Matching keys are `api`, `kind`, `where` (the location), `path` and `severity`; each takes shell-style globs. A `reason` is required. Waived changes don't count toward `fail_on`, but they still appear in the report (collapsed), in JSON with a `waived` field, and in SARIF as suppressed results with their justification, so the audit trail is complete. The report warns two weeks before a waiver expires, says when one has expired (its changes count again), and points out waivers that no longer match anything.

A reasonable policy: every waiver has an owner and expires within 90 days.

### The org-wide view

Each repository's nightly job uploads `.apimend/report.json`. The platform team combines them:

```bash
apimend rollup reports/                  # Markdown: by owner, then by API
apimend rollup reports/ --format json    # for a dashboard
```

[examples/workflows/apimend-org-rollup.yml](../examples/workflows/apimend-org-rollup.yml) collects the latest report from every repository and publishes the rollup weekly.

Exit criterion: breaking upstream changes are found by CI, not by incidents.

## Step 4: Repair

Goal: shorter time from "the provider changed" to "we're fixed".

- **Renamed response fields and type changes:** `apimend heal` writes a repair plan from live samples; `apimend verify` replays responses through it; `Healer.from_file(...)` applies it at runtime while the real code fix ships. Plans are reviewed like code. A human fills in what can't be inferred (a default for a removed field).
- **GraphQL:** `apimend heal` writes alias-based patches for renamed fields, so your code reads the same keys; `--apply` edits the files and `verify` re-validates every operation.
- **Code changes:** `apimend propose` asks Claude for a patch to the consumer code, given the drift and the plan. Treat it as a draft pull request.
- When the code is updated, `apimend accept` moves the baseline forward and removes the plan. Healing is a bridge, not a destination.

Start with propose-and-review everywhere. Automatic application to production is a decision to make per integration, after the plans have a track record.

## Step 5: Govern agents

An AI agent is an API consumer whose contract is read by a model. Apply the same steps to MCP servers ([details](mcp.md)):

- Scan finds the servers in `.mcp.json` and editor configs; baseline each one your agents use.
- Description and annotation changes are warnings: a tool whose description gains new instructions, or that stops being read-only, should be reviewed before agents use it, the same way a new OAuth scope would be.
- Gate server upgrades on the PR that bumps the version, and check self-updating servers (`npx -y`, remote URLs) nightly.

## Measuring it

Track these monthly, per owner and overall (most come straight from `apimend rollup --format json`):

| Metric | Where it comes from | Healthy direction |
| --- | --- | --- |
| Critical integrations covered | inventory vs. repos with a baseline | ↑ toward 100% |
| Incidents caused by upstream changes | your incident tracker | ↓ |
| Breaking drift found by CI before an incident | nightly reports, issues opened | ↑ at first, then steady |
| Time from detection to fix (or accepted baseline) | issue open → close | ↓ |
| Open waivers and their age | rollup `waived`, `apimend.yaml` history | low, none expired |
| Healthy APIs | rollup `healthy_apis / apis` | ↑ |

The business case is the first two rows: what an upstream breakage cost last time (engineer hours, failed shipments or payments, SLA credits), times how often it happens.

## By industry

| Setting | Start with | Watch especially |
| --- | --- | --- |
| Logistics and supply chain | Carrier, 3PL, customs and marketplace APIs; live endpoints on rate and tracking calls | Enum widening (new shipment statuses), renamed fields in tracking payloads |
| Fintech and payments | Processor and banking APIs, webhook payload samples | Request-side breaking changes (new required fields), removed enum values; keep the committed baselines and SARIF suppressions as audit evidence |
| SaaS with many connectors (unified APIs, iPaaS, integrators) | One repo or config per connector; rollup by connector owner | Volume: `only_used`, waivers with expiry, the rollup as the weekly triage list |
| Internal platforms and microservices | Other teams' OpenAPI specs or GraphQL schemas, checked in consumers' CI | Consumers find out before the provider deploys; share the report with the provider team |
| Teams shipping AI agents | Every MCP server and tool list; `fail_on: warning` for agents that write | Description and permission-hint changes |
| Regulated or air-gapped environments | Specs and recorded samples (`samples:`), no live calls | Runs entirely in your CI; no hosted service |

## For API providers

If you publish an API, you make this easier for every consumer by:

- publishing a machine-readable description (OpenAPI, a GraphQL schema, or a `tools/list` for MCP servers) at a stable URL or in a public repository, versioned with your releases;
- marking deprecations in the description (`deprecated: true`, `@deprecated(reason:)`) before removing things;
- adding your API to the scan registry ([src/apimend/registry.py](../src/apimend/registry.py)) so consumers are covered with no setup.

## Security notes

- Live checks call only the `GET` endpoints and GraphQL queries you configure. Use read-only tokens.
- `mcp:` commands run in CI with the same trust as the repository's own code; review them like code. apimend never calls a tool, only `initialize` and `tools/list`.
- Credentials come from the environment (`${VAR}`); scan never copies secret values into `apimend.yaml`.
- Recorded samples may contain personal data. Use synthetic accounts or scrub samples before committing them.

## What's not there yet

apimend is alpha (0.4). It doesn't rewrite outgoing requests, read provider changelogs (only deprecations in specs and response headers), or run as a proxy. See the [roadmap](../README.md#roadmap). Real-world drift that it misses or misgrades is the most useful thing to report.
