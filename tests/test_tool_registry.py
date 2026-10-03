"""Tests for schema derivation, dispatch and the workspace path guard."""

from __future__ import annotations

import json

import pytest

from minus.errors import (
    ToolArgumentError,
    ToolDisabledError,
    ToolExecutionError,
    UnknownToolError,
    WorkspacePathError,
)
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


class TestSubset:
    """Scoping the registry is how the two model tiers get different tools."""

    def _two_tools(self) -> ToolRegistry:
        registry = ToolRegistry()

        @registry.tool
        def alpha() -> str:
            """Alpha tool."""
            return "a"

        @registry.tool
        def beta() -> str:
            """Beta tool."""
            return "b"

        return registry

    def test_keeps_only_the_named_tools(self):
        scoped = self._two_tools().subset(["alpha"])

        assert scoped.names() == ["alpha"]
        assert "beta" not in scoped

    def test_the_original_registry_is_untouched(self):
        registry = self._two_tools()
        registry.subset(["alpha"])

        assert registry.names() == ["alpha", "beta"]

    def test_a_scoped_tool_still_dispatches(self):
        scoped = self._two_tools().subset(["alpha"])

        assert "a" in scoped.dispatch("alpha")

    def test_an_unknown_name_fails_loudly(self):
        # A typo in the tier wiring should break at startup rather than
        # silently handing a model a smaller toolset than intended.
        with pytest.raises(UnknownToolError):
            self._two_tools().subset(["alpah"])

    def test_a_bound_method_registers_with_a_derived_schema(self):
        # This is how the stateful `escalate` tool reaches the fast tier: it
        # carries a thinker with it, and `self` must not leak into the schema.
        class Thinker:
            def escalate(self, question: str) -> dict:
                """Hand a question to a slower model.

                Args:
                    question: The full question to think about.
                """
                return {"question": question}

        scoped = self._two_tools().subset([])
        scoped.tool(Thinker().escalate)

        schema = scoped.schemas()[0]["function"]
        assert schema["name"] == "escalate"
        assert "self" not in schema["parameters"]["properties"]
        assert schema["parameters"]["required"] == ["question"]


def _two_tools() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool
    def alpha() -> str:
        """Alpha tool."""
        return "a"

    @registry.tool
    def beta() -> str:
        """Beta tool."""
        return "b"

    return registry


class TestSwitchingToolsOff:
    """The dashboard's tools panel, at the level the registry sees it."""

    def test_a_switched_off_tool_is_not_offered_to_the_model(self):
        registry = _two_tools()

        registry.set_enabled("beta", False)

        assert [schema["function"]["name"] for schema in registry.schemas()] == ["alpha"]

    def test_it_is_still_registered_so_it_can_come_back(self):
        registry = _two_tools()
        registry.set_enabled("beta", False)

        assert "beta" in registry
        assert registry.names() == ["alpha", "beta"]
        assert registry.enabled("beta") is False

    def test_switching_it_back_on_restores_it(self):
        registry = _two_tools()
        registry.set_enabled("beta", False)

        registry.set_enabled("beta", True)

        assert registry.enabled("beta") is True
        assert len(registry.schemas()) == 2

    def test_calling_a_switched_off_tool_is_refused(self):
        """A model that invents the call gets an answer it can act on."""
        registry = _two_tools()
        registry.set_enabled("beta", False)

        with pytest.raises(ToolDisabledError):
            registry.dispatch("beta")

    def test_switching_an_unknown_tool_fails_loudly(self):
        with pytest.raises(UnknownToolError):
            _two_tools().set_enabled("gamma", False)

    def test_describe_lists_every_tool_with_its_switch(self):
        registry = _two_tools()
        registry.set_enabled("beta", False)

        described = registry.describe()

        assert [tool["name"] for tool in described] == ["alpha", "beta"]
        assert [tool["enabled"] for tool in described] == [True, False]
        assert described[0]["description"] == "Alpha tool."

    def test_a_tool_is_described_under_the_category_it_registered_with(self):
        """What the dashboard folders by, and the only place it is written."""
        registry = ToolRegistry()

        @registry.tool(category="  Google / Calendar ")
        def add_event() -> str:
            """Add an event."""
            return "ok"

        @registry.tool
        def get_current_time() -> str:
            """What time it is."""
            return "now"

        described = {tool["name"]: tool["category"] for tool in registry.describe()}

        # Cased and spaced once, so two spellings of one heading cannot draw
        # two folders. An uncategorised tool says so with an empty string
        # rather than being filed under a guess.
        assert described == {"add_event": "google/calendar", "get_current_time": ""}

    def test_a_tool_keeps_its_category_in_a_tier_it_was_given_to(self):
        registry = ToolRegistry()

        @registry.tool(category="files")
        def read_file() -> str:
            """Read a file."""
            return "ok"

        assert registry.subset(["read_file"]).describe()[0]["category"] == "files"

    def test_set_disabled_is_the_whole_state_at_once(self):
        """Applying a stored spec cannot leave something off that it omits."""
        registry = _two_tools()
        registry.set_enabled("alpha", False)

        registry.set_disabled(["beta"])

        assert registry.disabled == ["beta"]

    def test_set_disabled_rejects_a_name_that_is_not_registered(self):
        with pytest.raises(UnknownToolError):
            _two_tools().set_disabled(["gamma"])

    def test_a_subset_inherits_the_switch_but_not_the_switching(self):
        """One Tool object, two tiers: switching it in one must not move the other."""
        registry = _two_tools()
        registry.set_enabled("beta", False)

        scoped = registry.subset(["alpha", "beta"])
        scoped.set_enabled("beta", True)

        assert scoped.enabled("beta") is True
        assert registry.enabled("beta") is False
