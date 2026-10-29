#!/usr/bin/env bash
# Walk through the full loop: baseline -> drift -> heal -> verify -> use the plan.
set -uo pipefail
cd "$(dirname "$0")"
rm -rf .apimend

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "1. Record a baseline while the API is at v1"
apimend snapshot

step "2. Time passes; the provider ships v2. Check for drift"
PETSTORE_VERSION=v2 apimend check --quiet
echo "(exit code $? = breaking drift found)"

step "3. Generate a repair plan from the live responses"
apimend heal

step "4. Verify: the removed 'tag' field can't be auto-healed yet"
apimend verify

step "5. A human decides: default missing tags to \"unknown\", then re-heal and verify"
python3 - <<'EOF'
import json
p = ".apimend/plans/petstore/list-pets.json"
plan = json.load(open(p))
plan["ops"].append({"op": "default", "path": "$[].tag", "value": "unknown"})
json.dump(plan, open(p, "w"), indent=2)
EOF
apimend heal >/dev/null
apimend verify

step "6. The v1 consumer runs unchanged against v2 data"
python3 consumer.py
