"""Live configuration: what applies now, what needs a restart, what is refused.

Built against the real `build_config_controller` table wherever possible --
the value of that table is that it names where each setting actually lives, and
a test with its own invented table would prove nothing about it.
"""

from __future__ import annotations

from queue import Queue

import pytest

from minus.assembly import build_config_controller
from minus.config import Settings
from minus.control.config_control import BLOCKED, SECRETS, ConfigController, LiveField
from minus.core.sources import MergedTranscriptSource
from minus.runtime import Assistant


class FakeSpeaker:
    def __init__(self) -> None:
        self.voice = "am_puck"
        self.speed = 1.0
        self.lang = "en-us"
        self.chunk_max_chars = 300
        self.first_chunk_max_chars = 60


class FakeThinker:
    def __init__(self) -> None:
        self.deep_model = "deepseek/deepseek-v4-flash-0731:nitro"
        self.reasoning_effort = "high"
        self.max_tool_rounds = 4
        self.timeout_seconds = 120.0


class FakeMemory:
    def __init__(self) -> None:
        self.relevance_threshold = 0.356
        self.fact_search_top_k = 5
        self.extraction_model_name = "openai/gpt-oss-20b:nitro"


class FakeConversation:
    def __init__(self) -> None:
        self.max_tool_rounds = 7
        self.fact_top_k = 5


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
    settings = Settings()
    speaker = FakeSpeaker()
    source = MergedTranscriptSource(None, idle_timeout=30.0)
    assistant = Assistant(
        conversation=FakeConversation(),
        memory=FakeMemory(),
        thinker=FakeThinker(),
        results=Queue(),
        details=None,
    )
    controller = build_config_controller(assistant, speaker, source, settings)
    return controller, assistant, speaker, source, settings


class TestLiveApplication:
    def test_the_deep_model_reaches_the_thinker(self, wired):
        controller, assistant, _, _, settings = wired

        result = controller.apply({"deep_model": "anthropic/claude-opus-5"}, persist=False)

        assert result["applied"] == ["deep_model"]
        assert assistant.thinker.deep_model == "anthropic/claude-opus-5"
        assert settings.deep_model == "anthropic/claude-opus-5"

    def test_the_chat_model_is_read_from_settings_per_call(self, wired):
        """OpenRouterClient holds the Settings object, so this is the whole fix."""
        controller, _, _, _, settings = wired

        controller.apply({"chat_model": "openai/gpt-4o"}, persist=False)

        assert settings.chat_model == "openai/gpt-4o"

    def test_the_voice_reaches_the_speaker(self, wired):
        controller, _, speaker, _, _ = wired

        controller.apply({"tts_voice": "af_bella", "tts_speed": 1.25}, persist=False)

        assert speaker.voice == "af_bella"
        assert speaker.speed == 1.25

    def test_the_idle_timeout_reaches_the_transcript_source(self, wired):
        controller, _, _, source, _ = wired

        controller.apply({"idle_conversation_seconds": 90}, persist=False)

        assert source.idle_timeout == 90.0

    def test_fact_search_top_k_writes_both_copies(self, wired):
        """The value lives twice; setting one leaves the change half-applied."""
        controller, assistant, _, _, _ = wired

        controller.apply({"fact_search_top_k": 9}, persist=False)

        assert assistant.memory.fact_search_top_k == 9
        assert assistant.conversation.fact_top_k == 9

    def test_values_are_coerced_the_way_the_environment_would(self, wired):
        controller, assistant, _, _, _ = wired

        controller.apply({"deep_timeout_seconds": "45"}, persist=False)

        assert assistant.thinker.timeout_seconds == 45.0


class TestRestartRequired:
    def test_the_stt_model_cannot_apply_live(self, wired):
        """Whisper weights are loaded once, into a recorder built at startup."""
        controller, _, _, _, _ = wired

        result = controller.apply({"stt_model": "medium.en"}, persist=False)

        assert result["restart_required"] == ["stt_model"]
        assert result["applied"] == []

    def test_it_is_still_persisted_so_the_restart_picks_it_up(self, wired, tmp_path):
        controller, _, _, _, _ = wired

        result = controller.apply({"stt_model": "medium.en"})

        assert result["persisted"] == ["MINUS_STT_MODEL"]
        assert "MINUS_STT_MODEL=medium.en" in (tmp_path / ".env").read_text(encoding="utf-8")

    def test_the_field_list_separates_the_two(self, wired):
        controller, _, _, _, _ = wired

        described = controller.describe()

        assert "deep_model" in described["live"]
        assert "stt_model" in described["restart_required"]


class TestRefusals:
    @pytest.mark.parametrize("name", sorted(BLOCKED))
    def test_embedding_settings_are_blocked_outright(self, wired, name):
        """A restart does not un-corrupt a vector store, so 'restart' is a lie."""
        controller, _, _, _, _ = wired

        result = controller.apply({name: "something-else"}, persist=False)

        assert name in result["rejected"]
        assert result["applied"] == []

    def test_the_api_key_cannot_be_set(self, wired):
        controller, _, _, _, _ = wired

        result = controller.apply({"openrouter_api_key": "sk-stolen"}, persist=False)

        assert "openrouter_api_key" in result["rejected"]

    def test_the_api_key_is_never_reported(self, wired):
        controller, _, _, _, _ = wired

        described = controller.describe()

        assert "openrouter_api_key" not in described["live"]
        assert "openrouter_api_key" not in described["restart_required"]
        assert "openrouter_api_key" not in described["values"]
        assert described["secrets"] == sorted(SECRETS)

    def test_an_unknown_setting_is_refused(self, wired):
        controller, _, _, _, _ = wired

        result = controller.apply({"favourite_colour": "blue"}, persist=False)

        assert "favourite_colour" in result["rejected"]

    def test_a_bad_value_is_refused_rather_than_applied(self, wired):
        controller, assistant, _, _, _ = wired

        result = controller.apply({"deep_timeout_seconds": "not a number"}, persist=False)

        assert "deep_timeout_seconds" in result["rejected"]
        assert assistant.thinker.timeout_seconds == 120.0

    def test_one_bad_value_does_not_block_the_others(self, wired):
        controller, assistant, _, _, _ = wired

        result = controller.apply(
            {"deep_model": "anthropic/claude-opus-5", "embedding_dim": 512}, persist=False
        )

        assert result["applied"] == ["deep_model"]
        assert "embedding_dim" in result["rejected"]
        assert assistant.thinker.deep_model == "anthropic/claude-opus-5"


class TestDescribingEverything:
    """`values` is what the dashboard's management panel is drawn from."""

    def test_every_field_but_the_secret_carries_its_value(self, wired):
        controller, _, _, _, _ = wired

        described = controller.describe()

        assert set(described["values"]) == set(Settings.model_fields) - SECRETS

    def test_a_refused_field_still_reports_its_value(self, wired):
        """Its own group carries the reason and no value; a reader wants both."""
        controller, _, _, _, settings = wired

        described = controller.describe()

        assert described["values"]["embedding_dim"] == settings.embedding_dim
        assert described["values"]["console_log_level"] == settings.console_log_level

    def test_the_order_is_the_order_config_py_declares(self, wired):
        """Which is what groups the panel's rows the way the file reads."""
        controller, _, _, _, _ = wired

        described = controller.describe()

        assert list(described["values"]) == [
            name for name in Settings.model_fields if name not in SECRETS
        ]


class TestPersistence:
    def test_a_live_change_also_survives_a_restart(self, wired, tmp_path):
        controller, _, _, _, _ = wired

        controller.apply({"tts_voice": "af_bella"})

        assert "MINUS_TTS_VOICE=af_bella" in (tmp_path / ".env").read_text(encoding="utf-8")

    def test_persist_false_leaves_the_file_alone(self, wired, tmp_path):
        controller, _, _, _, _ = wired

        controller.apply({"tts_voice": "af_bella"}, persist=False)

        assert not (tmp_path / ".env").exists()

    def test_the_env_name_is_prefixed(self):
        assert ConfigController.env_name("deep_model") == "MINUS_DEEP_MODEL"


class TestTableHonesty:
    def test_every_live_field_is_a_real_setting(self, wired):
        """A typo here would silently mean 'restart required' forever."""
        controller, _, _, _, _ = wired

        unknown = set(controller.live) - set(Settings.model_fields)

        assert unknown == set()

    def test_a_field_is_live_only_if_it_is_in_the_table(self, wired):
        controller, _, _, _, _ = wired
        controller.live.pop("deep_model")

        result = controller.apply({"deep_model": "x/y"}, persist=False)

        assert result["restart_required"] == ["deep_model"]

    def test_the_table_entries_are_frozen(self, wired):
        controller, _, _, _, _ = wired

        with pytest.raises(AttributeError):
            controller.live["deep_model"].name = "other"

    def test_a_live_field_carries_its_own_name(self, wired):
        controller, _, _, _, _ = wired

        assert all(name == field.name for name, field in controller.live.items())
        assert isinstance(controller.live["deep_model"], LiveField)
