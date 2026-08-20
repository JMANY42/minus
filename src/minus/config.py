"""Runtime configuration for MINUS.

Every tunable value the assistant depends on lives here rather than as a
module-level constant next to the code that happens to use it. That matters
for two reasons beyond tidiness:

  * The values were previously unreachable without editing source. Model
    choice, the relevance threshold, TTS voice and chunk size were all
    hard-coded literals scattered across five modules.
  * Nothing could be varied per-run, so tests had to monkeypatch module
    globals to exercise alternate behaviour.

Settings are read from the environment (and a .env file) with a `MINUS_`
prefix, so `MINUS_CHAT_MODEL=openai/gpt-4o` overrides `chat_model` without a
code change. Fields that name a filesystem location default to `paths.py`
rather than duplicating the layout knowledge.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from minus import paths

# Resolved once, at import, rather than left relative. `.env` alone is relative
# to the working directory, which is fine when you launch from the repo and
# silently wrong under a systemd unit or from a subdirectory: the API key
# vanishes and every call comes back 401. The CWD file is kept second so that
# it still wins where one exists, which preserves today's behaviour.
_PROJECT_ENV_FILE = paths.project_root() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MINUS_",
        env_file=(_PROJECT_ENV_FILE, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # So the dashboard can change a value on a live object and have it
        # rejected at the setter rather than three seconds later inside a
        # provider call. No field has a custom validator, so this costs
        # nothing today and makes `settings.chat_model = 12` an error.
        validate_assignment=True,
    )

    # ---- LLM ----
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    app_title: str = "Minus"

    # Two tiers. `chat_model` is chosen for latency: it carries the whole
    # conversation and must answer "what time is it?" fast enough to feel
    # spoken. It is not good at sustained reasoning and is not asked to be --
    # when it hits something beyond it, it calls the `escalate` tool and the
    # deep tier answers on a background thread. See core/escalation.py.
    chat_model: str = "openai/gpt-oss-20b:nitro"
    fact_extraction_model: str = "openai/gpt-oss-20b:nitro"

    # DeepSeek V4 Flash is a sparse MoE that reasons on demand rather than by
    # being large, so its depth comes from `deep_reasoning_effort` below, not
    # from the model name. Dropping the effort parameter would make this tier
    # barely deeper than the fast one. anthropic/claude-opus-5 is the step up
    # if this proves too weak.
    deep_model: str = "deepseek/deepseek-v4-flash-0731:nitro"
    deep_reasoning_effort: str = "high"
    # Deliberately lower than max_tool_rounds: the deep tier reads a few files
    # to ground an answer, it does not go exploring unattended.
    deep_max_tool_rounds: int = 4
    # A worker thread cannot be killed, so this does not abort a wedged deep
    # call. It bounds how long one blocks the *next* escalation before the
    # thinker gives up waiting on it and accepts new work.
    deep_timeout_seconds: float = 120.0

    max_retries: int = 3
    max_tool_rounds: int = 7

    # Which tools are switched off, and for which tier: a comma-separated list
    # of `agent:tool` pairs, e.g. "conversational:escalate,deep:read_workspace_file".
    # Written by the dashboard's tools panel rather than by hand, and only the
    # switched-off ones are named -- so a tool a later build adds arrives
    # switched on rather than missing from a list of everything that was.
    disabled_tools: str = ""

    # ---- Google Tasks ----
    # The durable half of an OAuth grant. `minus google-auth` performs the
    # consent round trip once and writes these three; the hour-long access
    # token they buy is held in memory and never stored. Absent, the five
    # task tools are simply not registered -- see assembly.build_google_tools.
    #
    # MINUS_-prefixed like every other field here rather than bare
    # GOOGLE_CLIENT_ID: these are this assistant's grant, and a machine that
    # already exports Google credentials for something else should not have
    # them silently adopted.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    # Which list "add milk" means, and which calendar "put it in for Tuesday"
    # means. Empty is not a default -- it means MINUS asks, unless the account
    # has only one to choose from. Naming one here is how you stop being asked
    # every time without MINUS ever guessing. See tools/google_shared.py.
    google_tasks_list: str = ""
    google_calendar: str = ""

    # ---- Conversation lifetime ----
    # How long a silence ends the conversation. On the timeout MINUS condenses
    # the transcript and extracts durable facts, then starts a fresh
    # conversation. Without it, that work happens only when the process exits,
    # so an assistant left running would never learn anything -- and the longer
    # it ran, the more of one conversation it would try to condense at once.
    # Set to 0 to disable the rollover and go back to per-process conversations.
    idle_conversation_seconds: float = 30.0

    # ---- Semantic memory ----
    # Calibrated by `minus calibrate`: the midpoint between the direct-match
    # and related-topic similarity distributions. See scripts/calibrate.py for
    # how to re-derive it if the embedding model changes.
    relevance_threshold: float = 0.356
    fact_search_top_k: int = 5
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # ---- Speech ----
    tts_voice: str = "am_puck"
    tts_speed: float = 1.0
    tts_lang: str = "en-us"
    # A speech-quality knob, not a latency one: every chunk is a separate
    # synthesis call that Kokoro renders as a complete utterance, so seams are
    # audible and want to be rare and grammatical. Barge-in latency is bounded
    # by the playback block size instead -- see audio/tts.py. The first chunk
    # is held shorter because nothing is heard until it is synthesized.
    tts_chunk_max_chars: int = 300
    tts_first_chunk_max_chars: int = 60

    stt_model: str = "small.en"
    stt_realtime_model: str = "tiny.en"
    stt_device: str = "cuda"

    # ---- Misc ----
    timezone: str = "America/Chicago"
    log_level: str = "DEBUG"
    console_log_level: str = "INFO"
    # Runs kept in logs/ before the oldest are pruned. Previously unbounded.
    log_retention: int = 30

    @property
    def project_root(self) -> Path:
        return paths.project_root()

    @property
    def semantic_memory_db(self) -> Path:
        return paths.semantic_memory_db()

    @property
    def conversations_dir(self) -> Path:
        return paths.conversations_dir()

    @property
    def condensed_conversations_dir(self) -> Path:
        return paths.condensed_conversations_dir()


def load_settings(**overrides: object) -> Settings:
    """Build settings from the environment, with explicit overrides on top.

    The composition root calls this once and threads the result through; no
    module should reach for a global settings singleton.
    """
    return Settings(**overrides)  # type: ignore[arg-type]
