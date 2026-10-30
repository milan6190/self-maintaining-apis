# How `apimend scan` works

`apimend scan` reads a repository and writes an `apimend.yaml` that watches every API the code depends on. It never calls those APIs and never runs your code.

```bash
apimend scan                       # scan . and write apimend.yaml
apimend scan services/ web/        # several folders, one config
apimend scan --print               # show the config instead of writing it
apimend scan --format json         # an inventory, for dashboards and org-wide scans
apimend scan --enable-live         # also turn on live endpoints and GraphQL introspection
apimend scan --snapshot            # write the config, then record the baseline
```

## What it looks at

| Source | Examples | Gives |
| --- | --- | --- |
| API descriptions in the repo | `openapi.yaml`, `swagger.json`, multi-file specs, `schema.graphql`, introspection `schema.json` | A spec to watch (ready) |
| GraphQL operations | `.graphql`/`.gql` files; `gql\`...\``, `graphql(\`...\`)`, `/* GraphQL */` templates; Python `gql("""...""")` | The queries to check against the schema |
| GraphQL tooling configs | `codegen.yml`, `.graphqlrc.yml`, `graphql.config.json` | Which schema goes with which operations |
| HTTP calls in code | `requests.get(...)`, `httpx`, `fetch(...)`, `axios.post(...)`, `http.Get(...)`, `http.NewRequest("POST", ...)`, `RestTemplate`, `WebClient`, `Net::HTTP`, `curl`... in Python, JS/TS, Go, Java/Kotlin, Ruby, PHP, C#, Rust, Swift and more | Hosts, methods and URLs |
| Config files | `.env*`, `*.properties`, `application.yml`, `appsettings.json`, docker-compose, other YAML/JSON/TOML/INI | Base URLs from keys like `BILLING_API_URL` |
| Postman collections | `*.postman_collection.json` (with `{{baseUrl}}` variables resolved) | Requests with methods |
| Dependency manifests | `package.json`, `requirements*.txt`, `pyproject.toml`, `Pipfile`, `setup.py`, `go.mod`, `Gemfile`, `pom.xml`, `build.gradle`, `*.csproj`, `composer.json`, `Cargo.toml` | Known API SDKs (`stripe`, `@octokit/rest`, `twilio`...) |

In a git repository it reads the files git knows about (respecting `.gitignore`); otherwise it walks the folder, skipping `node_modules`, virtualenvs, build output and vendored code.

## Known APIs

For well-known providers, the official machine-readable description is published in a public repository. Scan uses it, so these APIs are ready with no setup:

| API | Matched by host | Matched by dependency | Description |
| --- | --- | --- | --- |
| Stripe | `api.stripe.com` | `stripe`, `stripe-go`, `stripe-java`, `Stripe.net`, `stripe/stripe-php` | [stripe/openapi](https://github.com/stripe/openapi) |
| GitHub REST | `api.github.com` | `@octokit/rest`, `@octokit/core`, `octokit`, `PyGithub`, `go-github`, `ghapi` | [github/rest-api-description](https://github.com/github/rest-api-description) |
| GitHub GraphQL | `api.github.com/graphql` | `@octokit/graphql` | [octokit/graphql-schema](https://github.com/octokit/graphql-schema) |
| Twilio | `api.twilio.com` | `twilio`, `twilio-go`, `twilio/sdk` | [twilio/twilio-oai](https://github.com/twilio/twilio-oai) |
| OpenAI | `api.openai.com` | `openai`, `openai-go`, `openai-java` | [openai/openai-openapi](https://github.com/openai/openai-openapi) |
| Slack Web API | `slack.com/api` | `slack-sdk`, `slackclient`, `@slack/web-api`, `@slack/bolt`, `slack-go` | [slackapi/slack-api-specs](https://github.com/slackapi/slack-api-specs) |
| Box | `api.box.com` | `boxsdk`, `box-sdk-gen`, `box-node-sdk`, `box-java-sdk` | [box/box-openapi](https://github.com/box/box-openapi) |

Other hosts of a known provider (`dashboard.stripe.com`, `hooks.slack.com`) are folded into its entry rather than listed separately. To add a provider, add an entry to [`src/apimend/registry.py`](../src/apimend/registry.py) with a spec URL the provider publishes.

## Keeping the noise out

Real repositories mention far more URLs than they call. Scan drops:

- **tests, fixtures and CI:** URLs that appear only in test files (`*.test.ts`, `test_*.py`, `*_test.go`, `spec/`, `__tests__/`, `e2e/`, `.github/`...) don't create an API. Spec and schema files under test folders are treated as fixtures, and operations in tests are left out of `operations:`;
- **documentation and websites:** `docs.*`, `/docs/...` paths, CDNs, dashboards, package registries, badges, licenses, social links;
- **placeholders:** `example.com` and any `example*` domain, `my-store.myshopify.com`-style placeholders, names that aren't real domains (`hydrogen.cache`);
- **local and dev hosts:** `localhost`, `127.0.0.1`, container names like `http://web:4242` (kept when the path is clearly an API, such as `http://orders-service/v1/orders`), and tunnels like ngrok (`--include-local` keeps localhost);
- **plain mentions:** a URL in a string is kept only if it is called, assigned to a URL-like setting, or clearly an API (`api.*` hosts, `/api/`, `/v1/`, `/graphql` paths).

On Stripe's sample app it finds exactly Stripe, and on LangChain's 3,275 files exactly the nine external services the code talks to.

## GraphQL operations and schemas

When a repo has several GraphQL schemas and no tooling config ties them to operations, scan validates each operation file against each local schema and assigns it to the one it validates against best. In Shopify's Hydrogen repo, this split 114 operations correctly between the Storefront and Customer Account schemas. If one file mixes both, it is listed for you to place by hand.

Interpolated text in templates (`...${FRAGMENT_NAME}`, `on ${typeName}`) is replaced by a same-length placeholder that validation skips, so the rest of the operation is still checked.

## The generated config

```yaml
apis:
  # Stripe: depends on stripe
  - name: stripe
    spec: "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json"

  # billing.internal.acme.com: 3 call(s) (GET 2, POST 1), e.g. app/billing.py:19
  # To watch it: uncomment and add auth headers if needed, or set `spec:` to its OpenAPI file
  # - name: billing-internal-acme
  #   headers:
  #     Authorization: "Bearer ${BILLING_INTERNAL_ACME_TOKEN}"
  #   endpoints:
  #     - name: invoices   # app/billing.py:19
  #       url: "https://billing.internal.acme.com/v1/invoices"
```

Only static `GET` URLs become live endpoints; templated URLs (`/invoices/{id}`) and other methods are listed as evidence only. Every entry's comment says where it was found, so it can be checked in seconds.

## The JSON inventory

`apimend scan --format json` prints, for each API: `name`, `title`, `kind` (`openapi`, `graphql`, `rest`), `ready`, `sources`, `spec`, `graphql`, the number of `calls` and their `methods`, `base_urls`, `operations` and `operation_count`, and up to 20 `evidence` locations.
