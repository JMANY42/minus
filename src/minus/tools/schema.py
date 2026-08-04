"""Derive OpenAI tool schemas from ordinary Python functions.

Tool schemas used to live in a hand-written tools.json while the
implementations lived in an if/else chain in tool_handler.py. Nothing tied the
two together, so a renamed parameter or a new required field silently produced
a schema that no longer described the function -- and the model would be told
about a tool that could not actually be called that way.

Here the function *is* the schema. Parameter names, types, defaults and
required-ness come from the signature; descriptions come from a Google-style
`Args:` block in the docstring.
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable
from typing import Any

from pydantic import create_model

_ARGS_HEADING = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$", re.IGNORECASE)
_OTHER_HEADING = re.compile(r"^\s*(Returns|Raises|Yields|Examples?|Notes?)\s*:\s*$", re.IGNORECASE)
# "name: description" or "name (type): description"
_ARG_LINE = re.compile(r"^\s*(?P<name>\*{0,2}\w+)\s*(\([^)]*\))?\s*:\s*(?P<desc>.+?)\s*$")


def split_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """Split a docstring into its summary and its per-argument descriptions."""
    if not doc:
        return "", {}

    lines = inspect.cleandoc(doc).splitlines()
    summary_lines: list[str] = []
    arg_descriptions: dict[str, str] = {}

    in_args = False
    current: str | None = None

    for line in lines:
        if _ARGS_HEADING.match(line):
            in_args = True
            current = None
            continue
        if _OTHER_HEADING.match(line):
            in_args = False
            current = None
            continue

        if not in_args:
            summary_lines.append(line)
            continue

        match = _ARG_LINE.match(line)
        if match:
            current = match.group("name").lstrip("*")
            arg_descriptions[current] = match.group("desc")
        elif current and line.strip():
            # Continuation of the previous argument's description.
            arg_descriptions[current] = f"{arg_descriptions[current]} {line.strip()}"

    summary = " ".join(part.strip() for part in " ".join(summary_lines).split()).strip()
    return summary, arg_descriptions


def build_parameters_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Build the JSON Schema for a function's parameters."""
    signature = inspect.signature(func)
    hints = typing.get_type_hints(func)
    _, arg_descriptions = split_docstring(func.__doc__)

    fields: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            # *args/**kwargs cannot be described by a JSON Schema object.
            continue

        annotation = hints.get(name, str)
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (annotation, default)

    model = create_model(f"{func.__name__}_Arguments", **fields)
    schema = model.model_json_schema()

    properties = schema.get("properties", {})
    for name, description in arg_descriptions.items():
        if name in properties:
            properties[name]["description"] = description

    # `title` keys are pydantic bookkeeping and only add prompt noise.
    schema.pop("title", None)
    for prop in properties.values():
        prop.pop("title", None)

    return {
        "type": "object",
        "properties": properties,
        "required": schema.get("required", []),
        "additionalProperties": False,
    }


def build_tool_schema(func: Callable[..., Any], name: str, description: str | None = None) -> dict:
    """Build the full OpenAI function-tool schema for `func`."""
    summary, _ = split_docstring(func.__doc__)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or summary,
            "parameters": build_parameters_schema(func),
        },
    }
