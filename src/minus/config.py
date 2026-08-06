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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MINUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
    chat_model: str = "openai/gpt-oss-20b:free"
    fact_extraction_model: str = "openai/gpt-oss-20b:free"

    # DeepSeek V4 Flash is a sparse MoE that reasons on demand rather than by
    # being large, so its depth comes from `deep_reasoning_effort` below, not
    # from the model name. Dropping the effort parameter would make this tier
    # barely deeper than the fast one. anthropic/claude-opus-5 is the step up
    # if this proves too weak.
    deep_model: str = "deepseek/deepseek-v4-flash-0731"
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
    # Deliberately small. An interrupt is only honoured between chunks (the
    # playback stream is never aborted mid-chunk -- see audio/tts.py), so this
    # constant is what bounds barge-in latency.
    tts_chunk_max_chars: int = 40

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
