#!/usr/bin/env bash
# Agent-tool drift: baseline an MCP server, then catch what its update breaks.
set -uo pipefail
cd "$(dirname "$0")"
rm -rf .apimend

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "1. Record the tools the agent was built against (server v1)"
apimend snapshot

step "2. The vendor ships v2. What changed for the agent?"
TOOLS_VERSION=v2 apimend check
echo "(exit code $? = breaking drift found)"

step "3. The same check as SARIF, for code-scanning dashboards"
TOOLS_VERSION=v2 apimend check --offline --format sarif -o .apimend/report.sarif >/dev/null
python3 -c "import json; r=json.load(open('.apimend/report.sarif'))['runs'][0]['results']; print(len(r), 'SARIF results')"
