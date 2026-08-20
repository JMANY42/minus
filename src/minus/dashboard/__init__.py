"""The management dashboard.

Deliberately empty, and it matters here more than usual: `tail.py` holds the
logic worth testing and imports nothing from textual, so leaving this module
bare is what lets those tests run in an environment where the `dashboard`
extra was never installed.
"""
