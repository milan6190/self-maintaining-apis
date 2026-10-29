# Changelog

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
