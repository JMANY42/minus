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

That relative command only works when the client starts the server from this
checkout. `minus mcp-config` prints a paste-ready snippet instead:

```bash
minus mcp-config
```

The snippet carries the absolute path to this checkout's interpreter,
`MINUS_PROJECT_ROOT` pointing at the repository, and the same `-m minus.mcp`
arguments. It mounts this server from any client's working directory, which is
what a GUI client with its own idea of cwd needs.

The built-in harness is the default surface; MCP is the portable one. Both drive
the same registry, so a capability added under `src/minus/tools/` reaches the
harness by import and an MCP client by the next `tools/list`.

## What is exposed

| group | tools |
|---|---|
| built-in | `get_current_time`, `list_workspace_files`, `read_workspace_file` |
| alarm | `set_alarm`, `list_alarms`, `cancel_alarm`, and the rest of the alarm group |
| triage | `torrent_search`, `list_torrents`, `qbittorrent_find`, `torrent_add`, `torrent_ingest`, `torrent_progress`, `triage_download`, `triage_scan`, `triage_verdict`, `triage_review`, `triage_preview`, `triage_vm_review`, `triage_promote`, `triage_trace` |
| vpn | `vpn_status`, `vpn_connect`, `vpn_disconnect` |
| google | tasks and calendar tools, only when all three `MINUS_GOOGLE_*` values are set |

Groups appear only when they are configured. Triage needs
`MINUS_QBITTORRENT_URL`; VPN needs `MINUS_AIRVPN_USERNAME`; Google needs the
grant. A missing group is absent rather than present-and-always-failing.

`triage_promote` takes `accept_review`, `reason` and `keep` in addition to the
hash and optional library name: `accept_review` is the explicit manual release
for a `needs_review` item, `reason` is recorded as provenance, and `keep` is a
comma-separated list of staged paths or basenames. Without `keep` it reviews every
video, else every audio file, else every file; only `.mp4` is copied into the
flat `library/`, and a selection with no `.mp4` is refused. Non-MP4 files are
the graveyard; provenance is written under `memory/library_provenance/`, not
inside the library. `clean` remains the ordinary gate, and
`suspicious`/`malicious` are never releasable this way.

A manual release now also requires VM review evidence: `triage_promote` refuses
unless `verdict.json` or `vm_review.json` records that the Windows guest opened
or launched a file (`detonated` true or `opened > 0`). A dry run or a host-only
ffprobe sample cannot unlock it. `triage_vm_review(torrent_hash, keep="...")` is
the standalone review pass: it opens every staged file in the guest — video in
VLC (or Windows Media Player), images in Paint, text in Notepad, anything else
through the shell association — and writes `quarantine/<hash>/vm_review.json`
without changing the verdict. Only an explicit handler that stays alive counts as
opened; the shell-association dialog is not a parser and is never counted. A
missing CD, an exhausted budget, or a kept file the guest never opened sets
`completed: false`, which also blocks release. It returns the per-file evidence
counts and lists (processes, network endpoints, file and registry writes,
Defender detections), and files not named in `keep` are moved to the guest's
graveyard. It is the MCP
equivalent of `minus triage vm`.

`torrent_ingest` also takes `release` (default false). With `release=true`, a
`clean` verdict is moved into the library and the download copy is removed by
the usual cleanup; anything else — `needs_review`, `suspicious`, `malicious` —
stays exactly where the gate left it. A client that wants the library copy
releases explicitly with `triage_promote` after `triage_review`, including the
`accept_review` case, so an ingest flag never stands in for that decision.

`torrent_ingest` accepts `expected_title`, the discovery query or the title a
client asked for, and carries it into the scan's media review. It is advisory:
it only feeds the title-coverage check behind `media:title-mismatch`, so a
mismatch is a reason to look, never a reason to promote — or to reject on its
own. `triage_preview` re-samples that review on demand and returns the frame
and contact-sheet paths under `quarantine/<hash>/preview/`, so a client that can
display images opens them instead of guessing from filenames. Frames show what a
file is, not that it is safe; the verdict gate is unchanged.

The recommendation is MCP-first parity for generic agent clients: every triage
stage — search, ingest, verdict, review, preview, acceptance, trace — is
reachable over this surface, so a client with no MINUS UI can drive the same
pipeline. The built-in harness is the default surface; MCP is the portable one,
and both reach the same stages rather than one being a reduced mirror.

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
  review, or the explicit acceptance of a passive `needs_review` item through
  `triage_promote(accept_review=true, reason=...)` **after** VM review evidence
  exists in `verdict.json` or `vm_review.json`;
- credentials are read from `.env` or the environment and are never returned by
  a tool.
