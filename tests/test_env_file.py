"""Editing .env without losing what a person put there by hand."""

from __future__ import annotations

import os

import pytest

from minus.services.env_file import quote, update_env_file

ORIGINAL = """\
# The key. Do not commit this file.
OPENROUTER_API_KEY=sk-secret

# Chosen for latency, not for reasoning.
MINUS_CHAT_MODEL=openai/gpt-oss-20b:nitro

export MINUS_TTS_VOICE=am_puck
SOMETHING_ELSE=untouched
"""


@pytest.fixture
def env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(ORIGINAL, encoding="utf-8")
    return path


def read(path) -> str:
    return path.read_text(encoding="utf-8")


class TestPreservation:
    def test_updates_a_key_in_place(self, env):
        update_env_file(env, {"MINUS_CHAT_MODEL": "anthropic/claude-opus-5"})

        assert "MINUS_CHAT_MODEL=anthropic/claude-opus-5" in read(env)
        assert "MINUS_CHAT_MODEL=openai/gpt-oss-20b:nitro" not in read(env)

    def test_keeps_comments_and_blank_lines(self, env):
        update_env_file(env, {"MINUS_CHAT_MODEL": "x/y"})
        content = read(env)

        assert "# The key. Do not commit this file." in content
        assert "# Chosen for latency, not for reasoning." in content
        assert "\n\n" in content

    def test_leaves_unknown_keys_alone(self, env):
        update_env_file(env, {"MINUS_CHAT_MODEL": "x/y"})

        assert "SOMETHING_ELSE=untouched" in read(env)

    def test_does_not_disturb_the_api_key(self, env):
        update_env_file(env, {"MINUS_CHAT_MODEL": "x/y"})

        assert "OPENROUTER_API_KEY=sk-secret" in read(env)

    def test_handles_an_exported_assignment(self, env):
        update_env_file(env, {"MINUS_TTS_VOICE": "af_bella"})

        assert "export MINUS_TTS_VOICE=af_bella" in read(env)

    def test_keeps_the_original_ordering(self, env):
        update_env_file(env, {"MINUS_CHAT_MODEL": "x/y"})
        keys = [line.split("=")[0] for line in read(env).splitlines() if "=" in line]

        assert keys[:2] == ["OPENROUTER_API_KEY", "MINUS_CHAT_MODEL"]


class TestAppending:
    def test_adds_a_key_that_was_not_there(self, env):
        update_env_file(env, {"MINUS_DEEP_MODEL": "deepseek/deepseek-v4-flash-0731:nitro"})

        assert "MINUS_DEEP_MODEL=deepseek/deepseek-v4-flash-0731:nitro" in read(env)

    def test_a_second_write_updates_rather_than_appends_again(self, env):
        update_env_file(env, {"MINUS_DEEP_MODEL": "first/model"})
        update_env_file(env, {"MINUS_DEEP_MODEL": "second/model"})
        content = read(env)

        assert content.count("MINUS_DEEP_MODEL=") == 1
        assert "second/model" in content

    def test_the_header_is_written_once(self, env):
        update_env_file(env, {"MINUS_DEEP_MODEL": "a/b"})
        update_env_file(env, {"MINUS_LOG_LEVEL": "INFO"})

        assert read(env).count("written by the minus dashboard") == 1

    def test_creates_the_file_when_absent(self, tmp_path):
        path = tmp_path / "nested" / ".env"

        update_env_file(path, {"MINUS_LOG_LEVEL": "INFO"})

        assert "MINUS_LOG_LEVEL=INFO" in read(path)


class TestQuoting:
    @pytest.mark.parametrize("value", ["", "two words", "has#hash", "has$var", 'has"quote'])
    def test_values_needing_quotes_get_them(self, value):
        assert quote(value).startswith('"')

    @pytest.mark.parametrize("value", ["plain", "openai/gpt-oss-20b:nitro", "0.356", "am_puck"])
    def test_ordinary_values_are_left_bare(self, value):
        assert quote(value) == value

    def test_a_quoted_value_round_trips_through_dotenv(self, tmp_path):
        from dotenv import dotenv_values

        path = tmp_path / ".env"
        update_env_file(path, {"MINUS_APP_TITLE": 'My "Assistant" $HOME'})

        assert dotenv_values(path)["MINUS_APP_TITLE"] == 'My "Assistant" $HOME'


class TestSafety:
    def test_refuses_a_key_outside_the_allowlist(self, env):
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            update_env_file(
                env,
                {"OPENROUTER_API_KEY": "stolen"},
                allowed=frozenset({"MINUS_CHAT_MODEL"}),
            )

        assert "sk-secret" in read(env)

    def test_leaves_no_temporary_file_behind(self, env):
        update_env_file(env, {"MINUS_CHAT_MODEL": "x/y"})

        assert list(env.parent.glob(".*.tmp")) == []

    def test_a_new_file_is_not_world_readable(self, tmp_path):
        path = tmp_path / ".env"

        update_env_file(path, {"MINUS_LOG_LEVEL": "INFO"})

        assert path.stat().st_mode & 0o077 == 0

    def test_an_existing_files_mode_is_kept(self, env):
        os.chmod(env, 0o640)

        update_env_file(env, {"MINUS_CHAT_MODEL": "x/y"})

        assert env.stat().st_mode & 0o777 == 0o640
