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
minus tools            # list the tools the assistant can call
minus memory           # interactively prune stored facts
minus calibrate        # recompute the fact-relevance threshold
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
├───────────────────────────────┤───────────────────────────────┤
│   input             (1/3)     │  EXTERNAL PROGRAMS        [p] │
│   > _                         ├───────────────────────────────┤
│                               │  AGENTS                   [a] │
└───────────────────────────────┴───────────────────────────────┘
```

`←`/`→` switches the viewer, `m h t p a` expands a panel, `i` focuses the
input, `escape` steps back out, `c` interrupts, `R` restarts the service, and
`q` quits **the dashboard, not MINUS**. `ctrl+←`/`ctrl+→` switch the view
without leaving the input box, which owns the bare arrow keys for its cursor.

Built for the console on the machine itself: sixteen ANSI colours, ASCII
borders and character meters, because the VT font has no block-drawing glyphs.
`--unicode` relaxes that over SSH.

It is a separate process and a separate dependency. `minus serve` never
imports textual. Reads come from the files MINUS already writes, so the log,
the conversation and the deep-think notes still render with the assistant
stopped -- only the input box needs the socket.

The expanded panels are scaffolding: each one says "nothing here yet", and
filling one in means returning a list from its `options()`.

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

## Architecture

```
src/minus/
├── cli.py          composition root — the only place that picks implementations
├── config.py       every tunable value, env-overridable
├── paths.py        the single definition of where data lives
├── core/           protocols, typed messages, prompts, agent loop, deep tier
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
backend or fact store is a change to `cli.py`.

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
uv run pytest             # 94 tests, no audio or torch needed
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
