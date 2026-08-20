"""CPU, memory, temperature and GPU, read from the kernel directly.

`psutil` is the obvious choice and is not worth it here: it is a compiled
dependency that exists to paper over cgroups, containers and four operating
systems, none of which apply to one Linux box running one assistant whose
pyproject deliberately holds core dependencies at five packages. The files
below are stable kernel ABI and the parsing is about eighty lines.

Every reader is injected, so the parsers are tested against captured text
rather than against whatever this machine happens to be doing -- and swapping
in psutil later, if a second platform ever matters, is a change to one file.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# nvidia-smi initialises NVML on every call, which costs 150-300ms. Far too
# slow for a redraw, hence the cache in SystemMetrics.
NVIDIA_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
NVIDIA_TIMEOUT = 2.0


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def run_command(command: list[str], timeout: float = NVIDIA_TIMEOUT) -> str:
    # No shell: the argument list is fixed, and a shell here would be an
    # injection surface for no benefit at all.
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    return completed.stdout


@dataclass
class CpuSample:
    """One reading of /proc/stat's aggregate line."""

    busy: int
    total: int

    def since(self, previous: CpuSample | None) -> float | None:
        """Percentage busy between two samples.

        CPU use is only meaningful as a difference -- the counters are since
        boot, so a single reading describes the machine's whole uptime and
        not what it is doing now. The first call therefore has no answer.
        """
        if previous is None:
            return None
        total = self.total - previous.total
        if total <= 0:
            return None
        return 100.0 * (self.busy - previous.busy) / total


def parse_cpu(text: str) -> CpuSample | None:
    """The `cpu` aggregate line of /proc/stat."""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        values = [int(value) for value in line.split()[1:]]
        # user nice system idle iowait irq softirq steal ...
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return CpuSample(busy=total - idle, total=total)
    return None


def parse_memory(text: str) -> dict:
    """Total and available memory, in bytes, from /proc/meminfo.

    MemAvailable rather than MemFree: free memory on a healthy Linux box is
    near zero because the page cache uses the rest, and reporting that as
    pressure would be alarming and wrong.
    """
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            values[key] = int(rest.split()[0]) * 1024

    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    return {
        "total": total,
        "available": available,
        "used": total - available,
        "percent": (100.0 * (total - available) / total) if total else 0.0,
    }


def parse_load(text: str) -> tuple[float, float, float]:
    parts = text.split()
    return (float(parts[0]), float(parts[1]), float(parts[2]))


def parse_gpu(text: str) -> list[dict]:
    """nvidia-smi --format=csv,noheader,nounits output."""
    gpus = []
    for line in text.strip().splitlines():
        fields = [part.strip() for part in line.split(",")]
        if len(fields) < 5:
            continue
        try:
            gpus.append(
                {
                    "name": fields[0],
                    "utilization": float(fields[1]),
                    "memory_used": float(fields[2]),
                    "memory_total": float(fields[3]),
                    "temperature": float(fields[4]),
                }
            )
        except ValueError:
            # A GPU that reports [N/A] for a field it does not support.
            continue
    return gpus


def read_temperature(reader: Callable[[str | Path], str], root: Path) -> float | None:
    """The hottest CPU-ish sensor under /sys/class/hwmon.

    Names vary by platform -- k10temp on AMD, coretemp on Intel -- so the
    sensor is chosen by name where recognised and by "hottest" otherwise,
    which is the number worth showing on a dashboard anyway.
    """
    if not root.exists():
        return None

    readings: list[float] = []
    for hwmon in sorted(root.glob("hwmon*")):
        try:
            name = reader(hwmon / "name").strip()
        except OSError:
            continue
        if name not in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
            continue
        for sensor in sorted(hwmon.glob("temp*_input")):
            try:
                readings.append(int(reader(sensor)) / 1000.0)
            except (OSError, ValueError):
                continue

    return max(readings) if readings else None


@dataclass
class SystemMetrics:
    """A cached view of the machine, safe to poll often."""

    proc: Path = Path("/proc")
    hwmon: Path = Path("/sys/class/hwmon")
    reader: Callable[[str | Path], str] = read_text
    runner: Callable[[list[str], float], str] = run_command
    gpu_failures: int = 0
    _previous_cpu: CpuSample | None = field(default=None, repr=False)

    # Three strikes and the GPU section goes quiet. A machine with no NVIDIA
    # card should not shell out twice a second forever to keep discovering it.
    max_gpu_failures: int = 3

    def sample(self) -> dict:
        return {
            "cpu_percent": self._cpu(),
            "memory": self._safe(lambda: parse_memory(self.reader(self.proc / "meminfo")), {}),
            "load": self._safe(lambda: parse_load(self.reader(self.proc / "loadavg")), None),
            "temperature": self._safe(lambda: read_temperature(self.reader, self.hwmon), None),
            "gpus": self._gpus(),
        }

    def _safe(self, call, default):
        try:
            return call()
        except (OSError, ValueError, IndexError, KeyError):
            logger.debug("A system metric was unreadable", exc_info=True)
            return default

    def _cpu(self) -> float | None:
        sample = self._safe(lambda: parse_cpu(self.reader(self.proc / "stat")), None)
        if sample is None:
            return None
        percent = sample.since(self._previous_cpu)
        self._previous_cpu = sample
        return percent

    def _gpus(self) -> list[dict]:
        if self.gpu_failures >= self.max_gpu_failures:
            return []
        if shutil.which("nvidia-smi") is None:
            self.gpu_failures = self.max_gpu_failures
            return []

        try:
            output = self.runner(
                [
                    "nvidia-smi",
                    f"--query-gpu={NVIDIA_QUERY}",
                    "--format=csv,noheader,nounits",
                ],
                NVIDIA_TIMEOUT,
            )
        except Exception:
            self.gpu_failures += 1
            logger.debug("nvidia-smi failed (%d)", self.gpu_failures, exc_info=True)
            return []

        self.gpu_failures = 0
        return parse_gpu(output)
