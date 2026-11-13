"""Malicious verdicts delete the payload and keep a verdict record.

Real files on disk: the hazard is the bytes, and the test proves the bytes are
gone while the evidence remains.
"""

from __future__ import annotations

import json
from pathlib import Path

from minus.config import Settings
from minus.triage.pipeline import TriagePipeline

HASH = "c" * 40


def _staged(tmp_path: Path) -> tuple[TriagePipeline, Path]:
    pipeline = TriagePipeline(quarantine=tmp_path / "quarantine", library=tmp_path / "library")
    files = pipeline.files_root(HASH) / "Movie"
    files.mkdir(parents=True)
    (files / "evil.exe").write_bytes(b"MZ" + b"\x00" * 32)
    return pipeline, files


def _payload() -> dict:
    return {
        "torrent_hash": HASH,
        "name": "Movie",
        "verdict": {"label": "malicious", "reasons": ["defender detection: Trojan:Win32/X"]},
        "static": {
            "flags": ["windows-pe"],
            "files": [{"relative": "Movie/evil.exe"}],
        },
    }


def test_reject_deletes_the_payload_and_keeps_the_record(tmp_path, caplog):
    pipeline, files = _staged(tmp_path)
    payload = _payload()

    result = pipeline.reject_malicious(HASH, payload)

    assert result["deleted"] is True
    assert not files.exists()
    assert not (tmp_path / "quarantine" / HASH).exists()

    record_path = Path(result["rejected_record"])
    assert record_path.is_file()
    record = json.loads(record_path.read_text())
    assert record["verdict"]["label"] == "malicious"
    assert record["file_names"] == ["Movie/evil.exe"]
    assert "Trojan:Win32/X" in " ".join(record["verdict"]["reasons"])

    assert (record_path.stat().st_mode & 0o777) == 0o600
    assert (record_path.parent.stat().st_mode & 0o777) == 0o700
    assert "MALICIOUS torrent" in caplog.text


def test_deletion_is_configurable_and_on_by_default():
    assert Settings().triage_delete_malicious is True
    assert Settings(triage_delete_malicious=False).triage_delete_malicious is False


def test_the_pipeline_default_keeps_malicious_payloads_deleted(tmp_path):
    pipeline = TriagePipeline(quarantine=tmp_path / "q", library=tmp_path / "l")
    assert pipeline.delete_malicious is True
