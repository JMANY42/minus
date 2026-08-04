"""The tool registry: one place to declare, describe and dispatch a tool.

Adding a capability used to mean editing two files that nothing kept in sync
-- a schema entry in tools.json and a branch in an if/else chain. Here a tool
is a decorated function; its schema is derived from its signature and its
dispatch entry is its registration. There is no second place to update, so
there is nothing to drift.

    @registry.tool
    def set_light(room: str, brightness: int = 100) -> str:
        '''Set a room's light brightness.

        Args:
            room: Room name, e.g. "office".
            brightness: Brightness from 0 to 100.
        '''
        ...

Arguments arrive from the model as JSON and are validated against the derived
schema before the function runs, so a tool body can trust its parameters.
"""

from __future__ import annotations

import inspect
import logging
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError, create_model

from minus.errors import ToolArgumentError, ToolExecutionError, UnknownToolError
from minus.services.json import JSONDecodeError, parse_json, serialize_json
from minus.tools.schema import build_parameters_schema, build_tool_schema

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    """A registered capability: the callable plus its derived schema."""

    name: str
    func: Callable[..., Any]
    schema: dict
    validator: Any

    def validate(self, arguments: dict) -> dict:
        try:
            return self.validator(**arguments).model_dump()
        except ValidationError as exc:
            raise ToolArgumentError(f"Invalid arguments for {self.name!r}: {exc}") from exc

    def __call__(self, **kwargs: Any) -> Any:
        return self.func(**kwargs)


class ToolRegistry:
    """A named collection of tools, with schema generation and dispatch."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Any:
        """Register a function as a tool. Usable bare or with arguments."""

        def register(target: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or target.__name__
            if tool_name in self._tools:
                raise ValueError(f"Tool {tool_name!r} is already registered")

            parameters = build_parameters_schema(target)
            self._tools[tool_name] = Tool(
                name=tool_name,
                func=target,
                schema=build_tool_schema(target, tool_name, description),
                validator=_argument_validator(target, tool_name),
            )
            logger.debug(
                "Registered tool %s with parameters %s",
                tool_name,
                sorted(parameters["properties"]),
            )
            return target

        if func is not None:
            return register(func)
        return register

    # ---- Introspection ----

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(
                f"Unknown tool: {name}. Available tools: {', '.join(self.names()) or 'none'}"
            ) from None

    def schemas(self) -> list[dict]:
        """Every tool's schema, in the array shape the chat API expects."""
        return [self._tools[name].schema for name in self.names()]

    # ---- Dispatch ----

    def dispatch(self, name: str, raw_arguments: str | dict | None = None) -> str:
        """Run a tool by name and return its result as a JSON string.

        Results are serialized here so that every tool body can return an
        ordinary Python object rather than remembering to encode itself --
        which the previous handlers each did by hand, inconsistently.
        """
        tool = self.get(name)
        arguments = tool.validate(_parse_arguments(raw_arguments, name))

        try:
            result = tool(**arguments)
        except (ToolArgumentError, UnknownToolError):
            raise
        except Exception as exc:
            raise ToolExecutionError(f"Tool {name!r} failed: {exc}") from exc

        return result if isinstance(result, str) else serialize_json(result, ensure_ascii=False)


def _argument_validator(func: Callable[..., Any], name: str) -> Any:
    """A pydantic model mirroring `func`'s parameters, used to validate input."""
    hints = typing.get_type_hints(func)
    fields: dict[str, Any] = {}
    for param_name, parameter in inspect.signature(func).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[param_name] = (hints.get(param_name, str), default)

    return create_model(f"{name}_Validator", **fields)


def _parse_arguments(raw_arguments: str | dict | None, tool_name: str) -> dict:
    """Normalise the model's tool arguments into a dict."""
    if raw_arguments in (None, ""):
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = parse_json(raw_arguments)
        except JSONDecodeError as exc:
            raise ToolArgumentError(
                f"Arguments for {tool_name!r} were not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ToolArgumentError(
                f"Arguments for {tool_name!r} must be a JSON object, got {type(parsed).__name__}"
            )
        return parsed

    raise ToolArgumentError(f"Unsupported argument type for {tool_name!r}: {type(raw_arguments)!r}")


# The registry the built-in tools attach to. Importing minus.tools populates it.
registry = ToolRegistry()
