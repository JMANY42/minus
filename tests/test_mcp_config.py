"""`minus mcp-config` exists to be pasted into another program's config.

So the test is about the shape of stdout, not about MINUS's own settings: an
absolute interpreter, the stdio transport, and the project root in the
environment the client will hand the server.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from minus import cli


def test_mcp_config_prints_a_stdio_server_with_an_absolute_command(capsys):
    code = cli.run_mcp_config(SimpleNamespace(name="minus"))

    assert code == 0
    config = json.loads(capsys.readouterr().out)
    server = config["mcpServers"]["minus"]
    assert server["type"] == "stdio"
    assert server["args"] == ["-m", "minus.mcp"]
    assert Path(server["command"]).is_absolute()
    assert "MINUS_PROJECT_ROOT" in server["env"]
