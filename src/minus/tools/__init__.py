"""Tool registration.

Importing this package registers every built-in tool on the shared registry.
A new capability is added by writing a decorated function in a module under
`builtin/` (or anywhere else) and importing it here.
"""

from __future__ import annotations

from minus.tools.builtin import clock as _clock  # noqa: F401  (registration side effect)
from minus.tools.builtin import files as _files  # noqa: F401  (registration side effect)
from minus.tools.registry import Tool, ToolRegistry, registry

__all__ = ["Tool", "ToolRegistry", "registry"]
