"""The /proc and /sys parsers, against captured text.

Readers are injected, so none of this touches the real machine -- a test that
asserted on whatever this box happens to be doing would prove nothing and fail
at random.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minus.system.metrics import (
    SystemMetrics,
    parse_cpu,
    parse_gpu,
    parse_load,
    parse_memory,
    read_temperature,
)

PROC_STAT = """\
cpu  100 10 50 800 20 0 5 0 0 0
cpu0 50 5 25 400 10 0 2 0 0 0
intr 12345
"""

PROC_STAT_LATER = """\
cpu  200 10 100 1400 20 0 5 0 0 0
cpu0 100 5 50 700 10 0 2 0 0 0
intr 99999
"""

MEMINFO = """\
MemTotal:       32000000 kB
MemFree:          500000 kB
MemAvailable:   16000000 kB
Buffers:          100000 kB
"""

NVIDIA = "NVIDIA GeForce RTX 4090, 42, 3000, 24564, 55\n"


class TestCpu:
    def test_a_single_sample_has_no_percentage(self):
        """The counters are since boot; one reading describes the whole uptime."""
        assert parse_cpu(PROC_STAT).since(None) is None

    def test_the_difference_between_two_samples(self):
        first = parse_cpu(PROC_STAT)
        second = parse_cpu(PROC_STAT_LATER)

        # busy 165 -> 315 (+150) against total 985 -> 1735 (+750)
        assert second.since(first) == pytest.approx(20.0)

    def test_identical_samples_are_not_a_division_by_zero(self):
        sample = parse_cpu(PROC_STAT)

        assert sample.since(sample) is None

    def test_a_file_without_the_aggregate_line(self):
        assert parse_cpu("intr 1\nctxt 2\n") is None


class TestMemory:
    def test_reports_used_against_available(self):
        """MemAvailable, not MemFree -- free is near zero on a healthy box."""
        memory = parse_memory(MEMINFO)

        assert memory["total"] == 32000000 * 1024
        assert memory["available"] == 16000000 * 1024
        assert memory["percent"] == pytest.approx(50.0)

    def test_an_empty_file_is_not_a_division_by_zero(self):
        assert parse_memory("")["percent"] == 0.0


class TestLoad:
    def test_reads_three_averages(self):
        assert parse_load("0.52 0.61 0.58 2/1234 5678") == (0.52, 0.61, 0.58)


class TestGpu:
    def test_parses_the_csv(self):
        gpus = parse_gpu(NVIDIA)

        assert gpus[0]["name"] == "NVIDIA GeForce RTX 4090"
        assert gpus[0]["utilization"] == 42.0
        assert gpus[0]["temperature"] == 55.0

    def test_skips_a_row_reporting_not_available(self):
        assert parse_gpu("Some Card, [N/A], [N/A], [N/A], [N/A]\n") == []

    def test_handles_no_gpus(self):
        assert parse_gpu("") == []


class TestTemperature:
    def test_takes_the_hottest_recognised_sensor(self, tmp_path):
        files = {}
        for name, sensors in (("k10temp", [45000, 62000]), ("nvme", [70000])):
            hwmon = tmp_path / f"hwmon{len(files)}"
            hwmon.mkdir()
            files[hwmon / "name"] = name
            (hwmon / "name").write_text(name, encoding="utf-8")
            for index, value in enumerate(sensors):
                (hwmon / f"temp{index + 1}_input").write_text(str(value), encoding="utf-8")

        # nvme is hotter, and deliberately not a CPU sensor.
        assert read_temperature(lambda p: Path(p).read_text(), tmp_path) == 62.0

    def test_no_sensors_is_not_an_error(self, tmp_path):
        assert read_temperature(lambda p: Path(p).read_text(), tmp_path) is None

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert read_temperature(lambda p: Path(p).read_text(), tmp_path / "absent") is None


class TestSystemMetrics:
    def build(self, texts: dict, runner=None) -> SystemMetrics:
        def reader(path):
            name = Path(path).name
            if name in texts:
                return texts[name]
            raise OSError(f"no {name}")

        return SystemMetrics(
            proc=Path("/proc"),
            hwmon=Path("/nonexistent"),
            reader=reader,
            runner=runner or (lambda command, timeout: NVIDIA),
        )

    def test_samples_everything_it_can(self):
        texts = {"stat": PROC_STAT, "meminfo": MEMINFO, "loadavg": "1.0 2.0 3.0 1/2 3"}
        metrics = self.build(texts)

        metrics.sample()  # priming read; CPU use only exists as a difference
        texts["stat"] = PROC_STAT_LATER
        sample = metrics.sample()

        assert sample["memory"]["percent"] == pytest.approx(50.0)
        assert sample["load"] == (1.0, 2.0, 3.0)
        assert sample["cpu_percent"] is not None

    def test_an_unreadable_file_does_not_lose_the_rest(self):
        metrics = self.build({"meminfo": MEMINFO})

        sample = metrics.sample()

        assert sample["cpu_percent"] is None
        assert sample["memory"]["percent"] == pytest.approx(50.0)

    def test_gpu_polling_gives_up_after_repeated_failures(self, monkeypatch):
        """A machine with no NVIDIA card must not shell out forever."""
        monkeypatch.setattr("minus.system.metrics.shutil.which", lambda name: "/usr/bin/nvidia-smi")
        calls = []

        def failing(command, timeout):
            calls.append(command)
            raise OSError("no such device")

        metrics = self.build({"stat": PROC_STAT}, runner=failing)
        for _ in range(10):
            metrics.sample()

        assert len(calls) == metrics.max_gpu_failures

    def test_a_missing_nvidia_smi_is_not_attempted(self, monkeypatch):
        monkeypatch.setattr("minus.system.metrics.shutil.which", lambda name: None)
        calls = []

        metrics = self.build({"stat": PROC_STAT}, runner=lambda c, t: calls.append(c) or "")
        metrics.sample()

        assert calls == []

    def test_a_successful_call_clears_the_failure_count(self, monkeypatch):
        monkeypatch.setattr("minus.system.metrics.shutil.which", lambda name: "/usr/bin/nvidia-smi")
        metrics = self.build({"stat": PROC_STAT})
        metrics.gpu_failures = 2

        sample = metrics.sample()

        assert metrics.gpu_failures == 0
        assert sample["gpus"][0]["utilization"] == 42.0
