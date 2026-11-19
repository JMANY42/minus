"""MCP surface: the MINUS tool registry over newline-delimited JSON-RPC.

`python -m minus.mcp` is the portable entry point; `minus mcp` is the same
server behind the existing CLI, and `minus-mcp` is the console script.
"""

from minus.mcp.server import McpServer, build_registry, main, serve

__all__ = ["McpServer", "build_registry", "main", "serve"]
