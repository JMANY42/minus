"""The terminal release gate: review, confirm, then move."""

from __future__ import annotations

import json
from types import SimpleNamespace

from minus import cli
from minus.triage.pipeline import TriagePipeline

HASH = "a" * 40


def _pipeline(tmp_path) -> TriagePipeline:
    quarantine = tmp_path / "quarantine"
    content = quarantine / HASH / "files" / "Movie"
    content.mkdir(parents=True)
    (content / "movie.mkv").write_bytes(b"payload")
    manifest = {
        "torrent_hash": HASH,
        "name": "Movie",
        "content_name": "Movie",
        "entries": [{"relative": "Movie/movie.mkv", "size": 7, "sha256_head": "0" * 64}],
    }
    (quarantine / HASH / "manifest.json").write_text(json.dumps(manifest))
    pipeline = TriagePipeline(
        quarantine=quarantine,
        library=tmp_path / "library",
        require_vpn=False,
    )
    pipeline.scan(HASH)
    return pipeline


def _args(**overrides) -> SimpleNamespace:
    values = {
        "action": "promote",
        "target": HASH,
        "name": "",
        "no_detonate": False,
        "accept_review": True,
        "yes": True,
        "reason": "checked by test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_manual_promote_with_yes_moves_the_content(tmp_path, monkeypatch, capsys):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(_args())

    assert code == 0
    assert (tmp_path / "library" / "movie" / "movie.mkv").exists()
    assert "moved_to" in capsys.readouterr().out


def test_manual_promote_without_a_terminal_requires_yes(tmp_path, monkeypatch, capsys):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    class NoTty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", NoTty())

    code = cli.run_triage_command(_args(yes=False))

    assert code == 1
    assert not (tmp_path / "library").exists()
    assert "--yes is required" in capsys.readouterr().out


def test_review_prints_the_facts_and_the_release_command(tmp_path, monkeypatch, capsys):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(_args(action="review", accept_review=False))

    assert code == 0
    output = capsys.readouterr().out
    assert "media_probe" in output
    assert "--accept-review" in output
