# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

MINUS is a voice assistant: a custom harness around an LLM (OpenRouter) with persistent
semantic memory, speech in and speech out, a control socket, and a Textual dashboard.
`README.md` documents it from a user's side — install, dashboard keys, systemd. This file
covers what you need to change the code.

## Commands

The venv is already built. Use it directly rather than `uv run` (which re-resolves and is
slower here):

```bash
.venv/bin/python -m pytest                      # whole suite (~770 tests, seconds)
.venv/bin/python -m pytest tests/test_agent.py  # one file
.venv/bin/python -m pytest tests/test_agent.py::test_name
.venv/bin/python -m pytest -k escalat           # by name
.venv/bin/python -m ruff check <paths you touched>
.venv/bin/python -m ruff format <paths you touched>
.venv/bin/python -m mypy src
```

Go through `.venv/bin/python -m` rather than the console scripts: this venv was created at
an older path, so `.venv/bin/mypy` and friends have a stale shebang and fail with "No such
file or directory".

Neither linter is green on a clean checkout, so don't read a pre-existing failure as
something you broke — check that your files are the ones reporting:

- `ruff check .` reports 48 E501/W291 findings, all of them in the demo story literal in
  `src/minus/audio/tts.py`'s `if __name__ == "__main__"` block. Lint the paths you touched.
- `mypy src` reports 4 errors, all in `dashboard/` against Textual's stubs.

`uv sync --extra audio --extra embeddings --extra dashboard` installs the optional stacks
(all three are already installed in this checkout). Tests that need one guard with
`pytest.importorskip` at module top (`textual`, `sounddevice`/`kokoro_onnx`, `numpy`), so
the suite passes on a bare core install — keep that guard when adding such a test.

Running it: `minus` (mic), `minus --no-mic` (typed), `minus serve` (headless/systemd),
`minus dash`, `minus say`, `minus status`, `minus tools`, `minus memory`, `minus calibrate`,
`minus google-auth`.

`.env` holds `OPENROUTER_API_KEY`, the three `MINUS_GOOGLE_*` credentials `minus
google-auth` writes, and any `MINUS_*` overrides; reading it is denied to
Claude by `.claude/settings.json`. `logs/` and `memory/` are runtime data, gitignored, and
anchored with a leading slash so they never shadow `src/minus/memory/` (which is code).

## Layering

Three top-level modules, and dependencies only point downwards:

- `cli.py` — argparse, one function per subcommand. Imports audio and textual lazily so
  `minus memory` works on a box with no PortAudio.
- `assembly.py` — the composition root. **The only module that picks implementations.**
  Building the object graph, the live-config table, and the control-command table all
  happen here.
- `runtime.py` — behaviour: the conversation loop, the deep-answer courier thread, the idle
  rollover, the SIGTERM handler. Constructs nothing; receives everything.

That split is why tests drive whole conversations with fakes and no entry point. Preserve
it: if you find yourself instantiating a client or a store outside `assembly.py`, that is
the smell.

Seams are `typing.Protocol` in `core/protocols.py` — `ChatModel`, `TranscriptSource`,
`SpeechSynthesizer`, `DetailSink`, `FactStore`, `Embedder`. Swapping a provider is an edit
to `assembly.py` and nothing else. `tests/fakes.py` has a fake for each.

## Two model tiers

`chat_model` is chosen for latency and carries every conversation. When it hits something
that needs real reasoning it calls the `escalate` tool (`core/escalation.py`), which returns
immediately and runs `deep_model` on a background thread. The answer arrives on a `Queue`,
not as a return value, and is delivered by the courier in `runtime.py`. It has two channels:
a short spoken line, and a full write-up that goes to a `DetailSink` (files under
`memory/deep_notes/`) and is never read aloud. The tier's depth comes from
`deep_reasoning_effort`, not model size — dropping that parameter makes the tier pointless.

`core/loop.py::ToolLoop` is the single tool-calling loop, shared by `Conversation` and
`DeepThinker`. Tier differences are constructor arguments (registry, max rounds, reasoning
effort), not separate loops.

Construction order matters: `Conversation` needs the registry holding `escalate`, and
`escalate` needs the conversation for context. `assembly.build_conversation` breaks the
cycle by building both and then calling `thinker.bind_snapshot(...)`.

## Threading

Four things share one speaker and one transcript: the conversation loop, the courier
thread, the idle rollover timer, and the control socket. `floor` — a single
`threading.Lock` built by the caller and passed to all of them — is what serializes them.
Anything that touches the transcript or speaks holds it.

Interrupt tokens: `speaker.token()` is captured *before* generation starts, and a reply
whose token has gone stale (the user started talking again) is dropped rather than spoken
over them. The courier captures its token at delivery time, not at escalation time —
capturing early would silently drop every deep answer.

Signal handlers only install on the main thread (CPython's rule), so a `SpeechSynthesizer`
must never install one; barge-in is routed through `audio/interrupt.py::InterruptBus`.

## Tools

A tool is one decorated function; the JSON schema is derived from its signature and
Google-style docstring (`tools/schema.py`), so there is no second place to keep in sync.

```python
@registry.tool
def set_light(room: str, brightness: int = 100) -> dict:
    """Set a room's light brightness.

    Args:
        room: Room name, e.g. "office".
    """
```

Write it under `tools/builtin/`, then **import it in `tools/__init__.py`** — registration is
an import side effect. The fast tier gets everything registered plus `escalate`; the deep
tier gets an explicit allowlist (`assembly.DEEP_TOOL_NAMES`), because a background job
should not inherit tools chosen for a user who is listening.

A tool holding a collaborator cannot be registered at import, so it is built in `assembly.py`
and attached to the tier's registry there instead: `escalate` needs the thinker, and the ten
Google tools need the OAuth credentials only `assembly` may read. `build_google_tools` returns
an empty list when `.env` has none, so an unconnected account leaves the model with fewer tools
rather than tools that fail — and `build_system_prompt(can_schedule=...)` is threaded off the
same fact, so the standing scheduling rules are absent rather than talking about a calendar
nobody connected.

`tools/policy.py::ToolPolicy` groups the per-tier registries under display names for the
dashboard and turns individual tools on and off. Switches persist as
`MINUS_DISABLED_TOOLS="conversational:escalate,deep:read_workspace_file"` in `.env` — only
the *disabled* ones are named, so a tool added by a later build arrives switched on.

Filesystem tools must route through `tools/workspace.py::resolve_workspace_path`, which
resolves before checking containment so symlinks out of the workspace are rejected.

## Google tasks and calendar

Two APIs, one grant. `services/google.py::GoogleCredentials` holds the refresh token and the
access token it buys; `services/google_tasks.py` and `services/google_calendar.py` are thin
REST wrappers over it, and `tools/google_tasks.py` / `tools/google_calendar.py` hold everything
model-facing. Scopes are requested together in `minus google-auth` because they cannot be added
to a grant afterwards — a tasks-only token stays one, which is why a 403 carrying
`ACCESS_TOKEN_SCOPE_INSUFFICIENT` is rewritten into "run `minus google-auth` again".

**The tools do not guess.** The fields the user owns — a task's title/due/list, an event's
title/start/end/all-day/location/calendar — are required parameters, so the model cannot omit
one and have a default chosen quietly. `tools/google_shared.py` holds the rule in one place and
both families use it: *ambiguous* raises `ClarificationNeeded` (only the user can settle it),
*not found* raises a plain `ToolArgumentError` carrying the real names (retrying cannot help),
and *unambiguous* — one calendar, or a name in `.env` — is used without asking, because there
was nothing to choose. `core/loop.py::tool_failure_message` is what makes it work end to end:
a `ClarificationNeeded` becomes "ask the user this and do not call the tool again", where every
other tool failure becomes "retry with valid arguments".

Two conventions worth knowing before editing these. The word `none` in an optional-value field
(a due date, a location) means "the user said there isn't one", as distinct from `""`, which is
what an unsure model sends. And `add_google_event` takes `all_day` as a required bool *and*
checks it against whether `start`/`end` carry times: either half alone is exactly what a guess
looks like, so a contradiction is a question rather than resolved in favour of one of them.

## Memory

`memory/service.py::MemoryService` is the facade: the running transcript on disk, the
condensation of a finished conversation, and fact extraction from that condensation. All
three collaborators are injectable and may be absent.

Facts are structured `(attribute, value)` slots in SQLite + sqlite-vec
(`memory/facts/store.py`). Dedupe and supersede are **exact matches on the normalized
attribute**, not similarity thresholds; similarity is only used for retrieval, gated by
`relevance_threshold` (re-derive with `minus calibrate` if the embedding model changes).
Single-valued attributes supersede, multi-valued accumulate. Known attributes are fed back
into the extraction prompt so the model reuses `preferred_language` rather than inventing
`programming_language`.

A conversation ends after `idle_conversation_seconds` of silence — measured from when MINUS
*stopped talking* — not when the process exits. `runtime.end_conversation_when_idle` may
decline (empty transcript, deep answer still in flight) and stay armed;
`end_conversation_now` is the deliberate twin and does not decline.

## Control socket and dashboard

A running assistant listens on `$XDG_RUNTIME_DIR/minus/control.sock` and it doubles as the
single-instance lock (`--no-control` to run a second one anyway). Wire format is
newline-delimited JSON, defined as pure functions in `control/protocol.py` with a closed set
of error codes; `control/server.py` is handed a dict of command handlers from
`assembly.build_control_handlers` and knows nothing about what any of them mean.

Live config works the same way: `assembly.build_config_controller` is an explicit table
naming every setting that can change on a running assistant and what each write touches
(often both the `Settings` object and the live object holding the value). A field absent
from that table is *not* live, and the dashboard says why rather than pretending.

`dashboard/` is the only package allowed to import textual, and `minus serve` never imports
it. The dashboard reads state from the files MINUS already writes (log, conversation JSON,
deep notes), so everything except the input box works with the assistant stopped. Its tests
drive the real widget tree through Textual's async pilot (`asyncio_mode = "auto"`).

The console pane exists because C extensions write to fd 1/2 directly and have never heard
of `logging`; `minus serve` redirects its own stdout/stderr into `logs/console-*.log`
before anything can write to them.

Restarting goes through `systemctl --user` (`dashboard/service.py`), not the socket — a
socket can ask a process to exit but cannot ask it to come back.

## Conventions

- **Comments explain why, not what.** Nearly every module opens with a docstring saying what
  problem its shape solves and what the previous shape got wrong. New code is expected to
  match that density; when you change a decision, update the paragraph that justified it.
- Every path comes from `paths.py`; every tunable from `config.py` (`MINUS_`-prefixed env
  overrides, `validate_assignment=True` so a bad live write fails at the setter). No module
  reaches for a global settings singleton — `assembly.py` threads one through.
- Errors raised deliberately derive from `MinusError` (`errors.py`), so callers can catch
  ours without catching the interpreter's. Don't raise bare `ValueError`/`RuntimeError`.
- Transcript messages are the dataclasses in `core/messages.py` with `to_wire()`, not
  hand-assembled dicts.
- ruff, line length 100, py312 target. `prompts.py` is E501-exempt because prompt text is
  wire content — rewrapping it changes what the model receives. mypy is deliberately
  non-strict.
- Conventional commits (`feat(dashboard):`, `fix(memory):`, `refactor:`). Leave changes
  uncommitted unless asked; the user commits.
