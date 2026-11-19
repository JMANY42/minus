# The web bridge

`src/minus/remote/` exposes download mode's search and grab to consumers that are not the
harness: another agent calling in-process, a process calling over loopback HTTP, and a browser
page loading one script. This file is the contract — endpoints, job shape, frontend protocol and
the trust boundary. The quick start and the command list live in `README.md` under "Web bridge:
search and grab from any frontend"; this document does not repeat them.

## Purpose and why it is not a plugin

`jellyfin-web` ships as a prebuilt static bundle. There is no extension API for adding a search
entry or a download action, and no build step a third party can hook; the only insertion point
that does not mean rewriting the bundle is a `<script>` tag in `index.html`. A page cannot be
given server-side code, and it should not be: the pipeline needs qBittorrent, the VPN preflight
and quarantine, which live behind `minus`, not inside a media server.

So the bridge is a local capability with three independent surfaces over it. The behaviour is
`DownloadService`; HTTP is a transport wrapped around one instance; a frontend is a description
of the page that may load the client. Jellyfin is the first frontend, not the architecture. A
different web app that can take a script tag is the `generic` frontend, and anything that cannot
even do that can still call the service in-process or the API directly.

The browser client is deliberately buildless: `src/minus/remote/web/minus.js` contains its own
CSS and DOM code, and the transport serves it with a small `window.__MINUS_REMOTE__` prefix. That
prefix is the only place the page learns the bridge's base URL, its per-process token and which
frontend it is; the asset itself never has to be rebuilt for a different host or port.

## Module layout

| file | role |
|---|---|
| `src/minus/remote/service.py` | `DownloadService` and `DownloadError`: search, grab, queue, workers, job dictionaries, qBittorrent sampler |
| `src/minus/remote/http.py` | `HttpBridge`: routing, token check, CORS, body cap, script prefix |
| `src/minus/remote/frontends/__init__.py` | `Frontend` protocol, `FrontendError`, `FRONTENDS`, `get_frontend` |
| `src/minus/remote/frontends/jellyfin.py` | `index.html` installer and tag for `jellyfin-web` |
| `src/minus/remote/frontends/generic.py` | tag-only frontend |
| `src/minus/remote/web/minus.js` | the one browser client, shared by every frontend |
| `src/minus/remote/__init__.py` | re-exports so callers need not know the package layout |

Imports point in one direction only:

- `http.py` imports `service.py` and `frontends/`. It owns the socket, the token and the
  frontend, and adds no search or grab behaviour.
- `service.py` imports neither `http.py` nor `frontends/`. It raises `DownloadError` carrying
  the status a transport should report, so a local caller and the HTTP layer mean the same thing
  by a bad request.
- `frontends/` imports neither of the other two. A frontend knows about a page and an asset, not
  about a service or a socket.

The transport stores the resolved frontend but uses only its `name` and `asset_path()`; it never
branches on which app is active, and `service.py` never learns that a frontend exists at all.

The pipeline and `DiscoveryTools` are imported lazily inside `service.py`
(`_assembly_pipeline`, `_discovery_tools`), so importing the bridge does not construct a harness;
`minus bridge …` is likewise imported lazily by `src/minus/cli.py`.

`minus.remote` is one consumer of the triage pipeline, never a second implementation of it.
`DownloadService.grab` re-resolves the candidate and then calls the same
`ensure_running` → `ingest(detonate=True, release=False)` → conditional `promote` sequence as the
harness; see `docs/ARCHITECTURE.md`.

`release=False` is not an accident of the harness call: `release=True` makes the pipeline promote
any `clean` verdict on its own, which would bypass this service's low-confidence gate. The two
steps exist so the decision about the library is made once, here.

## HTTP contract

`HttpBridge` binds `127.0.0.1:8099` by default; the CLI exposes `--host`, `--port` and
`--token`, and these are not `Settings` fields because a torrent bridge on a public interface is
a remote execution surface. The listening socket is `ThreadingHTTPServer` with HTTP/1.1 and
daemon request threads. If the requested port is `0`, the kernel's choice is reported in
`HttpBridge.port` and `base_url`. A wildcard bind is reported as `127.0.0.1` in `base_url`.

Routing strips trailing slashes, so `/health/` and `/api/job/<id>/` behave as their unadorned
forms. Methods are not negotiated: only `GET`, `POST` and `OPTIONS` are implemented, and a path
that does not accept the method falls through to `404` rather than `405`. Any other method gets
the standard library's `501`.

Responses are JSON with `Content-Type: application/json`; errors are always
`{"error": "<message>"}` with a non-empty string. An unexpected exception is logged and answered
`500` with the exception text. `/minus.js` is `application/javascript` with
`Cache-Control: no-store`.

### Authorisation

Every route except `/health` and `/minus.js` requires the header

```
X-Minus-Token: <token>
```

The token is minted per process with `secrets.token_urlsafe(32)`, or fixed through `--token`
when embedding. It is compared with `hmac.compare_digest` over UTF-8, and a missing or
wrong value gets `401 {"error": "unauthorized"}`. The token is published nowhere else: it is
placed in the `/minus.js` prefix, not printed by the CLI and not written to disk.

### CORS

`OPTIONS` always answers `204` with no body. When the request carries an `Origin` matching

```
\Ahttp://(?:127\.0\.0\.1|localhost):\d+\Z
```

the response also carries `Access-Control-Allow-Origin: <origin>`, `Vary: Origin`,
`Access-Control-Allow-Methods: GET, POST, OPTIONS` and
`Access-Control-Allow-Headers: content-type, x-minus-token`. The same headers are added to every
JSON and script reply with a matching origin. Any other origin gets no CORS headers at all. This
exists so a page served from the same machine can read the response; it is not access control.
The token is the boundary, and a non-browser client never sees an `Origin` check.

### Endpoints

| method | path | auth | success |
|---|---|---|---|
| `GET` | `/health` | no | `200 {"ok": true, "frontend": "<name>"}` |
| `GET` | `/minus.js` | no | `200` script text |
| `GET` | `/api/search?q=<text>&limit=<n>` | yes | `200`, envelope below |
| `POST` | `/api/grab` | yes | `202 {"job": "<id>"}` |
| `GET` | `/api/job/<id>` | yes | `200`, job below |

`/api/search` defaults `limit` to `8`. `limit` must parse as an integer (`400` otherwise) and is
then clamped to `1..50` by the service. The query is whitespace-collapsed; an empty query is
`400 {"error": "q is required"}`.

The search envelope is built from the discovery payload, with the threshold added:

| field | meaning |
|---|---|
| `query` | the normalised query discovery used |
| `kind` | discovery's query classification (for example `software`), `""` if unknown |
| `errors` | per-tracker failure strings; empty when every provider answered |
| `threshold` | `Settings.discovery_min_score`; a candidate below it is low-confidence |
| `candidates` | the ranked candidate dictionaries, as `DiscoveryTools.torrent_search` returned them |
| `pick` | discovery's pre-selected candidate, or `null` |

The service does not reshape candidates. The reference client reads `title`, `provider` (falling
back to `tracker`), `size_bytes`, `seeders`, `infohash`, `score`, `eligible`, `reasons`,
`reject_reason` and `low_confidence`; an implementer should treat the dictionary as discovery's,
not as a stable schema of its own.

`/api/grab` requires a JSON object body. Body handling, in order:

| condition | response |
|---|---|
| body over 64 KiB (`65536` bytes) | `413 {"error": "body too large"}`, connection closed |
| body is not valid JSON | `400 {"error": "body must be JSON"}` |
| body is JSON but not an object | `400 {"error": "body must be a JSON object"}` |
| missing/blank `query` or `infohash` | `400 {"error": "query and infohash are required"}` |
| no pipeline (qBittorrent unconfigured) | `503`, message naming `MINUS_QBITTORRENT_URL` |
| the re-search has no candidate with that infohash | `409` naming the infohash |
| that candidate has no `download_url` | `409 {"error": "the selected candidate has no magnet or .torrent URL"}` |
| two jobs already queued or running | `429 {"error": "the grab queue is full; try again shortly"}` |

The call reads `query` and `infohash` from the body and nothing else. The infohash is stripped and
lowercased by the service before comparison, so case and surrounding space do not matter. The
`Content-Type` header is not checked, only whether the bytes parse.

`/api/job/<id>` serialises the service's job dictionary verbatim; `404 {"error": "no such job"}`
covers both an unknown id and one already pruned.

```bash
curl -s http://127.0.0.1:8099/health
# the first line of the script carries the config object, token included
curl -s http://127.0.0.1:8099/minus.js
curl -s 'http://127.0.0.1:8099/api/search?q=debian&limit=8' \
  -H "X-Minus-Token: $token"
curl -s -X POST http://127.0.0.1:8099/api/grab \
  -H "X-Minus-Token: $token" -H 'Content-Type: application/json' \
  -d '{"query": "debian", "infohash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
```

### The script prefix

`/minus.js` returns this line, then the asset bytes:

```js
window.__MINUS_REMOTE__ = {base: "http://127.0.0.1:8099", token: "<token>", frontend: "jellyfin"};
```

All three values are JSON-encoded. `base` comes from `HttpBridge.base_url`; `frontend` is the
resolved frontend's `name`. The asset treats a missing or baseless config as "not mounted" and
returns without touching the page, and it does not mount twice in one document.

## Job model

A grab creates a job in `state: "queued"` and returns its id: 16 lowercase hex characters from
`secrets.token_hex(8)`. Two jobs may be outstanding (queued or running) per service; the pipeline
itself is serialised by a run lock, so the second worker can have a job queued while the first
runs. The job table is pruned back to 64 entries; only `done` and `error` jobs are dropped, oldest
first, leaving queued work alone. Polling a dropped id is `404`.

`state` is exactly one of:

| state | when |
|---|---|
| `queued` | accepted and waiting for a worker |
| `running` | the worker owns it |
| `done` | the pipeline returned a non-`failed` status |
| `error` | the pipeline returned `failed`, the pipeline was unavailable, or the run raised |

`stage` is the pipeline's milestone name, with the bridge's own states before and after it and a
few remapped where the toast needs a different word. The progress values are constants in
`service.py`; nothing else may move the bar.

| stage | progress | source |
|---|---|---|
| `queued` | `0.0` | created |
| `starting` | `0.02` | worker begins |
| `ingest` | unchanged (`0.02`) | before `pipeline.ingest` |
| `download` | `0.05` at add, then sampled | `added` milestone; qBittorrent fraction |
| `staged` | `0.45` | `staged` milestone, first half |
| `scanning` | `0.50` | `staged` milestone, second half |
| `verdict` | `0.80`, unchanged when `malicious_deleted` only renames it | `verdict` milestone; `malicious_deleted` keeps the stage name |
| `promote` | `0.90` | `promoted` milestone, or just before `pipeline.promote` |
| `done` | `1.0` | terminal success |
| failing stage | whatever it had reached | terminal error carries `result.stage` |

Any other pipeline `notify` kind becomes the stage name as sent, with no progress change; the
fixed points above are the only ones the bar knows how to place.

While the job is in `download` and has a torrent hash, a sampler thread reads qBittorrent every
two seconds and maps the transfer fraction onto the download slice:

```
progress = 0.05 + 0.35 * clamp(fraction, 0.0, 1.0)
```

so a full transfer arrives at `0.40`, below the `0.45` staged milestone. Sampling stops at
`0.45` or once the job is `done`/`error`, and a probe that raises (a torrent that vanished) is
logged and ignored. The sampler also sets `detail` to `download <percent>%` plus
` · <speed>` when the speed is above zero (for example `download 50% · 3.1 MB/s`), and
`eta_seconds` from the same read.

`progress` is monotonic: `_update` clamps every new value to at least the previous one, so a
late probe cannot walk the bar backwards past a milestone. `eta_seconds` is the only field taken
from qBittorrent's ETA and is `null` unless `0 <= eta < 8640000`; `8640000` is qBittorrent's
"unknown" sentinel (100 days), not a duration. No other progress source exists.

`elapsed_seconds` is an integer count of seconds since the job was created (`time.monotonic`),
not since it started running.

`events` is the trace the toast expands, oldest first, at most 100 rows. Each row is
`{"at": <ISO 8601 UTC>, "stage": <stage>, "detail": <detail>, "progress": <float>}`. A row is
appended when `stage`, `detail` or `state` changes, and otherwise only when progress has moved at
least `0.05` since the last row; when the cap is hit the oldest rows are deleted. `detail` is the
pipeline's verdict label when one is present, else a payload `name`/`title`, else `""`; the
sampler's `download …` line replaces it while transferring.

The public job shape is frozen and is exactly these keys:

```json
{
  "id": "6f1c2a3b4d5e6f70",
  "state": "running",
  "stage": "download",
  "detail": "download 41% · 3.1 MB/s",
  "result": null,
  "progress": 0.1935,
  "eta_seconds": 134,
  "elapsed_seconds": 27,
  "started_at": "2026-11-19T12:00:00.123456+00:00",
  "updated_at": "2026-11-19T12:00:27.234567+00:00",
  "torrent_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "name": "debian-12.5.0-amd64-netinst.iso",
  "error": "",
  "events": [{"at": "2026-11-19T12:00:00.123456+00:00", "stage": "queued", "detail": "", "progress": 0.0}]
}
```

`torrent_hash` and `error` are empty strings until known; `eta_seconds` and `result` are `null`
until known; `name` starts as the candidate's title. `started_at` and `updated_at` are
`datetime.now(UTC).isoformat()` strings.

### Success and failure

A candidate whose `score` is below the envelope's `threshold` is low confidence. It is still
ingested (quarantine is where a back-check happens), but the bridge never calls `promote` for it.

On a terminal success the bridge sets `state: "done"`, `stage: "done"`, `progress: 1.0`,
`eta_seconds: null`, `error: ""` and a `result` holding at least `status` and `torrent_hash`.
If the status is `clean`, `Settings.discovery_auto_promote` is on and the job is not
low-confidence, it calls `promote` and adds `moved_to` to `result`; a promotion exception is
recorded as `result.promote_error` and does not turn the job into an error, because the download
and verdict did succeed. `needs_review` is reported as `done` with that status and nothing
promoted.

The failure rule is explicit: a pipeline result whose `status` is `"failed"` becomes
`state: "error"`, with `stage` set to `result["stage"]` (default `"failed"`), `detail` and
`error` set to the pipeline's message, `eta_seconds: null`, and

```json
"result": {"status": "failed", "torrent_hash": "...", "stage": "download", "error": "..."}
```

It is not flattened into a `done` job whose `result.status` happens to say `failed`; the state and
the error text must agree. A missing pipeline inside the worker sets `state: "error"`,
`stage: "unavailable"` and the same `MINUS_QBITTORRENT_URL` message as the `503`. An exception
escaping the run sets `state: "error"`, `stage: "failed"`, `detail`/`error` to the exception
text, and leaves the progress where it stood.

`result` carries the pipeline's status and hash, plus `moved_to` or `promote_error` when
promotion was attempted. Consumers should switch on `state` first; `result` explains, it does
not decide.

## Frontend protocol

A frontend is a structural `Protocol` in `src/minus/remote/frontends/__init__.py`; implementations
do not inherit from it. The members are:

| member | contract |
|---|---|
| `name: str` | registry key, reported by `/health`, accepted by every CLI `--frontend` |
| `asset_path() -> Path` | the browser client to serve; both shipped frontends return `remote/web/minus.js` |
| `script_tag(base_url: str) -> str` | a `<script defer … data-minus-remote="1">` tag whose `src` is `base_url` plus `/minus.js`, trailing slash collapsed |
| `install(web_dir: Path) -> bool` | patch the page; `True` only when bytes changed |
| `status(web_dir: Path) -> bool` | whether a marked tag is present; a missing target file is `False` |
| `remove(web_dir: Path) -> bool` | undo the patch; `False` only when there was nothing to undo |
| `notes() -> str` | one sentence on how this frontend is installed, printed by `minus bridge serve` |

`asset_path` is checked at `HttpBridge` construction (`read_text`), so a broken checkout fails at
start-up rather than on the first page load. The registry is explicit:

```python
FRONTENDS = {"jellyfin": JellyfinFrontend(), "generic": GenericFrontend()}
```

`get_frontend(name)` raises `FrontendError` listing the sorted valid names for anything else.
`FrontendError` is a `MinusError`; the CLI turns it into a one-line message and exit status `1`.
A frontend that has no file to patch raises `FrontendError` from `install`, `status` and
`remove` rather than returning a misleading boolean.

The single browser client is written against the API above: `X-Minus-Token` on every call,
`GET /api/search?q=&limit=8`, `POST /api/grab {"query","infohash"}`, then `GET /api/job/<id>`
every two seconds. It renders a launcher and modal appended to `<body>` (outside Jellyfin's
re-rendered tree), and one toast per job that survives the modal closing. It gives up polling
after 30 minutes and labels the toast locally as `slow`; that is a client state, not something
the service reports.

## Jellyfin frontend

`jellyfin-web` is patched in place. `web_dir` is the directory holding `index.html`, default
`/usr/share/jellyfin/web`; `DEFAULT_BASE_URL` is `http://127.0.0.1:8099`.

- The inserted tag is
  `<script defer src="http://127.0.0.1:8099/minus.js" data-minus-remote="1"></script>`.
- The marker is `data-minus-remote`; the legacy marker `data-minus-jellyfin` is recognised by the
  same regular expression, so an install from an older bridge is replaced rather than duplicated
  or double-loaded.
- `install` first strips every script tag carrying either marker, together with the whitespace it
  was inserted with, then inserts exactly one canonical tag immediately before the first
  `</head>`. If there is no `</head>`, it appends the tag at the end of the file instead of
  refusing.
- `install` is idempotent. Before the first edit it writes `index.html.minus-backup` (suffix
  `.minus-backup`) if that file does not already exist; on a re-run the existing backup is kept,
  not overwritten. It holds the page as it was without a bridge tag. The backup is written before
  `index.html`, so a crash between the two still leaves something restorable.
- `remove` restores the backup when present and unlinks it, reporting `True` for that repair;
  without a backup it strips the marked tag and reports `True` only when a tag was removed.
  `install` reports `False` when the resulting page already matched, so "already patched" and
  "nothing to remove" are distinguishable without re-reading.
- `status` reports whether any marked tag exists, and `False` when `index.html` cannot be read.
- Write failures become `FrontendError`; a `PermissionError` names the remedy (`sudo minus bridge
  install --web-dir …` or `remove --web-dir …`) instead of surfacing a traceback.

A `jellyfin-web` upgrade replaces `index.html`, so the tag is gone: `minus bridge status` reports
not installed and `minus bridge install` must be run again. Nothing else in the frontend depends
on Jellyfin's internal bundles or routes.

## Generic frontend

The generic frontend has no installer: `install`, `status` and `remove` all raise
`FrontendError` with a message beginning "no installer; paste the tag from" and naming
`minus bridge tag --frontend generic` and the page's `<head>`. Its `script_tag` is the same shape
and marker as Jellyfin's, and `notes()` says to print the tag and paste it before `</head>`. The
bridge can serve it exactly like the Jellyfin frontend; it simply cannot know where another
app's page lives, and guessing would be worse than asking.

## Security and trust

- **Loopback only by default, and deliberately.** The bridge can add torrents, so `host` and
  `port` are CLI arguments rather than live settings; there is no supported LAN mode. Binding
  elsewhere is an explicit operator choice, not a configuration the assistant will make.
- **The token is the boundary, not CORS.** Anything that can reach loopback can fetch
  `/minus.js` and read the token printed in its first line, so the model is "local user may
  drive the bridge", not per-page authorisation. CORS only decides whether a browser will let a
  legitimately served page read the reply; a non-browser client is unaffected by it.
- **The client supplies an identity, never a URL.** `/api/grab` accepts `query` and `infohash`,
  then re-runs discovery at limit `20` and takes `download_url` from the matching candidate. A
  body carrying its own `download_url` is ignored, so a page cannot push an arbitrary magnet or
  `.torrent` URL into quarantine. An infohash that the fresh search does not return is `409`.
- **Promotion gate.** The bridge calls `promote` only for a `clean` pipeline status, with
  `Settings.discovery_auto_promote` on and a candidate at or above
  `Settings.discovery_min_score`. It adds no promotion shortcut: everything else is left
  in quarantine, and the quarantine, scan, verdict and VM behaviour are the existing pipeline's,
  not the bridge's. A refusal cannot leave a 0% torrent behind, because the VPN preflight runs
  before the magnet is handed to qBittorrent.
- **Bounded input.** Request bodies are capped at 64 KiB, at most two grabs may be outstanding,
  and the asset is read once at construction. Neither `service.py` nor `http.py` executes
  anything from the request; the values passed onward are the normalised query and title, the
  matched candidate's infohash, and the URL discovery returned for it.

## Testing

`tests/test_remote_bridge.py` covers three layers, and deliberately fakes the outside world.

- **HTTP wire format** binds `HttpBridge` to port `0` and speaks real HTTP through
  `http.client`: the exact envelopes and statuses above, token rejection (`401` for missing and
  wrong), search validation (`400`), unknown infohash (`409`), unknown job (`404`), missing
  pipeline (`503`), the third grab (`429`), the `/minus.js` prefix and the fact that its published
  token works, loopback-only CORS, and the job key set including event-row shape. Service
  behaviour is also exercised with no socket, which is the reuse path the split exists for.
- **Fakes** stand in for the two expensive collaborators: `FakeDiscovery` returns the discovery
  envelope without a tracker, and `FakePipeline` records the `ingest(detonate=True,
  release=False, expected_title=...)`/`promote` sequence and can block on a `threading.Event` to
  hold a job in a known stage. `DownloadService(torrent_probe=...)` replaces the qBittorrent read
  for the sampler tests: fraction mapping, the `8640000` ETA sentinel and a probe that raises.
  No test reaches a tracker, qBittorrent, or a Jellyfin installation.
- **Frontend patching** runs against a minimal `index.html` in `tmp_path`: idempotent install with
  one backup, legacy-marker replacement, restore on remove, strip without a backup, missing index
  as `False`, and permission failures turned into a `FrontendError` naming `sudo` (with the
  read-only-directory variant skipped under root).

Not covered, on purpose: the browser client's DOM behaviour is not executed — only the script
prefix and token are asserted — and no test proves a real `jellyfin-web` upgrade layout beyond
the one `index.html` shape.

## Extending

Add one module under `src/minus/remote/frontends/` implementing the protocol, then one line in
`FRONTENDS`. Nothing in `service.py` or `http.py` changes; the CLI's `--frontend` accepts the new
name and `/health` reports it.

1. `name` is the registry key and the value for `--frontend`; it is user-visible, so keep it
   short and lowercase.
2. Return a `Path` under `src/minus/remote/web/` from `asset_path`; the transport prefixes it with
   the config object, so a new asset must read `window.__MINUS_REMOTE__` and talk to the API
   above.
3. If the page can be patched, implement `install`/`status`/`remove` idempotently and take a
   backup before the first edit; if it cannot, raise `FrontendError` from all three and say how
   to paste the tag.
4. Add a registry-resolution test next to the existing ones, and a patching test if there is a
   file to patch.
