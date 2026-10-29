# How apimend works

## 1. Two sources of truth

| Source | Tells you | Used for |
| --- | --- | --- |
| **Spec** (OpenAPI 3.x / Swagger 2.0) | What the provider documents | Operations, parameters, request bodies, shared schemas |
| **Live responses** (URLs or recorded files) | What the provider actually returns | Response drift, rename detection by value, repair plans |

Specs drift from reality. Watching both catches undocumented changes, and live samples carry the values that make repairs safe.

## 2. Flattening

Every schema, whether declared in a spec or inferred from responses, is flattened into `path → field`, where a field has types, whether it's required, an optional enum, and an optional named-schema reference.

- Inference from samples: a property is required only if every sample has it, and `integer` + `number` merge to `number`.
- Specs: `$ref`, `allOf` (merged), `anyOf`/`oneOf` (types unioned; an enum applies only when every branch has one), and `nullable` are handled.
- `$ref`s are **boundaries**: a reference becomes a leaf that names its target, and each shared schema is diffed once. That keeps large, interlinked specs linear in size (Stripe's 8 MB spec diffs in under a second) and stops one change from being reported once per endpoint.

## 3. Diffing

Changes are found top-down. When a parent object disappears, its children aren't reported separately.

**Renames** are matched between a removed and an added field under the same parent when the types are compatible (ignoring `null`) and either:

- the names are similar after normalizing case and separators (`user_id` = `userId`, `email` ⊂ `emailAddress`), with similarity ≥ 0.6, or
- the recorded values overlap ≥ 80% across at least 3 distinct values (`label` → `title` with the same contents).

After each round, the new shape is rewritten in old names and diffed again, so renames nested under a renamed parent are found too.

**Severity** depends on direction. A consumer reads responses and writes requests, so the same change can be harmless one way and breaking the other (see the table in the README). A shared schema used in both directions keeps the stricter result.

**Sample-based caution**: a field looking optional because one sample omitted it is `info`, not `warning`, since sampling can't prove it.

## 4. Healing

From a live-sample diff, `heal` emits:

- a `rename` for every detected rename (outermost first)
- a `coerce` for scalar type changes where *every* observed new value converts back to the old type

Everything else that's breaking or a warning goes to `unresolved` for a human.

## 5. Verifying

`verify` applies the plan to each current sample and checks the result against the baseline shape: required fields present, types accepted (an integer satisfies `number`), and items in arrays complete. An empty array is valid.

## 6. Proposing code fixes (optional)

`apimend propose` sends the drift report, the plan and one source file to Claude, which suggests a unified diff that makes the consumer use the new API directly. Nothing is applied automatically.
