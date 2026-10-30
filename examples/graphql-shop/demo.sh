#!/usr/bin/env bash
# GraphQL walkthrough: baseline -> schema change -> what breaks for *your* queries -> heal -> verify.
# Runs in a temporary copy so the example files stay unchanged.
set -uo pipefail
work="$(mktemp -d)"
cp -r "$(dirname "$0")/." "$work"
cd "$work" && rm -rf .apimend

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "1. Record a baseline at schema v1"
apimend snapshot

step "2. The provider ships v2. Check what changed and which of your queries it breaks"
SHOP_SCHEMA=v2 apimend check --quiet
echo "(exit code $? = breaking drift found)"

step "3. Heal: renames are repaired with aliases, so your code reads the same keys"
apimend heal
cat .apimend/graphql-fixes/shop.patch

step "4. Apply the fixes, then verify"
apimend heal --apply > /dev/null
apimend verify
echo "(the removed field needs a human decision, so verify still fails)"

step "5. Remove the dropped field from the query, then verify again"
sed -i.bak '/legacySku/d' src/products.ts && rm -f src/products.ts.bak
apimend verify

step "6. Final queries"
cat src/queries/user.graphql src/products.ts | sed -n '1,12p;17,26p'
