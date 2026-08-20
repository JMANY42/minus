"""Grouping the tiers' registries, and the stored line that switches tools off.

The registry's own switch is test_tool_registry.py's; this is the layer above
it: which agent a tool belongs to, and how a spec written to .env yesterday
reaches the right registry today.
"""

from __future__ import annotations

import pytest

from minus.tools.policy import CODING_NOTE, ToolPolicy, UnknownAgentError, build_policy, parse_spec
from minus.tools.registry import ToolRegistry


def registry_of(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        # Named at call time, so one helper covers both tiers' toolsets.
        def tool(path: str = ".") -> str:
            """A tool."""
            return path

        registry.tool(tool, name=name)
    return registry


@pytest.fixture
def policy() -> ToolPolicy:
    return build_policy(
        conversational=registry_of("escalate", "read_workspace_file"),
        deep=registry_of("read_workspace_file"),
    )


class TestDescribing:
    def test_every_agent_is_listed_in_order(self, policy):
        assert [agent["key"] for agent in policy.describe()] == [
            "conversational",
            "deep",
            "coding",
        ]

    def test_the_agent_that_does_not_exist_yet_says_so(self, policy):
        coding = policy.describe()[-1]

        assert coding["tools"] == []
        assert coding["note"] == CODING_NOTE

    def test_each_tool_carries_its_description_and_its_switch(self, policy):
        tools = policy.describe()[0]["tools"]

        assert [tool["name"] for tool in tools] == ["escalate", "read_workspace_file"]
        assert all(tool["enabled"] for tool in tools)
        assert tools[0]["description"] == "A tool."


class TestSwitching:
    def test_it_switches_the_named_agents_copy_only(self, policy):
        policy.set_enabled("deep", "read_workspace_file", False)

        assert policy.agent("deep").registry.enabled("read_workspace_file") is False
        assert policy.agent("conversational").registry.enabled("read_workspace_file") is True

    def test_an_unknown_agent_is_refused(self, policy):
        with pytest.raises(UnknownAgentError):
            policy.set_enabled("nonesuch", "escalate", False)

    def test_an_agent_with_no_registry_is_refused_with_the_reason(self, policy):
        with pytest.raises(UnknownAgentError, match=CODING_NOTE):
            policy.set_enabled("coding", "escalate", False)


class TestTheStoredSpec:
    def test_only_the_switched_off_ones_are_written(self, policy):
        policy.set_enabled("deep", "read_workspace_file", False)

        assert policy.spec() == "deep:read_workspace_file"

    def test_an_untouched_policy_writes_nothing(self, policy):
        assert policy.spec() == ""

    def test_it_round_trips(self, policy):
        policy.set_enabled("conversational", "escalate", False)
        policy.set_enabled("deep", "read_workspace_file", False)
        spec = policy.spec()

        fresh = build_policy(
            conversational=registry_of("escalate", "read_workspace_file"),
            deep=registry_of("read_workspace_file"),
        )
        fresh.apply_spec(spec)

        assert fresh.spec() == spec

    def test_applying_a_spec_switches_everything_it_omits_back_on(self, policy):
        """Authoritative, so a tool dropped from the line is on again."""
        policy.set_enabled("conversational", "escalate", False)

        policy.apply_spec("")

        assert policy.agent("conversational").registry.disabled == []

    def test_a_stale_entry_is_dropped_rather_than_fatal(self, policy):
        """A .env may name a tool a later build removed; that must still start."""
        policy.apply_spec("conversational:gone,nosuch:escalate,deep:read_workspace_file")

        assert policy.agent("deep").registry.disabled == ["read_workspace_file"]

    def test_nothing_at_all_is_a_spec_too(self, policy):
        policy.apply_spec(None)

        assert policy.spec() == ""


class TestParsingASpec:
    def test_it_reads_agent_and_tool_pairs(self):
        assert parse_spec("a:one,b:two") == [("a", "one"), ("b", "two")]

    def test_whitespace_and_empty_entries_are_forgiven(self):
        """Hand-edited .env lines have trailing commas and stray spaces."""
        assert parse_spec(" a:one , , b:two,") == [("a", "one"), ("b", "two")]

    def test_an_entry_with_no_agent_is_dropped(self):
        assert parse_spec("one,b:two") == [("b", "two")]
