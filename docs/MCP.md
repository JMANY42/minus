# MINUS as an MCP server

MINUS already owns a tool registry: schemas are derived from function
signatures and docstrings, arguments are validated before a body runs, and
`dispatch` is the one path every tool goes through. The MCP server puts that
same registry on the wire, so a client that knows nothing about MINUS's Python
API can call `read_workspace_file`, `torrent_ingest`, `vpn_connect` and the rest.

There is no second tool list to maintain. Add a tool under `src/minus/tools/`
and it appears on the next server start.

## Start it

```bash
minus mcp                 # behind the existing CLI
minus-mcp                 # console script, for client configs
.venv/bin/python -m minus.mcp
```

The transport is newline-delimited JSON-RPC 2.0 over stdin/stdout. Logs go to
stderr and the run log; nothing else writes to stdout. The server has no
third-party dependencies, so it runs from a bare core install.

## Client configuration

This repository ships `.mcp.json`:

```json
{
  "mcpServers": {
    "minus": {
      "type": "stdio",
      "command": ".venv/bin/python",
      "args": ["-m", "minus.mcp"]
    }
  }
}
```

Any MCP client accepts the same shape. For a checkout at a different path,
replace the command with that checkout's `minus-mcp` or `python -m minus.mcp`.

## What is exposed

| group | tools |
|---|---|
| built-in | `get_current_time`, `list_workspace_files`, `read_workspace_file` |
| alarm | `set_alarm`, `list_alarms`, `cancel_alarm`, and the rest of the alarm group |
| triage | `list_torrents`, `qbittorrent_find`, `torrent_add`, `torrent_ingest`, `torrent_progress`, `triage_download`, `triage_scan`, `triage_verdict`, `triage_promote` |
| vpn | `vpn_status`, `vpn_connect`, `vpn_disconnect` |
| google | tasks and calendar tools, only when all three `MINUS_GOOGLE_*` values are set |

Groups appear only when they are configured. Triage needs
`MINUS_QBITTORRENT_URL`; VPN needs `MINUS_AIRVPN_USERNAME`; Google needs the
grant. A missing group is absent rather than present-and-always-failing.

`escalate` is deliberately absent: it needs a live conversation to snapshot and
a queue to answer on, which a stateless MCP call cannot provide. The coding
tools are absent too: they are an approval-gated workspace shell, and an MCP
client has no MINUS approval UI behind it.

## Protocol surface

- `initialize` — negotiates `2024-11-05`, `2025-03-26` or `2025-06-18`.
- `tools/list` — the enabled registry tools, with `inputSchema` derived from the
  same signature/docstring pair the chat model sees.
- `tools/call` — validates and dispatches; a tool failure is a result with
  `isError: true` and the message, so the client can correct it.
- `ping`, and empty `resources/list`, `resources/templates/list`, `prompts/list`
  for clients that probe them.
- Unknown methods return `-32601`; malformed lines return `-32700` and the
  server keeps reading.

## Safety boundary

The MCP server is a local stdio process with the user's privileges. It is not a
sandbox. What still holds:

- filesystem tools are confined to `MINUS_PROJECT_ROOT` by
  `tools/workspace.py::resolve_workspace_path` (resolve before containment);
- `torrent_ingest` and every triage stage refuse to fetch unless the AirVPN
  preflight passes, and nothing is executed on the host;
- a malicious verdict deletes the staged payload and returns an alert; promotion
  requires a `clean` verdict, which requires a detonation and a clear model
  review;
- credentials are read from `.env` or the environment and are never returned by
  a tool.
