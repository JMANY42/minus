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
asks the agent model to read any code-shaped files, and records a verdict. Only
a `clean` verdict is promoted into `library/`; a magnet or `.torrent` URL still
bypasses search and goes straight to the pipeline.

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
minus triage download <hash>   # stage a finished torrent into quarantine
minus triage scan <hash>       # static + model review (+ detonation if configured)
minus triage verdict <hash>
minus triage review <hash>     # static facts + sandboxed ffprobe; no promotion
minus triage promote <hash>    # refused unless the verdict is clean; asks qBittorrent
                               # to delete the torrent and its files (default)
minus triage promote <hash> --accept-review --yes
                               # explicit manual release of a needs_review item
```

`MINUS_TRIAGE_AUTO_CLEANUP` (on by default) is what removes the download copy:
after a successful promotion — a `clean` verdict or a manual `--accept-review`
release — MINUS asks qBittorrent to delete the torrent and the files it
downloaded, because the content now lives in `library/` with its provenance.
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
executable, script, archive, PDF/Office or other active content. It records the
accepting user, time, reason and `detonated: false` in the library provenance
JSON. `suspicious`, `malicious` and any active-content payload are never
releasable this way. Without `--yes` the terminal asks you to type the first
eight characters of the hash.

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

A `clean` verdict needs a real Windows guest, not just the static checks.
Install QEMU/KVM (`qemu-system-x86_64`, `qemu-img`) plus `mtools` for the
unattended-answer floppy, then point MINUS at the base image and the scratch
space:

```bash
MINUS_WINDOWS_BASE_IMAGE=/srv/minus/windows/base.qcow2
MINUS_WINDOWS_WORK_DIR=/srv/minus/windows/run
```

`minus vm-image` builds the base image once from an **official Microsoft
Windows 10 x64 ISO** — Enterprise LTSC or the Enterprise Evaluation both work;
the Evaluation needs no licence at all, and LTSC installs with its published
KMS client key and runs unactivated for the life of a detonation image. Do not
use a trimmed "Tiny11"/"Lean11"-style ISO: those remove WinRM, Defender,
PowerShell and often Sysmon, and a guest without them can neither be driven nor
produce behaviour that means anything. `minus vm-check` says what is still
missing before you build.

The guest boots under SeaBIOS with an IDE disk and an e1000 NIC. That is the
entire requirement: Windows 10 setup carries in-box drivers for both, so the
unattended install completes with no driver ISO. There is no OVMF/UEFI, no
emulated TPM and no virtio-win ISO to prepare — those are the things that turn
a one-off image build into an afternoon of driver wrangling, and for a
short detonation I/O speed is not what you are short of. A virtio-win ISO is
optional only if you deliberately want faster disk I/O.

The runner invokes `qemu-system-x86_64` (override with `MINUS_WINDOWS_QEMU`),
boots an immutable base plus a throwaway per-run overlay, and never exposes a
host share — do not add one back for convenience. The guest's only network is
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

## MCP server

Every portable tool MINUS builds is also available to any Model Context
Protocol client:

```bash
minus mcp                       # or: minus-mcp / python -m minus.mcp
```

The repository ships `.mcp.json`; point a client at it or at the same
command/args. The server is stdlib-only, speaks newline-delimited JSON-RPC over
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
