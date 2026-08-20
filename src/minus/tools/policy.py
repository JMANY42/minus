"""Which agent holds which tools, and which of those are switched on.

The registries already answer "what can this tier call" -- `assembly` builds
one for the conversation and one for the deep tier, and `subset` is what keeps
them apart. What was missing is a name for the collection of them: something a
dashboard can list by agent, and something a stored line in .env can be applied
to on the way up.

That is all this is. It holds the tiers in display order, hands out a
description of each, and turns one tool on or off in the tier that owns it. The
registries stay the source of truth about what is switched off; this only knows
where they are.

An agent with no registry is a tier that does not exist yet -- the coding agent
is the one this was written for. It is listed with the reason its list is
empty, rather than left out and wondered about, which is the same bargain the
external-programs panel already strikes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from minus.errors import MinusError
from minus.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# The keys are what a stored spec and the control socket say; the titles are
# what the dashboard draws. Kept apart so renaming the label on screen cannot
# invalidate a .env written by yesterday's build.
CONVERSATIONAL = "conversational"
DEEP = "deep"
CODING = "coding"

TITLES = {
    CONVERSATIONAL: "conversational",
    DEEP: "deep think",
    CODING: "coding",
}

# Why the coding agent's list is empty. There is no coding tier in MINUS at
# all yet; this is the seat kept warm for it, so that the panel does not
# change shape when it arrives -- only where its rows come from.
CODING_NOTE = "not implemented yet"


class UnknownAgentError(MinusError):
    """A tool was asked for on behalf of an agent that does not exist."""


@dataclass(frozen=True)
class AgentTools:
    """One tier's toolset, as something outside the process sees it."""

    key: str
    title: str
    registry: ToolRegistry | None = None
    # Filled in only when there is no registry: the reason there is none.
    note: str = ""

    def describe(self) -> dict:
        tools = self.registry.describe() if self.registry is not None else []
        return {"key": self.key, "title": self.title, "note": self.note, "tools": tools}


class ToolPolicy:
    """Every agent's tools, and the switch on each one."""

    def __init__(self, agents: Sequence[AgentTools]) -> None:
        self.agents = tuple(agents)

    # ---- Reading ----

    def describe(self) -> list[dict]:
        return [agent.describe() for agent in self.agents]

    def agent(self, key: str) -> AgentTools:
        for agent in self.agents:
            if agent.key == key:
                return agent
        known = ", ".join(agent.key for agent in self.agents) or "none"
        raise UnknownAgentError(f"Unknown agent: {key}. Known agents: {known}")

    # ---- Writing ----

    def set_enabled(self, agent_key: str, tool: str, enabled: bool) -> dict:
        """Switch one tool on or off for one agent, and report what it now is."""
        agent = self.agent(agent_key)
        if agent.registry is None:
            raise UnknownAgentError(f"The {agent.title} agent has no tools: {agent.note}")

        agent.registry.set_enabled(tool, enabled)
        return {"agent": agent.key, "tool": tool, "enabled": enabled, "spec": self.spec()}

    # ---- Persistence ----
    #
    # One string, because that is what `Settings` fields are and what .env can
    # hold: "conversational:escalate,deep:read_workspace_file". Only the
    # switched-off ones are written, so a tool added to a later build arrives
    # switched on rather than absent from a list of everything that was on.

    def spec(self) -> str:
        entries = [
            f"{agent.key}:{name}"
            for agent in self.agents
            if agent.registry is not None
            for name in agent.registry.disabled
        ]
        return ",".join(entries)

    def apply_spec(self, spec: str | None) -> None:
        """Switch off exactly what `spec` names, and everything else on.

        Tolerant on the way in, and deliberately so: this runs at startup
        against a file a human may have edited and an older build may have
        written. An entry naming an agent or a tool that is not there is logged
        and dropped -- refusing to start over a stale line would make removing
        a tool a breaking change to every .env that mentioned it.
        """
        wanted: dict[str, set[str]] = {}
        for entry in parse_spec(spec):
            agent_key, tool = entry
            try:
                agent = self.agent(agent_key)
            except UnknownAgentError:
                logger.warning("Ignoring disabled tool %r: no such agent %r", tool, agent_key)
                continue
            if agent.registry is None or tool not in agent.registry:
                logger.warning("Ignoring disabled tool %r: %s cannot call it", tool, agent.key)
                continue
            wanted.setdefault(agent.key, set()).add(tool)

        for agent in self.agents:
            if agent.registry is not None:
                agent.registry.set_disabled(wanted.get(agent.key, set()))


def parse_spec(spec: str | None) -> list[tuple[str, str]]:
    """`"conversational:escalate, deep:read_workspace_file"` as pairs.

    Whitespace and empty entries are ignored, so a hand-edited .env line with
    a trailing comma or a space after one still means what it looks like.
    """
    pairs: list[tuple[str, str]] = []
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        agent_key, separator, tool = entry.partition(":")
        if not separator or not agent_key.strip() or not tool.strip():
            logger.warning("Ignoring malformed disabled-tool entry %r", entry)
            continue
        pairs.append((agent_key.strip(), tool.strip()))
    return pairs


def build_policy(conversational=None, deep=None, coding=None) -> ToolPolicy:
    """The three tiers, in the order a reader meets them.

    Every agent is listed whether or not it has a registry behind it, which is
    what lets the dashboard draw the coding agent's empty list today and fill
    it in later without a change on either side.
    """
    return ToolPolicy(
        [
            AgentTools(CONVERSATIONAL, TITLES[CONVERSATIONAL], conversational),
            AgentTools(DEEP, TITLES[DEEP], deep),
            AgentTools(CODING, TITLES[CODING], coding, note="" if coding else CODING_NOTE),
        ]
    )


__all__ = [
    "CODING",
    "CONVERSATIONAL",
    "DEEP",
    "AgentTools",
    "ToolPolicy",
    "UnknownAgentError",
    "build_policy",
    "parse_spec",
]
