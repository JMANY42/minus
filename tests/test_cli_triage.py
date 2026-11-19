"""The terminal release gate: review, confirm, then move."""

from __future__ import annotations

import json
from types import SimpleNamespace

from minus import cli
from minus.triage.pipeline import TriagePipeline
from minus.triage.trace import JsonlTracer, TraceEvent

HASH = "a" * 40


def _pipeline(tmp_path) -> TriagePipeline:
    quarantine = tmp_path / "quarantine"
    content = quarantine / HASH / "files" / "Movie"
    content.mkdir(parents=True)
    (content / "movie.mp4").write_bytes(b"payload")
    manifest = {
        "torrent_hash": HASH,
        "name": "Movie",
        "content_name": "Movie",
        "entries": [{"relative": "Movie/movie.mp4", "size": 7, "sha256_head": "0" * 64}],
    }
    (quarantine / HASH / "manifest.json").write_text(json.dumps(manifest))
    pipeline = TriagePipeline(
        quarantine=quarantine,
        library=tmp_path / "library",
        require_vpn=False,
    )
    pipeline.scan(HASH)
    # A manual release now requires VM review evidence; this fixture is the
    # "guest opened the movie and observed nothing" case, not a dry run.
    payload = json.loads(pipeline.verdict_path(HASH).read_text())
    payload["sandbox"] = {
        "engine": "test-guest",
        "detonated": True,
        "opened": 1,
        "reviewed": True,
    }
    pipeline.verdict_path(HASH).write_text(json.dumps(payload))
    return pipeline


def _args(**overrides) -> SimpleNamespace:
    values = {
        "action": "promote",
        "target": HASH,
        "name": "",
        "keep": "",
        "release": False,
        "no_detonate": False,
        "accept_review": True,
        "yes": True,
        "reason": "checked by test",
        "frames": 6,
        "title": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_manual_promote_with_yes_moves_the_content(tmp_path, monkeypatch, capsys):
    pipeline = _pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(_args())

    assert code == 0
    assert (tmp_path / "library" / "movie.mp4").exists()
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


def _traced_pipeline(tmp_path) -> TriagePipeline:
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    tracer.emit(
        TraceEvent(stage="scan", event="done", torrent_hash=HASH, data={"verdict": "clean"})
    )
    tracer.emit(
        TraceEvent(stage="scan", event="done", torrent_hash="b" * 40, data={"verdict": "clean"})
    )
    return TriagePipeline(
        quarantine=tmp_path / "quarantine",
        library=tmp_path / "library",
        require_vpn=False,
        tracer=tracer,
    )


def test_trace_prints_only_the_events_for_the_requested_hash(tmp_path, monkeypatch, capsys):
    pipeline = _traced_pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(
        SimpleNamespace(
            action="trace",
            target=HASH,
            name="",
            keep="",
            release=False,
            no_detonate=False,
            accept_review=False,
            yes=False,
            reason="",
        )
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["count"] == 1
    assert output["trace_file"] == str(pipeline.trace_file)
    assert [event["torrent_hash"] for event in output["events"]] == [HASH]
    assert "ALERT" not in json.dumps(output)


def test_trace_without_a_target_prints_the_tail(tmp_path, monkeypatch, capsys):
    pipeline = _traced_pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(
        SimpleNamespace(
            action="trace",
            target="",
            name="",
            keep="",
            release=False,
            no_detonate=False,
            accept_review=False,
            yes=False,
            reason="",
        )
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["count"] == 2


def test_trace_without_a_tracer_says_so(tmp_path, monkeypatch, capsys):
    pipeline = TriagePipeline(
        quarantine=tmp_path / "quarantine",
        library=tmp_path / "library",
        require_vpn=False,
    )
    assert pipeline.trace_file is None
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(
        SimpleNamespace(
            action="trace",
            target=HASH,
            name="",
            keep="",
            release=False,
            no_detonate=False,
            accept_review=False,
            yes=False,
            reason="",
        )
    )

    assert code == 1
    assert "tracing is not configured" in capsys.readouterr().out


def test_trace_refuses_accept_review(tmp_path, monkeypatch, capsys):
    pipeline = _traced_pipeline(tmp_path)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: pipeline)

    code = cli.run_triage_command(
        SimpleNamespace(
            action="trace",
            target=HASH,
            name="",
            keep="",
            release=False,
            no_detonate=False,
            accept_review=True,
            yes=False,
            reason="",
        )
    )

    assert code == 1
    assert "--accept-review is only valid with `triage promote`." in capsys.readouterr().out


def _stub_pipeline(monkeypatch, **methods):
    class Stub:
        pass

    stub = Stub()
    for name, implementation in methods.items():
        setattr(stub, name, implementation)
    monkeypatch.setattr("minus.assembly.build_triage_pipeline", lambda _settings: stub)
    return stub


def test_preview_passes_frames_and_title_and_prints_its_json(monkeypatch, capsys):
    calls = {}

    def preview(target, *, frames, expected_title):
        calls.update(target=target, frames=frames, expected_title=expected_title)
        return {"status": "ok", "available": True, "frames": frames}

    _stub_pipeline(monkeypatch, preview=preview)

    code = cli.run_triage_command(
        _args(action="preview", accept_review=False, frames=3, title="The Movie")
    )

    assert code == 0
    assert calls == {"target": HASH, "frames": 3, "expected_title": "The Movie"}
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "available": True,
        "frames": 3,
    }


def test_ingest_passes_the_expected_title(monkeypatch, capsys):
    calls = {}

    def ingest(target, *, detonate, expected_title, release, name):
        calls.update(
            target=target,
            detonate=detonate,
            expected_title=expected_title,
            release=release,
            name=name,
        )
        return {"status": "ok"}

    monkeypatch.setattr("minus.scripts.qbt.ensure_running", lambda _settings: None)
    _stub_pipeline(monkeypatch, ingest=ingest)

    code = cli.run_triage_command(
        _args(action="ingest", accept_review=False, title="The Movie", no_detonate=True)
    )

    assert code == 0
    assert calls == {
        "target": HASH,
        "detonate": False,
        "expected_title": "The Movie",
        "release": False,
        "name": "",
    }
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}


def test_ingest_release_is_forwarded(monkeypatch, capsys):
    calls = {}

    def ingest(target, *, detonate, expected_title, release, name):
        calls.update(release=release, name=name)
        return {"status": "ok"}

    monkeypatch.setattr("minus.scripts.qbt.ensure_running", lambda _settings: None)
    _stub_pipeline(monkeypatch, ingest=ingest)

    code = cli.run_triage_command(
        _args(
            action="ingest",
            accept_review=False,
            release=True,
            name="The Movie",
        )
    )

    assert code == 0
    assert calls == {"release": True, "name": "The Movie"}
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}


def test_release_is_refused_without_ingest(monkeypatch, capsys):
    _stub_pipeline(monkeypatch)

    code = cli.run_triage_command(_args(action="promote", release=True))

    assert code == 1
    assert "--release is only valid with `triage ingest`." in capsys.readouterr().out


def test_vm_action_calls_vm_review_with_keep(monkeypatch, capsys):
    calls = {}

    def vm_review(target, *, keep=None):
        calls.update(target=target, keep=keep)
        return {"torrent_hash": target, "opened": 1, "detonated": True}

    _stub_pipeline(monkeypatch, vm_review=vm_review)

    code = cli.run_triage_command(_args(action="vm", accept_review=False, keep="movie.mp4"))

    assert code == 0
    assert calls == {"target": HASH, "keep": ["movie.mp4"]}
    assert json.loads(capsys.readouterr().out)["opened"] == 1


def test_keep_is_refused_outside_vm_and_promote(monkeypatch, capsys):
    _stub_pipeline(monkeypatch)

    code = cli.run_triage_command(_args(action="review", accept_review=False, keep="x"))

    assert code == 1
    assert "--keep is only valid" in capsys.readouterr().out
