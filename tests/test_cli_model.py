"""Choosing the model each tier uses.

The behaviour worth pinning is the split between "applied live" and "written for
the next start": both are successes, and a command that silently did one while
reporting the other is how a model change appears to work and then does not.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from minus.config import Settings
from minus.control.client import NotRunning
from minus.scripts import models


def _settings(**overrides) -> Settings:
    # `openrouter_api_key` has an env alias, so the kwarg must use it. The LLM
    # fields are pinned empty so an exported MINUS_LLM_* in the developer's shell
    # cannot decide what this test observes.
    values = {"OPENROUTER_API_KEY": "test-key", "llm_base_url": "", "llm_api_key": ""}
    values.update(overrides)
    return Settings(**values)


class FakeClient:
    """A ControlClient that records what it was asked and answers politely."""

    def __init__(self, socket_path, *, result=None):
        self.socket_path = socket_path
        self.result = result or {"applied": ["chat_model"], "persisted": ["MINUS_CHAT_MODEL"]}
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, command, **params):
        self.calls.append((command, params))
        return dict(self.result)


class ResolveTests(unittest.TestCase):
    def test_maps_each_flag_to_its_setting(self):
        changes = models.resolve(_settings(), conversational=" a/b ", deep="c/d", extraction="e/f")

        self.assertEqual(
            changes,
            {
                "chat_model": "a/b",
                "deep_model": "c/d",
                "fact_extraction_model": "e/f",
            },
        )

    def test_refuses_an_empty_name(self):
        with self.assertRaises(ValueError) as caught:
            models.resolve(_settings(), conversational="   ")
        self.assertIn("conversational", str(caught.exception))

    def test_refuses_an_empty_request(self):
        with self.assertRaises(ValueError):
            models.resolve(_settings())

    def test_names_the_tier_not_the_field(self):
        changes = models.resolve(_settings(), deep="x")
        self.assertEqual(list(changes), ["deep_model"])


class ApplyTests(unittest.TestCase):
    def test_live_path_sends_the_change_and_reports_it(self):
        called: list[str] = []

        def connect(path):
            called.append(str(path))
            return FakeClient(path)

        with tempfile.TemporaryDirectory() as temp_dir:
            result = models.apply(
                {"chat_model": "new/model"},
                _settings(),
                connect=connect,
                socket_path=Path("/tmp/minus-test.sock"),
                env_path=Path(temp_dir) / ".env",
            )

        self.assertTrue(result["live"])
        self.assertEqual(result["applied"], ["chat_model"])
        self.assertEqual(result["persisted"], ["MINUS_CHAT_MODEL"])
        self.assertEqual(called, ["/tmp/minus-test.sock"])
        self.assertFalse((Path(temp_dir) / ".env").exists())

    def test_stopped_assistant_writes_env_and_says_pending(self):
        def connect(path):
            raise NotRunning("nothing listening")

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("# keep me\nOPENROUTER_API_KEY=abc\n")

            result = models.apply(
                {"deep_model": "deep/model"},
                _settings(),
                connect=connect,
                socket_path=Path("/tmp/minus-test.sock"),
                env_path=env_path,
            )

            written = env_path.read_text()

        self.assertFalse(result["live"])
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["persisted"], ["MINUS_DEEP_MODEL"])
        self.assertEqual(result["restart_required"], ["deep_model"])
        self.assertIn("# keep me", written)
        self.assertIn("OPENROUTER_API_KEY=abc", written)
        self.assertIn("MINUS_DEEP_MODEL=deep/model", written)


class ProviderEndpointTests(unittest.TestCase):
    def test_a_direct_provider_overrides_the_openrouter_defaults(self):
        from minus.llm.client import OpenRouterClient

        settings = _settings(
            OPENROUTER_API_KEY="router-key",
            llm_base_url="https://api.deepseek.com/v1",
            llm_api_key="sk-deepseek",
        )
        client = OpenRouterClient(settings)

        self.assertTrue(str(client._client.base_url).startswith("https://api.deepseek.com"))
        self.assertEqual(client._client.api_key, "sk-deepseek")

    def test_without_an_override_the_openrouter_defaults_stand(self):
        from minus.llm.client import OpenRouterClient

        client = OpenRouterClient(_settings(OPENROUTER_API_KEY="router-key"))

        self.assertTrue(str(client._client.base_url).startswith("https://openrouter.ai"))


class CandidateTests(unittest.TestCase):
    def test_filters_on_id_and_name(self):
        catalogue = [
            {"id": "meta/llama-3.1-70b", "name": "Llama 3.1"},
            {"id": "openai/gpt-oss-20b", "name": "GPT OSS 20B"},
            {"id": "deepseek/v4", "name": "DeepSeek V4"},
        ]

        by_id = models.list_candidates(_settings(), "llama", fetch=lambda settings: catalogue)
        by_name = models.list_candidates(_settings(), "deepseek v4", fetch=lambda settings: catalogue)

        self.assertEqual([model["id"] for model in by_id], ["meta/llama-3.1-70b"])
        self.assertEqual([model["id"] for model in by_name], ["deepseek/v4"])

    def test_empty_filter_returns_the_catalogue(self):
        catalogue = [{"id": "a"}, {"id": "b"}]
        models_listed = models.list_candidates(_settings(), "", fetch=lambda settings: catalogue)
        self.assertEqual(len(models_listed), 2)
