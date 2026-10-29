# Repair plans

`apimend heal` writes one plan per endpoint to `.apimend/plans/<api>/<endpoint>.json`. A plan turns a response in the provider's **new** shape back into the shape your code was written for (the baseline).

```json
{
  "version": 1,
  "api": "petstore",
  "endpoint": "list-pets",
  "created": "2026-10-29T03:37:08+00:00",
  "ops": [ ... ],
  "unresolved": [ ... ]
}
```

## Paths

Paths start at `$`, the response body:

| Path | Means |
| --- | --- |
| `$` | the whole body |
| `$.data.user_id` | a property |
| `$.items[]` | every element of an array |
| `$.items[].id` | a property of every element |

Ops run in order. Paths in later ops use the **old** (baseline) names, because earlier renames have already been applied by the time they run.

## Ops

| Op | Fields | Effect | Written by |
| --- | --- | --- | --- |
| `rename` | `parent`, `from`, `to`, `confidence` | In each object at `parent`, move key `from` to `to` | `heal` |
| `coerce` | `path`, `from`, `to` | Convert the value at `path` to `string`, `integer`, `number` or `boolean` (values that can't convert are left alone) | `heal` |
| `default` | `path`, `value` | Set the key at `path` to `value` when it's missing | you |
| `drop` | `path` | Remove the key at `path` | you |

Ops you write by hand (`default`, `drop`, or any op with `"manual": true`) are kept when `apimend heal` regenerates the plan. A `default` also clears the matching entry from `unresolved`.

## `unresolved`

`unresolved` lists breaking or warning changes the plan can't fix: a removed field, a type change whose values don't convert, a new enum value. Each one needs a decision:

- add a `default` op if a fallback value is safe, or
- update the consumer code, then `apimend accept`.

`apimend verify` fails while a removed field your code expects is still missing.

## Applying a plan

```python
from apimend import Healer

heal = Healer.from_file(".apimend/plans/petstore/list-pets.json")
pets = heal(response_json)   # returns a new object; the input is not modified
```

Plans are versioned. A plan from a newer apimend raises `ValueError` rather than half-applying.
