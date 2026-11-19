# MINUS

Personal AI Assistant + Home Integration

## What is Minus?

**MINUS** is a personal AI assistant. It is (for now) designed for my personal use. The core gimmick I'm aiming for is to make it like Jarvis from Iron Man. I want to be able to talk outloud and then have minus intelligently take action to assist me. If my hardware allows, I want it to run locally on my server.

## Current Status

A work in progress. Minus is a custom harness around an LLM (via OpenRouter) with
persistent semantic memory, speech in and speech out.

## Install

```bash
uv sync                                          # core + dev tooling
uv sync --extra harness                          # the interactive `minus` surface
uv sync --extra audio --extra embeddings         # everything, incl. mic/TTS
```

Audio (`kokoro-onnx`, `sounddevice`, `RealtimeSTT`) and embeddings
(`sentence-transformers`, which pulls in torch) are optional extras so that
tests and CI stay fast and GPU-free.

Set `OPENROUTER_API_KEY` in `.env`. Everything else is optional and overridable
with `MINUS_`-prefixed environment variables — see `src/minus/config.py`.

## Run

```bash
minus                  # the harness: chat, code, download, vpn, sys
minus voice            # the original microphone assistant
minus voice --no-mic   # type instead of talking
minus tools            # list each agent's tools, and which are switched off
minus memory           # interactively prune stored facts
minus calibrate        # recompute the fact-relevance threshold
minus google-auth      # connect a Google account for the task/calendar tools
```

The harness is the default surface. Type a line to talk to the agent, or use a
slash command: `/mode code`, `/download <query|magnet|url>`, `/vpn connect`,
`/triage <hash>`, `/files`, `/doctor`, `/help`. The left rail switches modes; the right
rail shows tool activity and any quarantine alert. `ctrl+b` opens the file
explorer, which is always docked on the left: clicking a file shows a
syntax-highlighted preview and `diffs` shows the working-tree diff. Drag the
explorer's inner edge to resize the dock, or the divider beside the file tree to
grow the code view; the tree stays a 22-column sidebar and the code view takes
the rest. The dock stops with roughly 25-30 columns left for the conversation.
`minus serve` remains the headless service.

If the shell cannot find `minus`, link the venv script once (then `hash -r` or
open a new shell):

```bash
ln -sf "$PWD/.venv/bin/minus" ~/.local/bin/minus
```

Against an assistant that is already running:

```bash
minus dash                    # the management dashboard
minus say "what time is it"   # inject a line, as though it had been spoken
minus status                  # what it is doing right now
minus status --watch          # ...and keep printing as that changes
```

## Torrents, VPN and quarantine

In download mode a plain line is a search. `/download the great gatsby` (or
just `the great gatsby` while that mode is selected) queries cheap meta-index
APIs, ranks the results locally, and sends only the winner through the same
pipeline as a typed magnet: qBittorrent downloads into `quarantine/incoming`,
the pipeline copies the finished content into `quarantine/<hash>/`, inspects it,
asks the agent model to read any code-shaped files, and records a verdict. A
`clean` verdict from a discovery download is promoted automatically; a magnet
or `.torrent` URL still bypasses search and goes straight to the pipeline. A
typed magnet can release a `clean` result in the same call
(`triage ingest ... --release`; the harness's download mode does this);
`needs_review` stays in quarantine until the Windows guest has reviewed it
(`minus triage vm`), `triage_review` has looked at it and
`triage_promote --accept-review` explicitly accepts it. There is no path from a
failed scan to the library.

### How discovery picks

The default providers are **Knaben** (a meta-index whose JSON API already
carries 1337x and The Pirate Bay results), **Bitsearch** (a second index, so a
repeated infohash is evidence of a real release), **The Pirate Bay** via
`apibay.org`, and **Nyaa** via RSS. **Internet Archive** is the fallback when
the trackers produce nothing eligible.

**Prowlarr** and **Jackett** are used when configured. They own the per-site
definitions, so every site they support becomes searchable without a scraper in
MINUS — including 1337x and YourBittorrent, which answer plain HTTP with a
Cloudflare interstitial. Set `MINUS_PROWLARR_URL`/`MINUS_PROWLARR_API_KEY` or
`MINUS_JACKETT_URL`/`MINUS_JACKETT_API_KEY`; `MINUS_JACKETT_INDEXERS` can target
specific definitions. Deprecated sites such as RARBG and old Demonoid clones
are not added.

Ranking is deterministic and cheap — no model call. It rewards exact title
coverage, seeders, 1080p BluRay/Web-DL, verified uploads, agreement between
sources, a low Knaben virus score and a legal origin. It rejects CAM/TS
releases, executable/crack-style names, dead swarms, wrong-year matches,
category mismatches, and anything larger than `MINUS_TRIAGE_MAX_BYTES`.
`MINUS_DISCOVERY_MIN_SCORE` (default 45) is the normal cutoff. If nothing
clears it, a candidate that still passed every hard filter and scores at least
`MINUS_DISCOVERY_LOW_CONFIDENCE_SCORE` (default 25) is downloaded for the VM to
back-check and explicitly marked low confidence; it is **never** auto-promoted.
Below both floors the harness shows the shortlist and downloads nothing.

This ranking is a pre-filter, **not** a trust boundary. Every selected torrent
still lands in quarantine and still needs the verdict rules; a static-only scan
is `needs_review`, so without a configured detonation sandbox nothing is
auto-promoted and the content stays in quarantine. Tune discovery with
`MINUS_DISCOVERY_PROVIDERS`, `MINUS_DISCOVERY_FALLBACK_PROVIDERS`,
`MINUS_DISCOVERY_MIN_SCORE`, `MINUS_DISCOVERY_LIMIT`,
`MINUS_DISCOVERY_PREFERRED_QUALITY` and `MINUS_DISCOVERY_AUTO_PROMOTE`.
For open indexer coverage set `MINUS_JACKETT_URL` + `MINUS_JACKETT_API_KEY`
(optionally `MINUS_JACKETT_INDEXERS`), or the Prowlarr equivalents.

Direct scraping of 1337x and YourBittorrent is not used: both answer plain HTTP
with a Cloudflare interstitial. Demonoid is account-based, has no live public
API, and its original site closed in 2018; RARBG is gone. Knaben's 1337x
coverage plus Jackett/Prowlarr definitions are the supported routes to those
sites. The built-in direct providers are Knaben, Bitsearch, The Pirate Bay
(`apibay.org`) and Nyaa (RSS).

```bash
minus qbt setup                # start qBittorrent, loopback WebUI, quarantine paths
minus qbt status               # version, speeds and recent torrents
minus vpn status               # provider, interface, route, triage gate
minus vpn connect              # AirVPN through bluetit/goldcrest
minus vpn disconnect
minus triage ingest 'magnet:?xt=urn:btih:...'
minus triage ingest 'magnet:?xt=urn:btih:...' --release
                               # move it into library/ only if the verdict is clean
minus triage download <hash>   # stage a finished torrent into quarantine
minus triage scan <hash>       # static + host media sample + Windows review if configured
minus triage verdict <hash>
minus triage review <hash>     # static facts + sandboxed ffprobe; no promotion
minus triage preview <hash>    # re-sample frames into quarantine/<hash>/preview/
minus triage vm <hash>         # open every staged file in the Windows guest; no verdict change
minus triage vm <hash> --keep movie.mp4
                               # keep only those; everything else goes to the guest graveyard
minus triage promote <hash>    # refused unless the verdict is clean; asks qBittorrent
                               # to delete the torrent and its files (default)
minus triage promote <hash> --keep movie.mp4
                               # copy only the kept .mp4 files into library/
minus triage promote <hash> --accept-review --yes
                               # manual release of a needs_review item after VM review
minus triage promote <hash> --accept-review --yes --keep movie.mp4
                               # ...promoting only the named files
```

`MINUS_TRIAGE_AUTO_CLEANUP` (on by default) is what removes the download copy:
after a successful promotion — a `clean` verdict or a manual `--accept-review`
release — MINUS asks qBittorrent to delete the torrent and the files it
downloaded, because the content now lives in `library/` and its provenance is under
`memory/library_provenance/`.
The same happens after a `malicious` verdict, whose staged payload is deleted
outright. `needs_review` and `suspicious` keep the download, since it is what
you would inspect or release next. A cleanup that fails is logged and does not
undo the promotion or the rejection. Set
`MINUS_TRIAGE_AUTO_CLEANUP=false` to keep finished downloads until you remove
them yourself.

`triage review` prints the verdict, static flags, file list and a bounded
`ffprobe` pass run under bubblewrap (read-only input, no network). It is a
parser smoke test, not a detonation. `--accept-review` only releases a
`needs_review` item whose staged files are **passive media/text** (video, audio,
images, subtitles, plain text) and whose static/airlock flags show no
executable, script, archive, PDF/Office or other active content. It also
**requires VM review evidence**: `verdict.json` or `vm_review.json` must record
that the Windows guest opened or launched at least one file (`sandbox.detonated`
or `sandbox.opened > 0`). A dry run, a host-only `ffprobe`/frame sample, or a
scan with `--no-detonate` cannot satisfy that gate; promotion refuses and names
`minus triage vm` as the missing step. The provenance records the accepting
user, time, reason, `detonated` and `vm_reviewed: true`. `suspicious`,
`malicious` and any active-content payload are never releasable this way.
Without `--yes` the terminal asks you to type the first eight characters of the
hash.

### Windows review, including media

The detonation guest can also review files that are not Windows programs. In
review mode it reads every non-empty staged file from a **read-only CD** (an
ISO labelled `MINUS_REVIEW`), opens each one with the Windows application a
person would use, and records process, network, file, registry and Defender
evidence. Video is opened in the portable VLC installed in the base image
(or Windows Media Player when VLC is absent), images in Paint, text and
subtitles in Notepad, and anything else through the shell association.
Only an explicit handler that stays alive counts as opened; the shell
association may be the "how do you want to open this file?" dialog and is never
counted. Launchable shapes (`.exe`, `.ps1`, `.lnk`, …) are executed; everything
else is opened, because opening a movie in a player exercises the parser path
its viewer would take. A missing CD or a file the guest never opened leaves
the review incomplete, so it cannot make the verdict `clean`.

The read-only CD exists because of size: the WinRM upload used for detonation is
bounded by `MINUS_WINDOWS_MAX_ARTIFACT_BYTES` (64 MiB by default, per file and
in total), while the ISO channel carries files up to the ISO9660 level-3
single-file limit just under 4 GiB. A 2 GiB movie never becomes a million WinRM
chunks; anything over that ISO limit is refused before an image is written.
QEMU mounts the ISO read-only, so the guest can read it but cannot write back
to the host's copy.

```bash
minus triage scan <hash>        # review mode runs automatically when the guest is ready
minus triage vm <hash>          # run the review without touching the verdict
minus triage vm <hash> --keep movie.mp4
```

`scan` uses review mode automatically when a base image is configured, the
guest is ready (including `pycdlib`), and `MINUS_TRIAGE_VM_REVIEW` is on;
`--no-detonate` skips it. `minus triage vm` is
the standalone version: it writes `quarantine/<hash>/vm_review.json` and leaves
`verdict.json` alone, which is what an operator re-reads before accepting a
`needs_review` item. Opening a movie counts as a detonation only when the guest
reports it opened; a guest that opens nothing leaves the verdict at
`needs_review`.

Review also decides what the torrent is for. With no `--keep`, the primary-file
rule applies: every video, else every audio file, else every file. Files named
in `--keep` (relative paths or basenames) override that. Everything not kept is
moved into the guest's graveyard (`C:\triage\graveyard` inside the throwaway
overlay) and is not part of the review's working set; after a successful
promotion the staged host tree is deleted too, so only the kept files and the
library provenance remain. Promotion computes its own selection from the host
manifest, so promotion refuses a selection that includes a file the review did
not keep rather than copying it. Pass the same `--keep` list to `triage promote`.
A `--keep` entry that matches nothing is refused rather than silently promoting
everything.

VM review is evidence, not a guarantee: a clean result means the guest opened
the file and nothing suspicious was observed, not that the file is provably
safe. See `docs/VM_REVIEW.md` for the image build, Sysmon setup, environment
knobs and the evidence format.

### What lands in the library

`library/` holds **only flat `.mp4` files**: `library/<movie>.mp4`, no release
folders, artwork, subtitles or sidecars. The 0600 provenance JSON (verdict,
manifest, VM review, and for a manual acceptance the operator/time/reason) is
written under `memory/library_provenance/<slug>.provenance.json`, outside the
tree Jellyfin scans. Point a Jellyfin library at `library/` and it sees movies
only. A promotion with no `.mp4` is refused; non-MP4 files stay in the guest/
staged graveyard.

### Previewing media on the host

The Windows guest answers "what does opening this do"; it does not sample frames
or compare a title. That is a separate, cheaper host check: ffprobe and ffmpeg
run under bubblewrap with the staged tree read-only and a separate frame
directory writable. That is parser containment, not a VM — the files stay in
quarantine and nothing is executed.

```bash
minus triage preview <hash> --frames 6 --title "the great gatsby 2013"
```

Frames and a contact sheet land in `quarantine/<hash>/preview/`. `--title` is
optional and advisory: it only decides whether the sampled file resembles what
you asked for, reported as `media:title-mismatch`. `triage_review` and
`triage_preview` both return the sampled paths, so a vision-capable agent opens
the frames instead of guessing from a filename. They show what the file is, not
that it is safe — promotion still follows the verdict gate.

### Watching a download

Every stage appends a line to `quarantine/trace.jsonl` —
`{ts, op_id, stage, event, torrent_hash, duration_ms, data}` — so what happened
is still readable after the terminal has scrolled on:

```bash
minus triage trace                # the most recent events, across torrents
minus triage trace <hash>         # just this torrent
tail -f quarantine/trace.jsonl    # follow a run while it is happening
```

`minus triage trace` needs no hash because "did the sandbox run?" is a question
you ask before knowing which torrent to blame; the same record backs the
`triage_trace` MCP tool.

An agent client can drive the whole flow over MCP without the dashboard:
`torrent_search` → `torrent_ingest` → `triage_verdict` → `triage_review` →
`triage_vm_review` → `triage_promote(accept_review=true, reason=...)`.
`triage_review` reports the facts and whether manual release is allowed;
`triage_vm_review` opens the staged files in the Windows guest and writes
`vm_review.json`; `triage_promote` still refuses anything but `clean` unless the
operator explicitly accepts a passive `needs_review` item after that evidence
exists.

Configuration lives in `.env` (0600, gitignored): `MINUS_AIRVPN_USERNAME`,
`MINUS_AIRVPN_PASSWORD`, `MINUS_QBITTORRENT_URL`,
`MINUS_QBITTORRENT_PASSWORD`, and the `MINUS_TRIAGE_*` knobs. The AirVPN
password is never returned by the control socket, logged, or included in an
error string.

With credentials present, the harness brings the tunnel up on launch when
`MINUS_VPN_AUTOCONNECT` is on (the default) and no healthy tunnel already
exists, then binds qBittorrent to the discovered interface. Opening the harness
twice does not start a second client; `/vpn status` shows what happened.

AirVPN's WireGuard mode needs a loadable `wireguard` kernel module. If the
running kernel cannot load one, set `MINUS_AIRVPN_VPN_TYPE=openvpn` to use
AirVPN's alternate transport, or reboot into a kernel whose modules match.

A malicious verdict deletes the staged payload immediately, writes a 0600
record under `quarantine/rejected/` (hash, reasons, file names — never bytes),
and returns an `ALERT` the harness shows. Promotion stays refused for anything
that is not `clean`; `clean` requires a detonation that observed nothing and a
model review that did not escalate.

### The QEMU detonation guest

A `clean` verdict needs a real Windows guest, not just the static checks, and
the same guest reviews media it cannot execute. Install QEMU/KVM
(`qemu-system-x86_64`, `qemu-img`) plus `mtools` for the unattended-answer
floppy, and the Python transport the runner drives it with:

```bash
uv sync --extra windows          # adds pywinrm and pycdlib
```

Build the base image once from an **official Microsoft Windows 10 x64 ISO** —
Enterprise LTSC or the Enterprise Evaluation both work; the Evaluation needs no
licence at all, and LTSC installs with its published KMS client key and runs
unactivated for the life of a detonation image:

```bash
MINUS_WINDOWS_WINRM_PASSWORD=...     # the guest account the harness drives
minus vm-image --iso /path/to/Win10_Enterprise_x64.iso \
               --work-dir /srv/minus/windows \
               --sysmon-zip /path/to/Sysmon.zip \
               --vlc-zip /path/to/vlc-3.0.24-win64.zip
```

`--sysmon-zip` turns process/network/file/registry evidence into event logs the
harness reads directly; without it the harness falls back to polling
`Get-Process`, `Get-NetTCPConnection` and the Windows `Run` keys, which sees less
between samples but is still real telemetry, not a pretend result. `--vlc-zip`
installs a portable VLC under `C:\tools\vlc`; review mode prefers it for video
because Windows Media Player's first-run setup can exit instead of opening the
file. Both are optional, but a media review without a working player leaves
`opened` at zero and the verdict at `needs_review`. Do not use a trimmed
"Tiny11"/"Lean11"-style ISO: those remove WinRM, Defender, PowerShell and often
Sysmon, and a guest without them can neither be driven nor produce behaviour
that means anything.

Then point MINUS at the base image and the scratch space:

```bash
MINUS_WINDOWS_BASE_IMAGE=/srv/minus/windows/base.qcow2
MINUS_WINDOWS_WORK_DIR=/srv/minus/windows/run
```

`minus vm-check` says whether a run could start and what is still missing
before you build or scan. The other knobs are
`MINUS_WINDOWS_WINRM_USER`, `MINUS_WINDOWS_CPUS`, `MINUS_WINDOWS_MEMORY_MB`,
`MINUS_WINDOWS_BOOT_TIMEOUT`, `MINUS_WINDOWS_GUEST_TIMEOUT` (the WinRM
operation timeout), `MINUS_WINDOWS_MAX_ARTIFACT_BYTES` (the detonation upload
cap, 64 MiB) and `MINUS_WINDOWS_MIN_FREE_BYTES` (20 GiB). Review mode
(`MINUS_TRIAGE_VM_REVIEW`, on by default) is bounded by
`MINUS_TRIAGE_VM_REVIEW_TIMEOUT` (300 seconds); a failed run keeps its overlay,
console log and pidfile under `<MINUS_WINDOWS_WORK_DIR>/failed/<run>/` rather
than deleting the only record of what went wrong.

The guest boots under SeaBIOS with an IDE disk and an e1000 NIC. That is the
entire requirement: Windows 10 setup carries in-box drivers for both, so the
unattended install completes with no driver ISO. There is no OVMF/UEFI, no
emulated TPM and no virtio-win ISO to prepare — those are the things that turn
a one-off image build into an afternoon of driver wrangling, and for a
short detonation I/O speed is not what you are short of. A virtio-win ISO is
optional only if you deliberately want faster disk I/O.

The runner invokes `qemu-system-x86_64` (override with `MINUS_WINDOWS_QEMU`),
boots an immutable base plus a throwaway per-run overlay, and never exposes a
host share — do not add one back for convenience. A detonation pushes artifacts
over WinRM in base64 chunks; a review attaches them as a read-only CD, and the
guest copies them into its own writable working directory. Either way there is
no host→guest filesystem path the guest can browse. The guest's only network is
QEMU's `user,restrict=on` device with a single host→guest WinRM forward; keep it
that way rather than bridging the NIC, because the point of the guest is that it
has no route out.

The base image is a 64 GiB sparse qcow2 that Windows fills out to roughly 20–30
GiB of real blocks once installed. Put `MINUS_WINDOWS_BASE_IMAGE` and
`MINUS_WINDOWS_WORK_DIR` on an ext4 volume with that much room — the ext4 root
is usually the easiest (check `df`; this machine's root has about 62 GiB free).
Do not put them on an NTFS partition shared with Windows. A hibernated or
Fast-Startup volume still has Windows' journal open, so mounting it read-write
lets Linux's NTFS driver replay a journal Windows still holds and corrupts the
filesystem for both systems; the images would also compete with Windows for
free space. If NTFS is the only large volume you have, disable Fast Startup,
shut Windows down fully, mount read-write, and treat it as a stopgap.

`MINUS_WINDOWS_MIN_FREE_BYTES` (default 20 GiB) is checked before a run starts,
so a nearly-full disk fails before QEMU boots rather than mid-detonation. The
Windows ISO is only an input to the image build: once `minus vm-image` has
finished, it can be deleted, because every run boots the base image and a
throwaway overlay.

## Jellyfin search bridge

Jellyfin has no plugin hook for its web UI, so this is not a plugin:
`minus jellyfin serve` runs a small HTTP service on loopback, and a one-time
edit to `jellyfin-web`'s `index.html` adds a single script tag. Everything
behind the search is download mode's own path -- `discover` ranks,
`choose_best` picks, and the winner enters the same quarantine pipeline a typed
magnet does -- so a click in Jellyfin replaces the keystroke and nothing else.

```bash
minus jellyfin serve                              # 127.0.0.1:8099
sudo "$(command -v minus)" jellyfin web install   # patch /usr/share/jellyfin/web/index.html
minus jellyfin web status
sudo "$(command -v minus)" jellyfin web remove
```

`web install` keeps `index.html.minus-backup`, is idempotent, and must be re-run
after a `jellyfin-web` upgrade replaces the file. Reload the web client and a
launcher appears in the bottom-right corner: type a query, get the same ranked
shortlist the harness shows with size, seeders and score, and send a row to
quarantine. A library search that finds nothing also grows a bar offering the
same search, which is the case that motivates the whole thing.

The bridge is a `--user` service like the assistant itself; a unit whose
`ExecStart` is this checkout's `minus jellyfin serve` keeps it up across
logins:

```bash
systemctl --user enable --now minus-jellyfin
```

The bridge binds loopback deliberately -- it can add torrents, so it is not a
LAN service -- which means the launcher works only in a browser on the machine
running Jellyfin. The token the script carries is generated per process and
never written to disk.

What comes back is not an instant play button. A grab is one `clean` verdict
away from the library: the swarm downloads into `quarantine/incoming`, the
pipeline scans it, and only a clean, non-low-confidence result is promoted
automatically. Everything else waits in quarantine, exactly as `needs_review`
does in the harness.

## MCP server

Every portable tool MINUS builds is also available to any Model Context
Protocol client:

```bash
minus mcp                       # or: minus-mcp / python -m minus.mcp
```

The repository ships `.mcp.json`; point a client at it or at the same
command/args. `minus mcp-config` prints the portable version for a client that
does not start from this checkout — absolute interpreter and
`MINUS_PROJECT_ROOT` — so the server mounts from any working directory:

```bash
minus mcp-config
```

The server is stdlib-only, speaks newline-delimited JSON-RPC over
stdio, and exposes the built-in, alarm, triage, VPN and (when configured)
Google groups. See `docs/MCP.md` for the protocol surface and safety boundary.

## Dashboard

```bash
uv sync --extra dashboard
minus dash            # add --unicode for a terminal emulator rather than a VT
```

```
┌───────────────────────────────┬───────────────────────────────┐
│                               │  MEMORY                   [m] │
│   viewer            (2/3)     ├───────────────────────────────┤
│   ← conversation │ log │      │  HARDWARE                 [h] │
│     deep think →              ├───────────────────────────────┤
│                               │  TOOLS                    [t] │
│                               ├───────────────────────────────┤
├───────────────────────────────┤  EXTERNAL PROGRAMS        [p] │
│   input             > _       ├───────────────────────────────┤
├───────────────────────────────┤  AGENTS                   [a] │
│   console  [c]      (1/3)     ├───────────────────────────────┤
│   hidden until asked for      │  MANAGEMENT               [g] │
└───────────────────────────────┴───────────────────────────────┘
```

It opens holding nothing, so the letter keys are keys rather than typed text.
`m h t p a g` expands a panel and focuses it, `v` focuses the viewer and `V`
gives it the whole screen, `tab` walks everything, `enter` expands whatever is
focused, `i` focuses the input, `s` stops MINUS mid-reply, `e` ends the current
conversation and opens a fresh one, `c` opens the console, `R` restarts the
service, and `q` quits **the dashboard, not MINUS**.

`e` is the rollover a silence would eventually do, done now: the conversation
is condensed, its facts are extracted into the store the next one starts from,
and the transcript is dropped.

Focus lives in exactly one place: opening a panel takes it off the viewer or
the console, and pressing `v` or `c` collapses an expanded panel. Pressing the
key for what you already have puts it back down. The one exception is `i` —
you can type at the input box with a panel still enlarged behind it.

`escape` walks back out the way you came: it drops fullscreen, then steps out
of the input box back to the panel it was entered from, then collapses that
panel, then closes the console, and finally lets go of focus altogether.

The bare arrow keys belong to whatever has focus, so `←`/`→` scroll a wide log
line sideways and `↑`/`↓` scroll a deep-think note. Both modified with `ctrl`
move between things instead: `ctrl+←`/`ctrl+→` switch the view, and
`ctrl+↑`/`ctrl+↓` page between deep-think notes. Those keep working while the
input box has focus, which owns the bare arrows for its cursor.

The Linux console does not send a modifier with an arrow key at all — `ctrl+←`
reaches the dashboard as a plain `←` — so the same two jobs have unmodified
keys for the tty: `[`/`]` switch the view and `{`/`}` page between notes. Being
ordinary characters, they are typed rather than obeyed while the input box has
focus, the same as `1`/`2`/`3`.

Built for the console on the machine itself: sixteen ANSI colours, ASCII
borders and character meters, because the VT font has no block-drawing glyphs.
`--unicode` relaxes that over SSH.

It is a separate process and a separate dependency. `minus serve` never
imports textual. Reads come from the files MINUS already writes, so the log,
the conversation and the deep-think notes still render with the assistant
stopped -- only the input box needs the socket.

The console follows the same bargain. `minus serve` redirects its own stdout
and stderr into `logs/console-*.log` before anything can write to them, which
is the only way to catch what the C extensions print: they write to the file
descriptors directly and have never heard of the logging module. Under
systemd that output went to `/dev/null` and the journal, where the dashboard
could not reach it.

Three of the six panels do something once they are expanded. `m` lists every
fact MINUS remembers -- `↑`/`↓` moves, `space` marks, and `d` twice forgets
what is marked. `t` lists each agent's tools, grouped into folders by
category -- `←`/`→` moves between the conversational and deep-think agents
(and the coding agent, which has none yet), `space` or `tab` opens and closes
the folder under the cursor, and `space` switches the tool under the cursor on
or off for that agent alone. Tools that belong to no category sit loose at the
bottom of the list. A tool switched off is no longer offered to that
model, and the switch is written to `.env` as `MINUS_DISABLED_TOOLS`, so it
survives a restart. `g` lists every field of `config.py`, changes the ones
that can be changed live, and says why the rest cannot.

The other two are still scaffolding: they say "nothing here yet", and filling
one in means returning a list from its `options()`.

## Running as a service

```bash
minus systemd-unit > ~/.config/systemd/user/minus.service
systemctl --user daemon-reload
systemctl --user enable --now minus
loginctl enable-linger "$USER"     # or it is killed at logout
```

`minus serve` is the headless mode the unit runs: no console logging, and
`--no-mic` there means "take input only from the control socket" rather than
"read stdin", since a service has no stdin worth reading.

The unit is generated rather than tracked, because it has to name this
checkout and this interpreter. It is a `--user` unit: the assistant's audio
comes from the login session's PipeWire, which a system service cannot reach.

Stopping is graceful. `systemctl --user stop minus` sends SIGTERM, which ends
the conversation properly -- condensing it and extracting facts -- rather than
discarding what the session learned.

### The control socket

A running assistant listens on `$XDG_RUNTIME_DIR/minus/control.sock`
(newline-delimited JSON; see `src/minus/control/protocol.py`). It doubles as a
single-instance lock: a second `minus` refuses to start rather than fight the
first one for the microphone. Pass `--no-control` to run one alongside anyway.

## Google Tasks and Calendar

Minus can add, edit, move and delete both tasks and calendar events by voice
once a Google account is connected. Nothing is registered until it is, so an
unconnected install has ten fewer tools rather than ten that fail.

**It asks rather than guesses.** The fields it will not invent are the ones you
would notice being wrong: a task's title, due date and list; an event's title,
location, calendar, and whether it is all day or runs between two times. If you
say "put the dentist in for Tuesday", it asks what time and how long rather than
inventing an hour-long slot. That is enforced in the tools themselves, not just
asked for in the prompt — a call missing one of those fields is refused with the
question to put to you, and the model is told not to call it again until you
have answered. Saying "there's no due date" or "no location" works too: those
are answers, and they are recorded as such.

MINUS picks a list or calendar for itself only when there is nothing to choose:
when you named one, when `.env` names one, or when the account has exactly one
you can write to. Otherwise it asks. Read-only calendars you subscribe to
(holidays, birthdays, someone else's shared calendar) are shown when reading and
never offered as somewhere to put something.

Connecting is a one-time consent round trip through a browser:

1. Create a project at
   [console.cloud.google.com](https://console.cloud.google.com/projectcreate).
2. **APIs & Services → Library** — enable both **Google Tasks API** and
   **Google Calendar API**.
3. **OAuth consent screen → External.** Add your own Google account under
   *Audience* as a **Test user**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app.** Copy
   the client ID and secret.
5. Run `minus google-auth`, paste them in, approve in the browser it opens, and
   let it write the result to `.env`.

That leaves these lines in `.env`:

```bash
MINUS_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
MINUS_GOOGLE_CLIENT_SECRET=...
MINUS_GOOGLE_REFRESH_TOKEN=1//...      # what `minus google-auth` produces
MINUS_GOOGLE_TASKS_LIST=My Tasks       # optional; stops it asking which list
MINUS_GOOGLE_CALENDAR=Personal         # optional; stops it asking which calendar
```

The two optional lines are how you stop being asked every time without MINUS
ever guessing: naming one is a preference you stated, which is not the same as
it picking for you. Leave them out on a single-list, single-calendar account —
there is nothing to choose there, so nothing gets asked.

**Already connected for tasks?** Scopes cannot be added to a grant after the
fact: a refresh token issued when MINUS only knew about tasks stays a tasks-only
token, and the calendar tools will tell you so. Enable the Calendar API (step 2)
and run `minus google-auth` again — it re-approves both and replaces the token.

The refresh token is the only durable part, and it is what the assistant trades
for an hour-long access token on each run — nothing is written to disk while
Minus is running, and one token serves both APIs. Two things expire it:
revoking the app at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions),
and leaving the consent screen in **Testing** mode, which expires refresh tokens
after seven days. Publishing the app on that same screen stops the second one,
and needs no review while you are its only user. Either way the fix is to run
`minus google-auth` again.

The tools are conversational-tier only. The deep tier reads files to ground an
answer; it does not get to put things on your calendar from a background thread.

## Architecture

```
src/minus/
├── cli.py          argument parsing and one function per subcommand
├── assembly.py     composition root — the only place that picks implementations
├── runtime.py      the conversation loop, the deep courier, the idle rollover
├── config.py       every tunable value, env-overridable
├── paths.py        the single definition of where data lives
├── prompts.py      prompt text; imports nothing but paths, so anything may use it
├── core/           protocols, typed messages, agent loop, deep tier
├── llm/            OpenRouter client + malformed-tool-call retry
├── tools/          @tool registry, schema derivation, built-in tools
├── memory/         transcripts, condensation, fact extraction, fact store
├── services/       json helpers, the deep-answer detail sink, the .env writer
├── control/        the socket protocol, server, client, live config, systemd
├── system/         /proc and /sys readers for the hardware panel
├── dashboard/      the TUI (the only package allowed to import textual)
└── audio/          interrupt bus, speech-to-text, text-to-speech
```

Collaborators are injected rather than imported, and the seams are declared as
protocols in `core/protocols.py` (`ChatModel`, `TranscriptSource`,
`SpeechSynthesizer`, `FactStore`, `Embedder`). Swapping a model provider, TTS
backend or fact store is a change to `assembly.py`.

The three top-level modules are layered, and only downwards: `cli.py` parses
arguments and calls into `assembly.py`, which builds the object graph and hands
it to `runtime.py`, which runs it and constructs none of it. That is what lets
the tests drive a whole conversation with fakes and no entry point involved.

### Adding a tool

One decorated function. The JSON schema is derived from the signature and the
docstring, so there is no second place to keep in sync:

```python
from minus.tools.registry import registry


@registry.tool
def set_light(room: str, brightness: int = 100) -> dict:
    """Set a room's light brightness.

    Args:
        room: Room name, e.g. "office".
        brightness: Brightness from 0 to 100.
    """
    ...
```

Import it in `src/minus/tools/__init__.py` and it is live.

### Two model tiers

A model fast enough to feel spoken is not a model that reasons well. Minus runs
both rather than compromising:

- `chat_model` carries every conversation and the everyday tools.
- When it meets something beyond it — designing, planning, weighing tradeoffs,
  reading several files at once — it calls the `escalate` tool, and
  `deep_model` answers on a background thread. The conversation stays live the
  whole time; the fast model just says it is on it.
- The deep tier answers in two channels. The short one is spoken. The full
  write-up goes to a `DetailSink` (today, a file under `memory/deep_notes/`)
  and is never read aloud.
- Depth comes from `deep_reasoning_effort`, not model size — the default deep
  model reasons on demand, so dropping that parameter would make the tier
  pointless.

Set `MINUS_DEEP_MODEL` to change tiers without touching code.

### Semantic Memory

Minus remembers facts and preferences between sessions:

- Every user message is appended with potentially relevant facts, ranked by
  comparing the embedding of the message against each fact.
- When a conversation ends it is condensed, and the LLM extracts durable facts
  from the transcript.
- A conversation ends after 30 seconds of silence, not when the process does.
  That timer measures the quiet since MINUS *stopped talking*, so a deep answer
  that lands two minutes after the question still leaves a full silence to
  reply into. `MINUS_IDLE_CONVERSATION_SECONDS=0` disables it and goes back to
  one conversation per run.
- Facts are structured `(attribute, value)` slots. Dedupe and supersede are
  exact matches on the normalized attribute, not similarity thresholds.
- Single-valued attributes supersede; multi-valued ones accumulate.

Known attributes are fed back into the extraction prompt so the model reuses
`preferred_language` instead of inventing `programming_language`.

## Development

```bash
uv run pytest             # all the tests, no audio or torch needed
uv run ruff check .
uv run ruff format .
uv run mypy src
```

## Features

### Home Integration (hardware required)

- [ ] Play music
- [ ] Control lights
- [x] Build a dedicated MINUS dashboard screen

### General Assistance

- [x] Create calendar events and tasks
- [ ] Set reminders
- [ ] Set alarms

### Project Assistance

- [ ] Spawn agents
- [ ] Talk through problems

## Design Guidelines

- Be funny
- Be helpful
- Call out bad ideas
- Avoid unnecessary refusals
- Prioritize fast responses over in depth analysis for conversations.
