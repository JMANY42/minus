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
uv sync --extra audio --extra embeddings         # everything, incl. mic/TTS
```

Audio (`kokoro-onnx`, `sounddevice`, `RealtimeSTT`) and embeddings
(`sentence-transformers`, which pulls in torch) are optional extras so that
tests and CI stay fast and GPU-free.

Set `OPENROUTER_API_KEY` in `.env`. Everything else is optional and overridable
with `MINUS_`-prefixed environment variables — see `src/minus/config.py`.

## Run

```bash
minus                  # microphone mode
minus --no-mic         # type instead of talking
minus tools            # list each agent's tools, and which are switched off
minus memory           # interactively prune stored facts
minus calibrate        # recompute the fact-relevance threshold
minus google-auth      # connect a Google account for the task/calendar tools
```

Against an assistant that is already running:

```bash
minus dash                    # the management dashboard
minus say "what time is it"   # inject a line, as though it had been spoken
minus status                  # what it is doing right now
minus status --watch          # ...and keep printing as that changes
```

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
what is marked. `t` lists each agent's tools -- `←`/`→` moves between the
conversational and deep-think agents (and the coding agent, which has none
yet), and `space` switches the tool under the cursor on or off for that agent
alone. A tool switched off is no longer offered to that model, and the switch
is written to `.env` as `MINUS_DISABLED_TOOLS`, so it survives a restart. `g`
lists every field of `config.py`, changes the ones that can be changed live,
and says why the rest cannot.

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
uv run pytest             # 413 tests, no audio or torch needed
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

- [ ] Create calendar events and tasks
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
