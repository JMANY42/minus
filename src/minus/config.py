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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
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
        # provider call. It also runs the field validators below, so a bad
        # timezone is refused on a live write as it is at startup.
        validate_assignment=True,
    )

    # ---- LLM ----
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY", repr=False)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    app_title: str = "Minus"

    # Any OpenAI-compatible provider can be pointed at here, which is what makes
    # a direct DeepSeek key usable: set MINUS_LLM_BASE_URL=https://api.deepseek.com/v1
    # and MINUS_LLM_API_KEY=sk-... and the OpenRouter fields above are ignored.
    llm_base_url: str = ""
    # repr=False on the credentials here and below: a traceback, a log line
    # that formats settings, or a `print(settings)` in a debugging session must
    # not be the thing that writes a key to disk. The values stay reachable for
    # the code that needs them; only the printed form omits them.
    llm_api_key: str = Field(default="", repr=False)

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
    google_client_secret: str = Field(default="", repr=False)
    google_refresh_token: str = Field(default="", repr=False)
    # Which list "add milk" means, and which calendar "put it in for Tuesday"
    # means. Empty is not a default -- it means MINUS asks, unless the account
    # has only one to choose from. Naming one here is how you stop being asked
    # every time without MINUS ever guessing. See tools/google_shared.py.
    google_tasks_list: str = ""
    google_calendar: str = ""

    # ---- Triage: qBittorrent in, verdict out ----
    # ---- VPN (AirVPN) ----
    # MINUS can bring the tunnel up itself. AirVPN's own suite is the only
    # client: bluetit is the daemon and goldcrest is what talks to it, so there
    # is no second VPN client or config file. Empty `vpn_interface` is
    # discovered from the route after a successful connect rather than guessed.
    vpn_provider: str = "airvpn"
    vpn_client_command: str = "goldcrest"
    vpn_interface: str = ""
    vpn_autoconnect: bool = True
    vpn_connect_timeout: float = 120.0
    # On Arch the current login session does not pick up a freshly added
    # `airvpn` group until the user logs in again. Falling back to a
    # passwordless `sudo -n` (one sudoers line) makes the harness work in that
    # window; direct invocation is always tried first.
    vpn_use_sudo_fallback: bool = True
    airvpn_username: str = ""
    airvpn_password: str = Field(default="", repr=False)
    # A server name or a country/continent ISO code. Empty asks AirVPN to pick
    # the best-scoring server, which is the least surprising default.
    airvpn_server: str = ""
    airvpn_country: str = ""
    airvpn_vpn_type: str = "wireguard"
    airvpn_network_lock: bool = True
    require_vpn_for_triage: bool = True

    # Empty means the triage tools are not registered at all: an unconfigured
    # tool is a round trip spent discovering it cannot work. Set the Web UI URL
    # (e.g. http://127.0.0.1:8080) to turn them on.
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = Field(default="", repr=False)
    # The WebUI the harness starts when nothing is listening. 127.0.0.1 is not
    # configurable: a torrent client's WebUI on a public interface is a remote
    # code execution surface, not a convenience.
    qbittorrent_webui_port: int = 8080
    qbittorrent_command: str = "qbittorrent-nox"
    # qBittorrent performs the download, so per-user routing cannot be proven
    # from inside MINUS. This names the device qBittorrent must bind to
    # (its Network Interface setting); empty leaves the check off.
    qbittorrent_vpn_interface: str = ""
    qbittorrent_incoming_dir: Path = Field(default_factory=paths.quarantine_incoming_dir)
    qbittorrent_listen_port: int = 0
    triage_quarantine_dir: Path = Field(default_factory=paths.quarantine_dir)
    triage_library_dir: Path = Field(default_factory=paths.library_dir)
    # Deliberately generous but finite: a 4K remux is ~50 GiB, and a pipeline
    # that will happily copy an unbounded amount is a foot-gun on /home.
    triage_max_bytes: int = 5 * 1024**3
    # A `clean` verdict requires a detonation. The dry-run runner never provides
    # one, so leaving this on means "static-only is never clean" -- which is the
    # honest default.
    triage_require_detonation: bool = True
    # A malicious verdict deletes the staged payload at once; the verdict record
    # (hash, reasons, file names) is kept as evidence, never the bytes.
    triage_delete_malicious: bool = True
    # After a successful promotion or a malicious rejection, ask qBittorrent to
    # delete the torrent and the files it downloaded: the bytes are either in
    # the library or the hazard, and neither needs a second copy. needs_review
    # keeps the download so a human can look at it. A cleanup failure is logged
    # and reported, never allowed to turn a release into a failure.
    triage_auto_cleanup: bool = True
    # add -> wait for completion -> stage -> scan, as one tool call. The wait is
    # bounded: a torrent that never finishes is reported, not retried forever.
    triage_auto_flow: bool = True
    triage_download_timeout: float = 3600.0
    triage_poll_seconds: float = 5.0
    # The agent reads code-shaped files before anything is released. This is
    # advisory to the deterministic verdict -- it can escalate, never clear --
    # but a malicious read deletes the payload, which is what turns "quarantined
    # until checked" into "quarantined until the model has looked at it".
    triage_model_review: bool = True
    triage_model_review_max_bytes: int = 256 * 1024
    # Empty uses the conversational model; naming a cheaper or stronger one is
    # allowed without touching code.
    triage_model_review_model: str = ""

    # ---- Torrent discovery ----
    # A query in download mode is resolved by cheap meta-search APIs, ranked
    # locally, and then handed to the same quarantine pipeline as a magnet the
    # user typed. Ranking is a pre-filter, never a trust boundary: the verdict
    # rules still decide what leaves quarantine.
    discovery_enabled: bool = True
    # Comma-separated names. `prowlarr` is inserted automatically when its URL
    # and key are set, because that is the only way to cover indexers the
    # built-in APIs do not expose.
    discovery_providers: str = "knaben,bitsearch,apibay,nyaa"
    # Only queried when the primary APIs produce nothing eligible. Kept
    # separate so a legal archive fallback does not pad every result list.
    discovery_fallback_providers: str = "internetarchive"
    # Below this score the harness reports the shortlist instead of downloading
    # it. The default is calibrated so a seeded 1080p release clears it and a
    # CAM or an unknown tiny file does not.
    discovery_min_score: float = 55.0
    # Results fetched per provider; the harness shows at most a handful.
    discovery_limit: int = 20
    discovery_timeout: float = 12.0
    # Prefer 1080p over 4K: the smaller file is usually the right trade for a
    # bot that still has to copy it into quarantine and scan it.
    discovery_preferred_quality: str = "1080p"
    # Only a verdict of `clean` is ever promoted. With no detonation sandbox a
    # static-only result is `needs_review`, so this stays a no-op in practice
    # until one is configured.
    discovery_auto_promote: bool = True
    # Optional Prowlarr instance. It carries the user's indexer definitions,
    # including private trackers, so MINUS does not have to scrape them.
    prowlarr_url: str = ""
    prowlarr_api_key: str = Field(default="", repr=False)
    # Jackett exposes every configured indexer through one Torznab endpoint.
    # This is the supported route to Cloudflare-fronted sites such as 1337x and
    # YourBittorrent; MINUS does not maintain HTML scrapers for them.
    jackett_url: str = ""
    jackett_api_key: str = Field(default="", repr=False)
    # Empty means the `all` indexer. A comma list targets specific definitions.
    jackett_indexers: str = ""

    # ---- Windows detonation sandbox ----
    # An immutable qcow2 base image plus a per-run overlay. Everything empty by
    # default: the sandbox is off until a base image exists and pywinrm is
    # installed (`uv sync --extra windows`, `minus vm-image`).
    windows_qemu: str = "qemu-system-x86_64"
    windows_base_image: str = ""
    windows_work_dir: str = ""
    windows_winrm_user: str = "minus"
    windows_winrm_password: str = Field(default="", repr=False)
    windows_cpus: int = 4
    windows_memory_mb: int = 6144
    windows_boot_timeout: float = 420.0
    windows_guest_timeout: float = 180.0
    windows_max_artifact_bytes: int = 64 * 1024**2
    windows_min_free_bytes: int = 20 * 1024**3
    windows_iso: str = ""
    windows_virtio_iso: str = ""
    windows_sysmon_zip: str = ""

    # ---- Alarm ----
    # The CLI the `set_alarm` tool runs: a full path, or a bare name looked up
    # on PATH. Defaults to where `cargo install` puts it -- see paths.py for
    # why a bare `alarm` is not the default.
    alarm_command: str = Field(default_factory=lambda: str(paths.alarm_command()))

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
    # An IANA Area/Location name. The user's own zone: what "7am" means to the
    # alarm and the Google tools when nobody names a place.
    timezone: str = "America/Chicago"
    log_level: str = "DEBUG"
    console_log_level: str = "INFO"
    # Runs kept in logs/ before the oldest are pruned. Previously unbounded.
    log_retention: int = 30

    # Lua plugins under plugins/. Off by default: lupa runs Lua in-process with
    # no instruction or memory limit, so a plugin directory is code you would
    # run. Turn it on deliberately (MINUS_PLUGINS_ENABLED=true).
    plugins_enabled: bool = False

    @field_validator("timezone")
    @classmethod
    def _iana_timezone(cls, value: str) -> str:
        """Refuse a zone the tz database does not know, e.g. "Americas/Dallas".

        Checked here rather than where it is used, because the places that use
        it hand it on -- to Google, to the alarm CLI -- and each would fail in
        its own way, long after startup, in the middle of a request.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{value!r} is not an IANA timezone; use Area/Location, e.g. America/Chicago"
            ) from exc
        return value

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
