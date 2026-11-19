"""The trace is a report, not a participant: it records, and never breaks a scan."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from minus.triage.pipeline import TriagePipeline
from minus.triage.trace import JsonlTracer, TraceEvent, read_trace, trace_span

HASH = "a" * 40


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class JsonlTracerTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.path = self.root / "trace.jsonl"
        self.tracer = JsonlTracer(self.path)

    def test_each_emit_is_one_json_line_and_appends(self):
        self.tracer.emit(TraceEvent(stage="scan", event="one", torrent_hash=HASH))
        self.tracer.emit(TraceEvent(stage="scan", event="two", torrent_hash=HASH))

        recorded = events(self.path)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[0]["event"], "one")
        self.assertEqual(recorded[1]["event"], "two")
        self.assertTrue(recorded[0]["ts"])
        self.assertEqual(recorded[0]["stage"], "scan")

    def test_file_is_private(self):
        self.tracer.emit(TraceEvent(stage="scan", event="one"))

        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_broken_echo_does_not_escape(self):
        tracer = JsonlTracer(self.path, echo=lambda event: 1 / 0)

        tracer.emit(TraceEvent(stage="scan", event="one"))

        self.assertEqual(len(events(self.path)), 1)


class TraceSpanTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "trace.jsonl"
        self.tracer = JsonlTracer(self.path)

    def test_done_carries_duration_and_the_yielded_result(self):
        with trace_span(
            self.tracer, op_id="op", stage="download", event="stage", torrent_hash=HASH, url="u"
        ) as result:
            result["files"] = 3

        recorded = events(self.path)
        self.assertEqual([entry["event"] for entry in recorded], ["stage.start", "stage.done"])
        done = recorded[-1]
        self.assertIsInstance(done["duration_ms"], float)
        self.assertEqual(done["data"], {"url": "u", "files": 3})
        self.assertEqual(done["torrent_hash"], HASH)

    def test_failure_is_recorded_and_re_raised(self):
        with (
            self.assertRaises(RuntimeError),
            trace_span(self.tracer, op_id="op", stage="scan", event="inspect") as result,
        ):
            result["stage"] = "static"
            raise RuntimeError("boom")

        recorded = events(self.path)
        self.assertEqual(
            [entry["event"] for entry in recorded], ["inspect.start", "inspect.failed"]
        )
        self.assertEqual(recorded[-1]["data"]["error"], "boom")
        self.assertEqual(recorded[-1]["data"]["error_type"], "RuntimeError")

    def test_no_tracer_still_yields_a_dict(self):
        with trace_span(None, op_id="op", stage="scan", event="inspect", a=1) as result:
            result["b"] = 2

        self.assertEqual(result, {"a": 1, "b": 2})
        self.assertFalse(self.path.exists())


class ReadTraceTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "trace.jsonl"
        self.tracer = JsonlTracer(self.path)

    def test_missing_file_is_an_empty_history(self):
        self.assertEqual(read_trace(self.path), [])

    def test_filters_by_hash_case_insensitively_and_respects_limit(self):
        for index in range(4):
            self.tracer.emit(TraceEvent(stage="scan", event=f"a{index}", torrent_hash="A" * 40))
        self.tracer.emit(TraceEvent(stage="scan", event="other", torrent_hash="b" * 40))

        filtered = read_trace(self.path, torrent_hash="a" * 40)
        self.assertEqual([entry["event"] for entry in filtered], ["a0", "a1", "a2", "a3"])

        limited = read_trace(self.path, torrent_hash="a" * 40, limit=2)
        self.assertEqual([entry["event"] for entry in limited], ["a2", "a3"])

    def test_malformed_lines_are_skipped(self):
        self.path.write_text(
            json.dumps({"stage": "scan", "event": "ok", "torrent_hash": HASH})
            + "\n"
            + "{not json\n"
            + "[]\n"
        )

        self.assertEqual([entry["event"] for entry in read_trace(self.path)], ["ok"])


class PipelineTraceTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.quarantine = self.root / "quarantine"
        self.library = self.root / "library"
        self.path = self.root / "trace.jsonl"

    def stage(self, name: str = "Movie") -> None:
        content = self.quarantine / HASH / "files" / name
        content.mkdir(parents=True)
        (content / "movie.mkv").write_bytes(b"payload")
        manifest = {
            "torrent_hash": HASH,
            "name": name,
            "content_name": name,
            "entries": [{"relative": f"{name}/movie.mkv", "size": 7, "sha256_head": "0" * 64}],
        }
        (self.quarantine / HASH / "manifest.json").write_text(json.dumps(manifest))

    def scan_pipeline(self, sandbox=None) -> TriagePipeline:
        kwargs = {"sandbox": sandbox} if sandbox is not None else {}
        return TriagePipeline(
            quarantine=self.quarantine,
            library=self.library,
            require_vpn=False,
            tracer=JsonlTracer(self.path),
            **kwargs,
        )

    def test_scan_records_inspection_and_verdict(self):
        self.stage()

        self.scan_pipeline().scan(HASH)

        recorded = events(self.path)
        names = {(entry["stage"], entry["event"]) for entry in recorded}
        self.assertIn(("scan", "inspect.start"), names)
        self.assertIn(("scan", "inspect.done"), names)
        self.assertIn(("verdict", "verdict.done"), names)
        verdict = next(entry for entry in recorded if entry["event"] == "verdict.done")
        self.assertEqual(verdict["torrent_hash"], HASH)
        self.assertEqual(verdict["data"]["label"], "needs_review")

    def test_a_sandbox_skip_reason_means_run_is_never_called(self):
        self.stage()

        class SkippingSandbox:
            name = "fake_vm"

            def skip_reason(self, artifacts):
                return "guest is disabled"

            def run(self, artifacts, *, timeout):
                raise AssertionError("run must not be called when skipped")

        result = self.scan_pipeline(SkippingSandbox()).scan(HASH)

        self.assertIn("Sandbox fake_vm skipped: guest is disabled", result["notes"])
        skipped = [entry for entry in events(self.path) if entry["event"] == "sandbox.skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["data"]["reason"], "guest is disabled")

    def test_an_already_present_magnet_is_not_added_again(self):
        class PresentClient:
            def torrent(self, torrent_hash):
                return {"hash": torrent_hash, "progress": 0.0}

            def add_torrent(self, urls, **kwargs):
                raise AssertionError("add_torrent must not be called for a present torrent")

            def torrents(self):
                return [{"hash": HASH, "progress": 0.0}]

        pipeline = TriagePipeline(
            quarantine=self.quarantine,
            library=self.library,
            client=PresentClient(),
            require_vpn=False,
            tracer=JsonlTracer(self.path),
        )
        magnet = f"magnet:?xt=urn:btih:{HASH}&dn=Something"

        result = pipeline.ingest(magnet, detonate=False, wait=False)

        self.assertEqual(result["torrent_hash"], HASH)
        self.assertIn("status", result)
        recorded = events(self.path)
        skipped = [entry for entry in recorded if entry["event"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["stage"], "add")
        self.assertEqual(skipped[0]["data"]["reason"], "already present")
        self.assertEqual(skipped[0]["data"]["torrent_hash"], HASH)


if __name__ == "__main__":
    unittest.main()
