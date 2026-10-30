# GraphQL

apimend watches a GraphQL API at two levels:

1. **the schema:** every change between the baseline and the current schema, graded like the reference implementation grades it;
2. **your operations:** the queries, mutations and fragments your code sends, validated against both schemas, so you see exactly which of them a change breaks.

## Where the schema comes from

| Config | Source |
| --- | --- |
| `graphql: https://api.example.com/graphql` | Introspection of the live endpoint (with `headers:` for auth). Older servers that reject `includeDeprecated` on arguments are retried with a simpler query. |
| `spec: schema.graphql` | SDL (`.graphql`, `.gql`, `.graphqls`, `.sdl`, or any file that starts like SDL), including `extend`, directives, descriptions and block strings |
| `spec: schema.json` | An introspection result, with or without the `{"data": ...}` wrapper |

All three produce the same model, so a baseline taken from a file can be compared with a live endpoint. Schema parsing is checked against the reference implementation (graphql-core) on GitHub's public schema: the models are identical, apart from whitespace in printed default values, which comparisons ignore.

## Operations

```yaml
operations:
  - src/**/*.graphql
  - src/**/*.ts
  - src/**/*.tsx
  - "!**/*.test.*"            # exclusions start with !
```

Operations are read from `.graphql`/`.gql` files and from code:

- JS/TS/Vue/Svelte: `` gql`...` ``, `` graphql`...` ``, `` gql(`...`) ``, `` graphql(`...`) `` and `` /* GraphQL */ `...` ``
- Python: `gql("""...""")` and `gql("...")`

Templates that define a schema (`` gql`type Query {...}` `` on a server) are skipped. Interpolations are handled: `${FRAGMENT}` at the top level is ignored, and `...${NAME}` or `on ${Type}` become placeholders that are not checked, so the rest of the operation still is.

### What validation checks

Unknown fields, arguments, types, fragments, enum values and input fields; missing required arguments and input fields; fields that need or forbid a selection; `null` for non-null; undefined variables; and variables whose type doesn't fit where they are used (`$id: String` passed to `ID!`). These are the checks that catch schema drift. Purely stylistic rules (unused variables or fragments, overlapping field merges) are not applied.

## What `check` reports

| Kind | Severity | Meaning |
| --- | --- | --- |
| `operation-broken` | 🔴 breaking | Valid against the baseline, invalid now |
| `operation-invalid` | 🟡 warning | Invalid against both schemas (a bug in the query, or the wrong schema) |
| `operation-uses-deprecated` | 🟡 warning when new, ⚪ info if it already was | Uses a deprecated field, argument, input field or enum value |
| schema changes | as in the table below | Marked `used by <operation>` when they touch your operations |

| Schema change | Severity |
| --- | --- |
| Type, field, argument, input field, enum value or union member removed; interface no longer implemented; type kind changed | 🔴 breaking |
| Field removed and a similar one added with the same type | 🔴 breaking, reported as a rename (fixable) |
| Field type changed so clients can no longer read it (`String!` → `String`, `Int` → `String`) | 🔴 breaking |
| Argument or input type changed so old values are rejected (`Int` → `Int!`) | 🔴 breaking |
| Required argument or input field added | 🔴 breaking |
| Enum value or union member added; interface added; default value changed; newly deprecated | 🟡 warning |
| Type, field, optional argument or input field added; safe type changes | ⚪ info |

The root types are compared by role, so renaming `Query` to `RootQuery` is not reported as a removal.

`only_used: true` hides schema changes that touch none of your operations, and adds one line saying how many were hidden.

## Repairing operations

`apimend heal` turns breaks caused by renames into edits:

| Break | Edit | Why it is safe |
| --- | --- | --- |
| Field `email` renamed to `emailAddress` | `email` → `email: emailAddress` | The alias keeps the response key, so code reading `email` is unchanged |
| Already aliased: `mail: email` | `mail: emailAddress` | Same |
| Argument `pageSize` renamed to `size` | `pageSize: $n` → `size: $n` | Arguments don't shape the response |

The edits go to `.apimend/graphql-fixes/<api>.json` with a unified diff next to it (`.patch`). Review it, then:

```bash
apimend heal --apply   # edit the files (or `git apply .apimend/graphql-fixes/<api>.patch`)
apimend verify         # every operation re-validated against the current schema
```

The plan records a hash of each file before and after the fix, so `verify` knows whether a fix is pending, already applied, or stale (the file changed since `heal`). Breaks that can't be fixed mechanically, such as a removed field, are listed as `unresolved`.

## Live GraphQL queries

To also compare real responses, add endpoints with a `query`:

```yaml
endpoints:
  - name: featured
    query: "{ featured { id title price } }"
    variables: {first: 5}
```

They are sent as `POST` to the API's `graphql:` URL with its headers. If a query that used to work starts returning `errors`, that is reported as breaking.
