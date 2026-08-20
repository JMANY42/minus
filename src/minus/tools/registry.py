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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError, create_model

from minus.errors import (
    ToolArgumentError,
    ToolDisabledError,
    ToolExecutionError,
    UnknownToolError,
)
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
    # Where a reader files this tool: "google calendar", or "google/sheets" for
    # a nested one. Declared at registration beside everything else about the
    # tool, so there is no second list of what belongs where to fall out of
    # date when a tool is added. Empty means uncategorised, which is a real
    # answer -- `get_current_time` belongs under no heading -- and the
    # dashboard draws those loose rather than inventing a "misc" folder.
    category: str = ""

    def validate(self, arguments: dict) -> dict:
        try:
            return self.validator(**arguments).model_dump()
        except ValidationError as exc:
            raise ToolArgumentError(f"Invalid arguments for {self.name!r}: {exc}") from exc

    def __call__(self, **kwargs: Any) -> Any:
        return self.func(**kwargs)


class ToolRegistry:
    """A named collection of tools, with schema generation and dispatch.

    A registry also holds which of its tools are switched off. That lives here
    rather than on `Tool` because the same `read_workspace_file` object is
    shared by every tier that was given it -- see `subset` -- and switching it
    off for the deep tier must not take it away from the conversation. A
    registry is the smallest thing that is per-tier, so it is where the switch
    belongs.

    Disabling hides a tool rather than removing it: it is still registered,
    still listed by `names()`, and still there to be switched back on. What
    changes is that it is no longer offered to the model and no longer runs.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()

    def tool(
        self,
        func: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        category: str = "",
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
                category=normalize_category(category),
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
        """Every registered tool, switched on or not."""
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(
                f"Unknown tool: {name}. Available tools: {', '.join(self.names()) or 'none'}"
            ) from None

    def enabled(self, name: str) -> bool:
        self.get(name)
        return name not in self._disabled

    @property
    def disabled(self) -> list[str]:
        """The switched-off tools, sorted. Only ever names that are registered."""
        return sorted(self._disabled & set(self._tools))

    def active_names(self) -> list[str]:
        return [name for name in self.names() if name not in self._disabled]

    def schemas(self) -> list[dict]:
        """Every enabled tool's schema, in the array shape the chat API expects.

        The enabled ones only: this is what the model is told it can call, and
        offering a tool that `dispatch` would then refuse would be inviting a
        failure rather than preventing one.
        """
        return [self._tools[name].schema for name in self.active_names()]

    def describe(self) -> list[dict]:
        """Every tool, whether it is on, and what it does.

        For a reader rather than for a model: `schemas()` is deliberately
        missing the switched-off ones, so anything offering to switch them back
        on has to ask for the whole list somewhere.
        """
        return [
            {
                "name": name,
                "description": self._tools[name].schema["function"].get("description", ""),
                "enabled": name not in self._disabled,
                "category": self._tools[name].category,
            }
            for name in self.names()
        ]

    # ---- Switching tools on and off ----

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Switch one tool on or off for this registry.

        Raises UnknownToolError for a name that is not registered, so a stale
        instruction -- a disabled tool named in .env that has since been
        deleted, a typo over the control socket -- is answered rather than
        remembered as a disablement of nothing.
        """
        self.get(name)
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def set_disabled(self, names: Iterable[str]) -> None:
        """Switch off exactly these, and switch everything else on.

        The whole state in one call, which is what makes a stored list of
        disabled tools authoritative: applying it can never leave something
        switched off that the list no longer mentions.
        """
        wanted = set(names)
        unknown = wanted - set(self._tools)
        if unknown:
            raise UnknownToolError(f"Unknown tool(s): {', '.join(sorted(unknown))}")
        self._disabled = wanted

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        """A new registry holding only `names`, sharing this one's Tools.

        Which tier may call which tool is a composition-root decision, not a
        property of the tool itself: the same `read_workspace_file` is offered
        to both the conversational model and the deep tier. Expressing the
        split here keeps `Tool` free of tier flags that would have to be kept
        in sync with the wiring.

        Raises UnknownToolError for a name that is not registered, so a typo in
        the wiring fails at startup rather than silently shrinking a tier.
        """
        scoped = ToolRegistry()
        for name in names:
            scoped._tools[name] = self.get(name)
        # Carried over, then owned separately: a tool switched off where it was
        # taken from arrives switched off, and switching it on here leaves the
        # source registry alone.
        scoped._disabled = self._disabled & set(scoped._tools)
        return scoped

    # ---- Dispatch ----

    def dispatch(self, name: str, raw_arguments: str | dict | None = None) -> str:
        """Run a tool by name and return its result as a JSON string.

        Results are serialized here so that every tool body can return an
        ordinary Python object rather than remembering to encode itself --
        which the previous handlers each did by hand, inconsistently.
        """
        tool = self.get(name)
        if name in self._disabled:
            # Not offered in `schemas()`, so this is a model calling something
            # it was never told about -- or one that was switched off between
            # the schemas being sent and the call coming back. Refused as a
            # tool error, which the loop turns into a result the model can read
            # and act on rather than an exception that ends the turn.
            raise ToolDisabledError(f"Tool {name!r} is switched off and cannot be called.")
        arguments = tool.validate(_parse_arguments(raw_arguments, name))

        try:
            result = tool(**arguments)
        except (ToolArgumentError, UnknownToolError):
            raise
        except Exception as exc:
            raise ToolExecutionError(f"Tool {name!r} failed: {exc}") from exc

        return result if isinstance(result, str) else serialize_json(result, ensure_ascii=False)


def normalize_category(category: str) -> str:
    """`" Google / Calendar "` as `"google/calendar"`.

    Categories are typed by hand at each registration site and compared by the
    dashboard when it groups them, so two spellings of the same heading would
    draw two folders. Cased and spaced once here rather than at every reader.
    """
    parts = [part.strip().lower() for part in (category or "").split("/")]
    return "/".join(part for part in parts if part)


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
