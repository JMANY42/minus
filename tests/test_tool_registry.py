"""Tests for schema derivation, dispatch and the workspace path guard."""

from __future__ import annotations

import json

import pytest

from minus.errors import ToolArgumentError, ToolExecutionError, UnknownToolError, WorkspacePathError
from minus.tools.registry import ToolRegistry
from minus.tools.schema import split_docstring
from minus.tools.workspace import resolve_workspace_path


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


class TestSchemaDerivation:
    def test_schema_comes_from_signature_and_docstring(self, registry):
        @registry.tool
        def set_light(room: str, brightness: int = 100) -> str:
            """Set a room's light brightness.

            Args:
                room: Room name, e.g. "office".
                brightness: Brightness from 0 to 100.
            """
            return "ok"

        (schema,) = registry.schemas()
        function = schema["function"]
        properties = function["parameters"]["properties"]

        assert schema["type"] == "function"
        assert function["name"] == "set_light"
        assert function["description"] == "Set a room's light brightness."
        assert properties["room"]["type"] == "string"
        assert properties["room"]["description"] == 'Room name, e.g. "office".'
        assert properties["brightness"]["type"] == "integer"
        assert properties["brightness"]["default"] == 100
        # A parameter with a default is optional; one without is required.
        assert function["parameters"]["required"] == ["room"]

    def test_multiline_argument_descriptions_are_joined(self):
        summary, args = split_docstring(
            """Do a thing.

            Args:
                path: A path that has
                    a wrapped description.
            """
        )
        assert summary == "Do a thing."
        assert args["path"] == "A path that has a wrapped description."

    def test_no_argument_tool_produces_empty_properties(self, registry):
        @registry.tool
        def ping() -> str:
            """Check liveness."""
            return "pong"

        (schema,) = registry.schemas()
        assert schema["function"]["parameters"]["properties"] == {}
        assert schema["function"]["parameters"]["required"] == []

    def test_duplicate_registration_is_rejected(self, registry):
        @registry.tool
        def dupe() -> str:
            """First."""
            return "a"

        with pytest.raises(ValueError, match="already registered"):

            @registry.tool
            def dupe() -> str:
                """Second."""
                return "b"


class TestDispatch:
    def test_dispatch_validates_and_calls(self, registry):
        seen = {}

        @registry.tool
        def echo(text: str, times: int = 1) -> dict:
            """Echo text.

            Args:
                text: What to say.
                times: How many times.
            """
            seen["args"] = (text, times)
            return {"said": text * times}

        result = registry.dispatch("echo", '{"text": "hi", "times": 2}')

        assert seen["args"] == ("hi", 2)
        assert json.loads(result) == {"said": "hihi"}

    def test_defaults_apply_when_omitted(self, registry):
        @registry.tool
        def greet(name: str = "world") -> str:
            """Greet someone.

            Args:
                name: Who to greet.
            """
            return f"hello {name}"

        assert registry.dispatch("greet", None) == "hello world"
        assert registry.dispatch("greet", "") == "hello world"
        assert registry.dispatch("greet", {"name": "minus"}) == "hello minus"

    def test_unknown_tool_names_the_available_ones(self, registry):
        @registry.tool
        def known() -> str:
            """Known."""
            return "y"

        with pytest.raises(UnknownToolError, match="known"):
            registry.dispatch("nonexistent", "{}")

    def test_malformed_json_arguments_are_rejected(self, registry):
        @registry.tool
        def anything(value: str = "x") -> str:
            """Anything.

            Args:
                value: A value.
            """
            return value

        with pytest.raises(ToolArgumentError, match="not valid JSON"):
            registry.dispatch("anything", "{not json")

    def test_wrong_argument_type_is_rejected_before_the_body_runs(self, registry):
        ran = []

        @registry.tool
        def typed(count: int) -> str:
            """Typed.

            Args:
                count: A number.
            """
            ran.append(count)
            return "ok"

        with pytest.raises(ToolArgumentError):
            registry.dispatch("typed", '{"count": "not-a-number"}')
        assert ran == []

    def test_body_failures_surface_as_tool_execution_error(self, registry):
        @registry.tool
        def explodes() -> str:
            """Explode."""
            raise RuntimeError("boom")

        with pytest.raises(ToolExecutionError, match="boom"):
            registry.dispatch("explodes", "{}")


class TestWorkspaceGuard:
    def test_relative_paths_resolve_inside_the_workspace(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hi")
        assert resolve_workspace_path("notes.txt", root=tmp_path) == tmp_path / "notes.txt"

    def test_parent_traversal_is_rejected(self, tmp_path):
        with pytest.raises(WorkspacePathError, match="escapes"):
            resolve_workspace_path("../../etc/passwd", root=tmp_path)

    def test_absolute_paths_are_rejected(self, tmp_path):
        with pytest.raises(WorkspacePathError, match="workspace-relative"):
            resolve_workspace_path("/etc/passwd", root=tmp_path)

    def test_symlink_pointing_outside_is_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(outside)

        # Resolution happens before the containment check, so a symlink cannot
        # be used to step out of the workspace.
        with pytest.raises(WorkspacePathError, match="escapes"):
            resolve_workspace_path("link.txt", root=tmp_path)
