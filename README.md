# MINUS

Personal AI Assistant + Home Integration

## What is Minus?

**MINUS** is a personal AI assistant. It is (for now) designed for my personal use. The core gimmick I'm aiming for is to make it like Jarvis from Iron Man. I want to be able to talk outloud and then have minus intelligently take action to assist me. If my hardware allows, I want it to run locally on my server.

## Current Status

A work in progress. Minus is a custom harness around an LLM (via OpenRouter) with
persistent semantic memory, speech in and speech out.

## Install

```bash
uv sync --extra dev                              # core + test tooling
uv sync --extra dev --extra audio --extra embeddings   # everything, incl. mic/TTS
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

## Architecture

```
src/minus/
├── cli.py          composition root — the only place that picks implementations
├── config.py       every tunable value, env-overridable
├── paths.py        the single definition of where data lives
├── core/           protocols, typed messages, prompts, the agent loop
├── llm/            OpenRouter client + malformed-tool-call retry
├── tools/          @tool registry, schema derivation, built-in tools
├── memory/         transcripts, condensation, fact extraction, fact store
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

### Semantic Memory

Minus remembers facts and preferences between sessions:

- Every user message is appended with potentially relevant facts, ranked by
  comparing the embedding of the message against each fact.
- When a conversation ends it is condensed, and the LLM extracts durable facts
  from the transcript.
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
- [ ] Build a dedicated MINUS dashboard screen

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
