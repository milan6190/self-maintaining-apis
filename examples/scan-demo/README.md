# Scan demo: a small "company" repo

`acme-app/` is a tiny app with the kinds of API usage real repos have:

| Where | What |
| --- | --- |
| `requirements.txt`, `package.json` | SDKs for Stripe, Slack and GitHub GraphQL |
| `app/billing.py` | Calls to an internal billing service (GET, POST, and a templated URL) |
| `app/alerts.py` | The Slack SDK and a Slack webhook |
| `web/src/api.ts` | A REST base URL and a GraphQL endpoint |
| `web/src/graphql/*.graphql`, `web/codegen.yml` | GraphQL operations tied to that endpoint by a codegen config |
| `web/src/github.ts` | A GitHub GraphQL query in a `graphql(...)` template |
| `services/rates/main.go` | A Go `http.Get` to a third-party API |
| `config/.env.example` | Base URLs in environment settings |
| `specs/orders-api.yaml` | The OpenAPI description of the orders API |
| `postman/` | A partner's API as a Postman collection |
| `README.md`, docs links, `localhost` | Noise that should be ignored |

```bash
cd acme-app
apimend scan --print            # the config it would write
apimend scan --format json      # the inventory
apimend scan --snapshot         # write apimend.yaml and record a baseline (downloads the public specs)
```
