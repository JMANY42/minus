"""The interactive harness: `minus` with no subcommand.

`run_harness` is the entry point. The app and its thread helpers are exported
too so tests can drive the real widget tree through Textual's pilot rather than
starting a terminal.
"""

from minus.harness.app import MODES, MinusHarness, run_harness

__all__ = ["MODES", "MinusHarness", "run_harness"]
