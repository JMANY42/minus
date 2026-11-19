"""Secret scanning and credential-free reprs, against real files and settings.

No stand-ins: the scanner's whole job is to behave on real bytes -- binary
files, skipped trees, ignored decode errors -- so the fixtures here write real
files and let it walk them. The repr tests build a real `Settings`, because a
credential that leaks through the model's own printed form leaks regardless of
who printed it.

Each test that writes a secret writes it only into `tmp_path`. The finding
must name the file and line; the secret itself must appear nowhere in the
result, including the error messages of these tests.
"""

from __future__ import annotations

import json

from minus.config import Settings
from minus.security import scan_file, scan_repository

# 24 characters after the prefix, the shortest the OpenAI-style pattern accepts
# above its floor. Split so a reviewer can see what is a credential and what is
# pattern noise.
_SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2"


def test_a_key_shaped_string_is_reported_by_line_and_kind_never_quoted(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text(
        "first line, nothing here\n"
        "second line, still clean\n"
        f"OPENAI_KEY={_SECRET}\n"
        "fourth line\n",
        encoding="utf-8",
    )

    findings = scan_file(target)

    assert findings == [{"path": str(target), "line": 3, "kind": "openai_key"}]
    # The report is what gets pasted into an issue or a commit message; it must
    # be safe to paste.
    rendered = json.dumps(findings)
    assert _SECRET not in rendered
    assert "sk-" not in rendered


def test_a_clean_file_has_no_findings(tmp_path):
    target = tmp_path / "clean.py"
    target.write_text(
        "API_KEY_NAME = 'OPENROUTER_API_KEY'\n"
        "def main():\n"
        "    print('nothing secret here')\n",
        encoding="utf-8",
    )

    assert scan_file(target) == []


def test_binary_files_are_skipped(tmp_path):
    target = tmp_path / "model.onnx"
    # NUL in the first block marks it binary even though the key-shaped bytes
    # are present and would match once decoded with errors="ignore".
    target.write_bytes(b"\x00\x01\x02\x00" + _SECRET.encode("ascii"))

    assert scan_file(target) == []


def test_caches_checkouts_and_runtime_state_are_not_walked(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('clean')\n", encoding="utf-8")

    for skipped in (".venv", "memory"):
        directory = tmp_path / skipped
        directory.mkdir()
        (directory / "leak.txt").write_text(f"token={_SECRET}\n", encoding="utf-8")

    findings = scan_repository(tmp_path)

    assert findings == []
    assert not any(".venv" in item["path"] or "memory" in item["path"] for item in findings)


def test_scan_repository_finds_real_files_and_skips_nested_venv(tmp_path):
    (tmp_path / "config").mkdir()
    real = tmp_path / "config" / "secrets.txt"
    real.write_text(f"line one\nkey: '{_SECRET}'\n", encoding="utf-8")
    nested = tmp_path / "src" / ".venv" / "lib"
    nested.mkdir(parents=True)
    (nested / "leak.py").write_text(f"key = '{_SECRET}'\n", encoding="utf-8")

    findings = scan_repository(tmp_path)

    assert findings == [{"path": str(real), "line": 2, "kind": "openai_key"}]


def test_settings_repr_omits_every_credential(monkeypatch):
    # The exact construction the acceptance calls for; the second kwarg is not
    # a field name, so it is also proof that a stray env-style keyword does not
    # smuggle a value into the repr.
    settings = Settings(
        OPENROUTER_API_KEY="supersecretvalue123456",
        MINUS_LLM_API_KEY="anothersecret123456",
    )
    rendered = repr(settings)
    assert "supersecretvalue123456" not in rendered
    assert "anothersecret123456" not in rendered

    # And the value that *is* loaded, through the environment, still does not
    # print. All six credential fields are exercised: a repr that hides one and
    # forgets another is the failure mode this guards.
    credentials = {
        "MINUS_LLM_API_KEY": "anothersecret123456",
        "MINUS_GOOGLE_CLIENT_SECRET": "googleclientsecret1234",
        "MINUS_GOOGLE_REFRESH_TOKEN": "googlerefreshtoken1234",
        "MINUS_QBITTORRENT_PASSWORD": "qbittorrentpass1234",
        "MINUS_WINDOWS_WINRM_PASSWORD": "winrmpassword1234",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)

    settings = Settings()
    rendered = repr(settings)
    for value in credentials.values():
        assert value not in rendered
    assert settings.llm_api_key == credentials["MINUS_LLM_API_KEY"]
    assert settings.google_client_secret == credentials["MINUS_GOOGLE_CLIENT_SECRET"]
    assert settings.google_refresh_token == credentials["MINUS_GOOGLE_REFRESH_TOKEN"]
    assert settings.qbittorrent_password == credentials["MINUS_QBITTORRENT_PASSWORD"]
    assert settings.windows_winrm_password == credentials["MINUS_WINDOWS_WINRM_PASSWORD"]
