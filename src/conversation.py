import json
import logging

from response import generate_response
from logging_utils import pretty_json
from tool_handler import ToolHandler


logger = logging.getLogger(__name__)


class Conversation:
    def __init__(self, tools_path=None, max_tool_rounds=5):
        self.messages = []
        self.tool_handler = ToolHandler(tools_path=tools_path)
        self.tools = self.tool_handler.load_tools()
        self.max_tool_rounds = max_tool_rounds

    def reply(self, transcript):
        user_message = {"role": "user", "content": transcript}
        self.messages.append(user_message)
        logger.debug("New conversation message:\n%s", pretty_json(user_message))
        seen_tool_calls = set()
        completed_tool_rounds = 0

        while completed_tool_rounds < self.max_tool_rounds:
            completion = generate_response(self.messages, tools=self.tools)
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []

            assistant_message = {"role": "assistant", "content": message.content}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": tool_call.type,
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                    for tool_call in tool_calls
                ]

            self.messages.append(assistant_message)
            logger.debug("New conversation message:\n%s", pretty_json(assistant_message))

            if not tool_calls:
                return (message.content or "").strip()

            tool_round_failed = False

            for tool_call in tool_calls:
                tool_name = tool_call.function.name
                raw_arguments = tool_call.function.arguments
                signature = json.dumps(
                    {"name": tool_name, "arguments": self._normalize_tool_arguments(raw_arguments)},
                    sort_keys=True,
                    default=str,
                )

                if signature in seen_tool_calls:
                    error_message = (
                        f"Tool call {tool_name!r} was already executed with these arguments and its result is already available. "
                        "Do not repeat it; use the existing result to answer the user."
                    )
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": error_message,
                    }
                    self.messages.append(tool_message)
                    logger.debug("New conversation message:\n%s", pretty_json(tool_message))
                    tool_round_failed = True
                    continue

                seen_tool_calls.add(signature)

                try:
                    result = self.tool_handler.execute(tool_name, raw_arguments)
                    logger.debug("Tool result for %s:\n%s", tool_name, pretty_json(result))
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                    self.messages.append(tool_message)
                    logger.debug("New conversation message:\n%s", pretty_json(tool_message))
                except (ValueError, TypeError, OSError) as exc:
                    error_message = (
                        f"Tool execution failed for {tool_name!r}: {exc}. "
                        "Please retry with a valid workspace-relative path or corrected arguments."
                    )
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": error_message,
                    }
                    self.messages.append(tool_message)
                    logger.debug("New conversation message:\n%s", pretty_json(tool_message))
                    tool_round_failed = True
                    break

            if not tool_round_failed:
                completed_tool_rounds += 1

        raise RuntimeError("Tool call limit reached before the model produced a final response.")

    def _normalize_tool_arguments(self, raw_arguments):
        if raw_arguments in (None, ""):
            return {}
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str):
            try:
                return json.loads(raw_arguments)
            except json.JSONDecodeError:
                return {"raw": raw_arguments}

        return {"value": raw_arguments}