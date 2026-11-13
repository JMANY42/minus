"""Lua plugins: tools written in Lua, loaded from `plugins/`.

Why Lua and not "more Python": a tool is one decorated function, and that is
already cheap to add. What is expensive to add *safely on a running assistant*
is code you did not write. Lua gives a small, auditable surface, a
table-returning contract that maps onto the JSON schema the registry already
speaks, and one directory per plugin that can be replaced and reloaded without
touching the interpreter.

The contract, one plugin per directory::

    plugins/<dir>/init.lua

    return {
      name = "echo",
      description = "Echo the given text back.",
      category = "",                       -- optional; "" draws the tool loose
      args = {
        text = { type = "string", description = "...", required = true },
        loud = { type = "boolean", default = false },
      },
      run = function(args) return { echoed = args.text } end,
    }

`args` types: string, integer, number, boolean, array, object. A returned table
becomes JSON (an integer-keyed 1..n table becomes an array); a returned string
is used as the tool result as-is.

The schema is *declared* by the plugin rather than derived from a signature,
because a Lua function has no signature Python can introspect — the one place
where the registry's "the function is the schema" rule cannot hold.

SANDBOX: THIS IS NOT A SECURITY BOUNDARY.
`lupa` runs Lua in-process. Denying `os`, `io`, `require`, `debug` and
`package` stops a plugin from casually reading the disk or shelling out; it does
not stop a determined one, and there is no instruction or memory limit. Treat
`plugins/` as code you would run. The point of the sandbox is that a plugin
written by mistake cannot take the assistant down quietly: every call is
wrapped, every failure becomes a tool error the model can read, and a plugin
that fails to load is reported and skipped rather than aborting startup.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import create_model

from minus import paths
from minus.errors import MinusError, ToolError, ToolExecutionError
from minus.services.json import serialize_json
from minus.tools.registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)

PLUGIN_FILE = "init.lua"

# Removed from every plugin's global table. `ffi` and `jit` are LuaJIT's route
# back out to C, so they matter as much as `os` does.
_DENIED_GLOBALS = (
    "os",
    "io",
    "package",
    "debug",
    "dofile",
    "loadfile",
    "load",
    "loadstring",
    "require",
    "collectgarbage",
    "newproxy",
    "jit",
    "ffi",
)

# Declaration type -> the Python type the validator uses.
_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class PluginError(MinusError):
    """A plugin could not be loaded, or is not the agreed shape."""


@dataclass(frozen=True)
class PluginSpec:
    """One plugin, as read off disk but not yet registered."""

    name: str
    description: str
    category: str
    arguments: dict[str, dict]
    path: Path
    source: str

    def schema(self) -> dict:
        """The OpenAI function schema the registry will hand the model."""
        properties: dict[str, dict] = {}
        required: list[str] = []
        for argument, declaration in self.arguments.items():
            entry: dict[str, Any] = {"type": declaration["type"]}
            if declaration.get("description"):
                entry["description"] = declaration["description"]
            properties[argument] = entry
            if declaration.get("required"):
                required.append(argument)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def validator(self) -> type:
        """A pydantic model over the declared arguments."""
        fields: dict[str, Any] = {}
        for argument, declaration in self.arguments.items():
            annotation = _TYPES[declaration["type"]]
            if declaration.get("required"):
                default = ...
            elif "default" in declaration:
                default = declaration["default"]
            else:
                default = None
            fields[argument] = (annotation, default)
        return create_model(f"{self.name}_PluginArguments", **fields)


@dataclass
class _Loaded:
    """A plugin's live runtime and its entry point."""

    spec: PluginSpec
    run: Any
    runtime: Any = field(repr=False)


def lua_available() -> bool:
    """Whether the `plugins` extra is installed."""
    return importlib.util.find_spec("lupa") is not None


def load_plugins(registry: ToolRegistry, directory: Path | None = None) -> LuaPluginHost | None:
    """Register every plugin found, or return None when lupa is not installed.

    Missing lupa is not an error: plugins are an extra, and a checkout that
    never asked for them must still run `minus tools` and `minus serve`.
    """
    if not lua_available():
        logger.info("Lua plugins are off: lupa is not installed (uv sync --extra plugins)")
        return None

    host = LuaPluginHost(directory)
    host.register(registry)
    return host


class LuaPluginHost:
    """Finds, compiles and serves Lua plugins."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.directory = Path(directory) if directory is not None else paths.plugins_dir()
        self._registry = registry
        self._loaded: dict[str, _Loaded] = {}
        # What this host has put on the registry, so a reload can take away what
        # a deleted plugin left behind and leave everything else alone.
        self._installed: set[str] = set()

    # ---- Discovery ----

    def paths(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(path / PLUGIN_FILE for path in self.directory.iterdir() if path.is_dir())

    def discover(self) -> tuple[dict[str, _Loaded], dict[str, str]]:
        """Compile every plugin, plus a per-directory reason for the invalid ones.

        Each file is compiled exactly once here; the runtime that answers the
        tool call is the same one that produced the spec, so a plugin with a
        load-time side effect does not get two of them.
        """
        loaded: dict[str, _Loaded] = {}
        errors: dict[str, str] = {}

        for path in self.paths():
            directory = path.parent.name
            if not path.is_file():
                errors[directory] = f"missing {PLUGIN_FILE}"
                continue
            try:
                entry = _load(path)
            except PluginError as exc:
                errors[directory] = str(exc)
                continue
            if entry.spec.name in loaded:
                errors[directory] = f"duplicate plugin name {entry.spec.name!r}"
                continue
            loaded[entry.spec.name] = entry

        return loaded, errors

    # ---- Loading and registration ----

    def reload(self) -> dict:
        """(Re)compile every plugin and make the registry match the directory.

        Safe on a running assistant: `ToolLoop` asks for schemas at the start of
        every turn, so a changed argument list is offered from the next turn and
        a removed plugin stops being offered rather than failing at call time.
        """
        loaded, errors = self.discover()

        removed = sorted(self._installed - set(loaded))
        if self._registry is not None:
            for name in removed:
                if name in self._registry:
                    self._registry.remove_tool(name)

            for name, entry in list(loaded.items()):
                tool = self._build_tool(entry)
                if name in self._installed:
                    self._registry.replace_tool(tool)
                elif name in self._registry:
                    # A plugin must not be able to take over a built-in tool by
                    # choosing its name: that would change what a model was
                    # already told about without anyone editing source.
                    errors[entry.spec.path.parent.name] = (
                        f"name {name!r} collides with an existing tool"
                    )
                    loaded.pop(name)
                    continue
                else:
                    self._registry.add_tool(tool)

        self._loaded = loaded
        self._installed = set(loaded)
        return {"loaded": sorted(loaded), "removed": removed, "errors": errors}

    def register(self, registry: ToolRegistry) -> dict:
        """Install into `registry` and remember it for later reloads."""
        if not lua_available():
            # A host built directly (rather than through `load_plugins`) must
            # not take the assistant down on a core install.
            logger.info("Lua plugins are off: lupa is not installed (uv sync --extra plugins)")
            return {"loaded": [], "removed": [], "errors": {}}
        self._registry = registry
        result = self.reload()
        if result["errors"]:
            logger.warning("Plugin load problems: %s", result["errors"])
        logger.info(
            "Loaded %d Lua plugin(s): %s", len(result["loaded"]), ", ".join(result["loaded"])
        )
        return result

    def _build_tool(self, entry: _Loaded) -> Tool:
        name = entry.spec.name

        def call(**arguments: Any) -> Any:
            return self.call(name, **arguments)

        return Tool(
            name=name,
            func=call,
            schema=entry.spec.schema(),
            validator=entry.spec.validator(),
            category=entry.spec.category,
        )

    # ---- Calling ----

    def call(self, name: str, **arguments: Any) -> Any:
        entry = self._loaded.get(name)
        if entry is None:
            raise ToolExecutionError(f"Plugin {name!r} is not loaded")

        # An optional argument the caller left out arrives as None from the
        # pydantic model; Lua should see nothing rather than a nil slot.
        payload = {key: value for key, value in arguments.items() if value is not None}
        try:
            result = entry.run(_to_lua(entry.runtime, payload))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"Plugin {name!r} failed: {exc}") from exc

        converted = _from_lua(result)
        return converted if isinstance(converted, str) else serialize_json(converted)


# ---- Reading a plugin off disk ----


def _load(path: Path) -> _Loaded:
    source = path.read_text(encoding="utf-8")
    runtime = _runtime()
    try:
        table = runtime.execute(source)
    except Exception as exc:
        raise PluginError(f"Lua failed to load {path}: {exc}") from exc

    if _lua_type(table) != "table":
        raise PluginError(f"{path} must return a table")

    name = _text(table["name"])
    if not name or not name.replace("_", "").isalnum():
        raise PluginError(f"{path}: name must be a non-empty identifier")

    run = table["run"]
    if _lua_type(run) != "function":
        raise PluginError(f"{path}: run must be a function")

    spec = PluginSpec(
        name=name,
        description=_text(table["description"]) or f"Lua plugin {name}",
        category=_text(table["category"]),
        arguments=_arguments(table["args"], path),
        path=path,
        source=source,
    )
    return _Loaded(spec=spec, run=run, runtime=runtime)


def _arguments(table: Any, path: Path) -> dict[str, dict]:
    if table is None:
        return {}
    if _lua_type(table) != "table":
        raise PluginError(f"{path}: args must be a table")

    declarations: dict[str, dict] = {}
    for argument, declaration in table.items():
        if _lua_type(declaration) != "table":
            raise PluginError(f"{path}: argument {argument!r} must be a table")

        kind = _text(declaration["type"]) or "string"
        if kind not in _TYPES:
            raise PluginError(f"{path}: argument {argument!r} has unknown type {kind!r}")

        entry: dict[str, Any] = {"type": kind}
        description = _text(declaration["description"])
        if description:
            entry["description"] = description

        # `required` and `default` come back as plain Python bools/values, not
        # Lua objects, so presence is a `None` check rather than a type check.
        required = declaration["required"]
        if required is not None:
            entry["required"] = bool(required)
        default = declaration["default"]
        if default is not None:
            entry["default"] = _from_lua(default)

        declarations[str(argument)] = entry
    return declarations


# ---- The Lua side ----


def _runtime():
    """A fresh, restricted Lua runtime per plugin.

    One runtime each rather than one shared: a plugin that reassigns a global
    would otherwise change what the next plugin sees, and the failure would be
    blamed on the wrong file.
    """
    import lupa

    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    globals_ = runtime.globals()
    for name in _DENIED_GLOBALS:
        # Not every name exists in every Lua build (Lua 5.4 has no `loadstring`).
        with contextlib.suppress(Exception):
            globals_[name] = None

    def log(message: Any) -> None:
        logger.info("[plugin] %s", message)

    globals_["log"] = log
    return runtime


def _lua_type(value: Any) -> str:
    """`lupa.lua_type`, normalised so callers can compare strings.

    Returns "none" for values that are not Lua objects at all (a Python bool,
    for instance), which is why scalar presence is checked with `is None`
    rather than through this.
    """
    import lupa

    return lupa.lua_type(value) or "none"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _to_lua(runtime: Any, value: Any) -> Any:
    if isinstance(value, dict):
        table = runtime.table()
        for key, item in value.items():
            table[key] = _to_lua(runtime, item)
        return table
    if isinstance(value, (list, tuple)):
        table = runtime.table()
        for index, item in enumerate(value, start=1):
            table[index] = _to_lua(runtime, item)
        return table
    return value


def _from_lua(value: Any) -> Any:
    """A Lua value as something `serialize_json` can hold."""
    import lupa

    kind = lupa.lua_type(value)
    if kind is None:
        # Not a Lua object: lupa hands back plain Python values, and `nil`
        # becomes None. A tool result of nil is an empty result, not a null.
        return {} if value is None else value
    if kind != "table":
        # A function, thread or userdata cannot be JSON. Returning it lets
        # `serialize_json` raise, which `dispatch` turns into a tool error.
        return value

    items = list(value.items())
    if not items:
        return {}

    # An integer-keyed 1..n table is Lua's array; anything else is a record.
    keys = [key for key, _ in items]
    if all(isinstance(key, int) for key in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_from_lua(value[index]) for index in range(1, len(keys) + 1)]

    return {str(key): _from_lua(item) for key, item in items}
