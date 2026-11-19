# Agent tools (MCP)

An AI agent depends on its tools the way an app depends on an API, with one difference: the contract is read by a model at run time. When an MCP server you use ships an update, nothing fails to compile. The agent calls a tool that no longer exists, leaves out an argument that is now required, or follows a description that now says something new. Usually nobody notices until a business process quietly goes wrong.

apimend treats an MCP server's tool list as a contract: it records it, checks it, and reports what changed from the agent's side.

```yaml
apis:
  - name: shipping-tools
    mcp: npx -y @acme/shipping-mcp          # a command that starts the server over stdio
    owner: agent-platform
    env:                                     # extra environment for the command (optional)
      SHIPPING_API_KEY: ${SHIPPING_API_KEY}

  - name: docs-search
    mcp: https://mcp.example.com/docs        # or a Streamable HTTP endpoint
    headers:
      Authorization: "Bearer ${DOCS_TOKEN}"

  - name: vendor-tools
    spec: vendor/tools.json                  # or a saved tools/list result
```

`mcp:` can be a URL, a command string, or a list of arguments (`[node, dist/server.js]`). apimend sends `initialize`, then `tools/list` (following `nextCursor` pages), and stops the server. It never calls a tool. A saved file can be a `tools/list` result, the JSON-RPC response that wraps one, or a bare list of tools, so a vendor can publish their tool list the way API providers publish OpenAPI.

`apimend scan` finds servers in `.mcp.json`, `.vscode/mcp.json`, `.cursor/mcp.json` and `claude_desktop_config.json`. It writes them as commented-out entries, because checking one starts a process or calls a URL; `--enable-live` turns them on. It copies the command and the names of the environment variables the server reads, never their values.

## What is graded how

Tool arguments are what the agent **sends**, so they follow request rules. Structured output (`outputSchema`) is what it **reads**, so it follows response rules. Everything else is specific to agents.

| Change | Severity | Why |
| --- | --- | --- |
| Tool removed | 🔴 breaking | Calls fail; prompts and allowlists name a tool that's gone |
| Tool renamed (same arguments or description, similar name) | 🔴 breaking | Same as removed, but you're told what it became |
| New required argument | 🔴 breaking | The agent doesn't know to send it |
| Argument type changed incompatibly / enum value removed | 🔴 breaking | Calls the agent already makes start failing |
| Output field removed or renamed | 🔴 breaking | Code or prompts that read it get nothing |
| Description changed | 🟡 warning | The model's instructions changed. This is also how a compromised server adds new instructions after it was approved |
| `readOnlyHint` true → false, `destructiveHint` → true, `openWorldHint` → true, `idempotentHint` → false | 🟡 warning | The tool now declares more risk; hosts that auto-approve it should re-check |
| Output schema removed | 🟡 warning | Structured results may stop |
| MCP protocol version changed | 🟡 warning | Clients may need an update |
| New tool, new optional argument, output schema added, server version changed, risk hints narrowed | ⚪ info | |

Annotation hints use MCP's defaults when absent (`readOnlyHint: false`, `destructiveHint: true`, `openWorldHint: true`, `idempotentHint: false`), so a server that starts stating `destructiveHint: false` is reported as narrowing, not as noise.

## Try it

```bash
./examples/agent-tools/demo.sh
```

The example is a small stdio server for shipping tools. Its v2 renames `track_package`, adds a required `service_level` to `quote_rate`, renames an output field, and changes `lookup_address` so its description asks the model to send the customer's phone and email, while it stops being read-only:

```
| 🔴 | tool quote_rate input     | new required request field `$.service_level` |
| 🔴 | tool track_package        | tool `track_package` appears to be renamed to `track_shipment` (confidence 1.00) |
| 🔴 | tool track_package output | `$.eta` is no longer returned |
| 🟡 | tool lookup_address       | the description the model reads changed; review it before agents use it: … |
| 🟡 | tool lookup_address       | `readOnlyHint` changed true → false: the tool now declares it is able to modify its environment |
```

## Recommended practice

- **Pin and baseline every server your agents use**, including internal ones. Commit `.apimend/baseline/<name>/spec.json`: it is the reviewed record of what the agent was approved to call.
- **Treat description and annotation warnings as security review items**, not style changes. Set `fail_on: warning` for agents that act on production systems or customer data.
- **Run the check before the agent upgrades**, not after: check on the PR that bumps the server version, and nightly for servers that update themselves (`npx -y`, remote URLs).
- `apimend check --format sarif` puts tool drift in the same code-scanning view as other security findings.

## Limits

- Only tools are compared today; prompts and resources are not.
- Renames are detected from identical argument schemas or descriptions plus name similarity, and always need review.
- Changes to a tool's behavior that its schema and description don't show can't be detected from the tool list. Add a recorded response under `endpoints:` if you can call the tool through an HTTP API.
