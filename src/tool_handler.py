import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent


class ToolHandler:
    def __init__(self, tools_path=None):
        self.tools_path = Path(tools_path) if tools_path else WORKSPACE_ROOT / "tools.json"
        self.tools = self._load_tools()
        self.tool_names = {
            tool["function"]["name"]: tool for tool in self.tools if tool.get("function", {}).get("name")
        }

    def load_tools(self):
        return self.tools

    def execute(self, tool_name, raw_arguments):
        if tool_name not in self.tool_names:
            raise ValueError(f"Unknown tool: {tool_name}")

        arguments = self._parse_arguments(raw_arguments)

        if tool_name == "get_current_time":
            return self._get_current_time(arguments)
        if tool_name == "list_workspace_files":
            return self._list_workspace_files(arguments)
        if tool_name == "read_workspace_file":
            return self._read_workspace_file(arguments)

        raise ValueError(f"No executor registered for tool: {tool_name}")

    def _load_tools(self):
        if not self.tools_path.exists():
            return []

        with self.tools_path.open("r", encoding="utf-8") as file_handle:
            tools = json.load(file_handle)

        if not isinstance(tools, list):
            raise ValueError("tools.json must contain a JSON array of tool definitions")

        return tools

    def _parse_arguments(self, raw_arguments):
        if raw_arguments in (None, ""):
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str):
            return json.loads(raw_arguments)

        raise TypeError(f"Unsupported tool argument type: {type(raw_arguments)!r}")

    def _resolve_workspace_path(self, relative_path="."):
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("Tool paths must be workspace-relative")

        resolved = (WORKSPACE_ROOT / candidate).resolve()
        workspace_prefix = str(WORKSPACE_ROOT.resolve()) + os.sep

        if resolved != WORKSPACE_ROOT.resolve() and not str(resolved).startswith(workspace_prefix):
            raise ValueError("Tool path escapes the workspace root")

        return resolved

    def _get_current_time(self, arguments):
        central_time = datetime.now(ZoneInfo("America/Chicago"))
        dt_string = central_time.strftime("%Y-%m-%d %H:%M:%S %Z%z")
        return json.dumps({"central_time": dt_string}, ensure_ascii=False)

    def _list_workspace_files(self, arguments):
        relative_path = arguments.get("path", ".")
        directory_path = self._resolve_workspace_path(relative_path)

        if not directory_path.exists():
            raise FileNotFoundError(f"Path does not exist: {relative_path}")
        if not directory_path.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {relative_path}")

        entries = []
        for item in sorted(directory_path.iterdir(), key=lambda path: path.name.lower()):
            entries.append(
                {
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                }
            )

        return json.dumps(
            {
                "path": str(directory_path.relative_to(WORKSPACE_ROOT)),
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _read_workspace_file(self, arguments):
        relative_path = arguments.get("path")
        if not relative_path:
            raise ValueError("read_workspace_file requires a path")

        file_path = self._resolve_workspace_path(relative_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File does not exist: {relative_path}")
        if not file_path.is_file():
            raise IsADirectoryError(f"Path is not a file: {relative_path}")

        content = file_path.read_text(encoding="utf-8")
        return json.dumps(
            {
                "path": str(file_path.relative_to(WORKSPACE_ROOT)),
                "content": content,
            },
            ensure_ascii=False,
        )