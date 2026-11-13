"""The read-only posture collector, against real Settings.

No stand-ins: the point of `minus doctor` is that it describes the settings the
assistant will actually use, so every test here builds a real `Settings` (from a
scrubbed environment) and inspects the real collector. Paths are pointed at
`tmp_path` through the environment, which is the same mechanism an operator
uses.
"""

from __future__ import annotations

import json

import pytest

from minus.config import Settings
from minus.doctor import collect_posture, format_posture, posture_json

# Everything that could otherwise be inherited from the developer's shell or a
# repository .env, and would make a test depend on the machine it runs on.
_SCRUBBED = (
    "OPENROUTER_API_KEY",
    "MINUS_LLM_API_KEY",
    "MINUS_LLM_BASE_URL",
    "MINUS_VPN_INTERFACE",
    "MINUS_QBITTORRENT_URL",
    "MINUS_REQUIRE_VPN_FOR_TRIAGE",
    "MINUS_WINDOWS_BASE_IMAGE",
    "MINUS_PLUGINS_ENABLED",
    "MINUS_TRIAGE_QUARANTINE_DIR",
    "MINUS_TRIAGE_LIBRARY_DIR",
)


@pytest.fixture
def scrubbed(monkeypatch, tmp_path):
    monkeypatch.setenv("MINUS_PROJECT_ROOT", str(tmp_path))
    for name in _SCRUBBED:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _posture(settings: Settings, root):
    return collect_posture(settings, socket_path=root / "run" / "control.sock")


def test_a_key_from_the_environment_is_named_never_printed(scrubbed, monkeypatch):
    fixture = "test-value-must-not-be-printed"
    monkeypatch.setenv("OPENROUTER_API_KEY", fixture)

    posture = _posture(Settings(), scrubbed)

    assert posture["provider"]["api_key"] == "environment"
    assert fixture not in format_posture(posture)
    assert fixture not in posture_json(posture)


def test_enforcement_with_no_interface_is_reported_as_fail_closed(scrubbed, monkeypatch):
    monkeypatch.setenv("MINUS_REQUIRE_VPN_FOR_TRIAGE", "true")

    settings = Settings(vpn_interface="", qbittorrent_url="")
    posture = _posture(settings, scrubbed)

    assert any("fails closed" in finding for finding in posture["dangerous"])


def test_a_world_writable_quarantine_directory_is_dangerous(scrubbed):
    quarantine = scrubbed / "quarantine"
    quarantine.mkdir()
    quarantine.chmod(0o777)

    settings = Settings(
        triage_quarantine_dir=quarantine,
        triage_library_dir=scrubbed / "library",
    )
    posture = _posture(settings, scrubbed)

    assert posture["storage"]["quarantine"]["exists"] is True
    assert posture["storage"]["quarantine"]["mode"] == "0777"
    assert any(
        "quarantine" in finding and "group or other" in finding
        for finding in posture["dangerous"]
    )


def test_json_output_round_trips(scrubbed):
    posture = _posture(Settings(), scrubbed)

    assert json.loads(posture_json(posture)) == posture


def test_provider_url_hides_userinfo_but_keeps_the_host(scrubbed):
    settings = Settings(llm_base_url="https://user:pw@example.test/v1")

    posture = _posture(settings, scrubbed)
    base_url = posture["provider"]["base_url"]

    assert "pw" not in base_url
    assert "example.test" in base_url
    assert "/v1" in base_url
    assert "pw" not in format_posture(posture)
