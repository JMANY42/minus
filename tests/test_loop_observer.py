"""The observer events a harness draws its thinking bar from.

The loop's tool events are covered by the harness tests; this pins the model
side: a round announces that it is thinking, a provider-supplied reasoning
trace is surfaced once, and the final answer closes the cycle.
"""

from __future__ import annotations

from types import SimpleNamespace

from minus.core.loop import ToolLoop
from minus.core.messages import Transcript


class ScriptedModel:
    def __init__(self, completions):
        self.completions = list(completions)
        self.calls: list[dict] = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.completions.pop(0)


class FakeTools:
    def schemas(self):
        return []

    def dispatch(self, name, arguments):
        return '{"ok": true}'


def _completion(content: str, *, reasoning: str = "", tool_calls=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        reasoning_content=reasoning,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_thinking_thought_and_answer_events_are_emitted_in_order():
    call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(
            name="read_workspace_file",
            arguments='{"path": "notes.txt"}',
        ),
    )
    model = ScriptedModel(
        [
            _completion("", reasoning="I need to read the notes.", tool_calls=[call]),
            _completion("done", reasoning="Now I can answer."),
        ]
    )
    events: list[tuple[str, dict]] = []
    loop = ToolLoop(
        model=model,
        transcript=Transcript(),
        tools=FakeTools(),
        max_tool_rounds=3,
        observer=lambda event, payload: events.append((event, dict(payload))),
    )

    answer = loop.run()

    assert answer.content == "done"
    names = [event for event, _ in events]
    assert names == [
        "thinking",
        "thought",
        "tool_start",
        "tool_end",
        "thinking",
        "thought",
        "answer",
    ]
    assert events[1][1]["text"] == "I need to read the notes."
    assert events[3][1]["name"] == "read_workspace_file"
    assert events[6][1]["text"] == "done"


def test_a_model_without_reasoning_emits_no_thought_event():
    model = ScriptedModel([_completion("plain answer")])
    events: list[tuple[str, dict]] = []
    loop = ToolLoop(
        model=model,
        transcript=Transcript(),
        tools=FakeTools(),
        observer=lambda event, payload: events.append((event, dict(payload))),
    )

    loop.run()

    assert [event for event, _ in events] == ["thinking", "answer"]
