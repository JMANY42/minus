import logging
from dataclasses import asdict, is_dataclass

from response import generate_response
from memory import ConversationMemory
from services.json import pretty_json
from tool_handler import ToolHandler


logger = logging.getLogger(__name__)


def create_user_message(transcript):
    return {"role": "user", "content": transcript}


def create_assistant_message(message):
    assistant_message = {"role": "assistant", "content": message.content}

    tool_calls = getattr(message, "tool_calls", None) or []
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

    return assistant_message

def _make_json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {key: _make_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_json_safe(item) for item in value]
    if is_dataclass(value):
        return _make_json_safe(asdict(value))
    if hasattr(value, "__dict__"):
        return {key: _make_json_safe(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


def create_fact_message(facts):
    return {"role": "user", "content": _make_json_safe(facts)}

def create_tool_message(tool_call_id, content):
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def append_message(messages, message, memory=None):
    serializable_message = _make_json_safe(message)
    messages.append(serializable_message)
    logger.debug("New conversation message:\n%s", pretty_json(serializable_message))

    if memory is not None:
        memory.save(messages)


def execution_error_message(tool_name, exc):
    return (
        f"Tool execution failed for {tool_name!r}: {exc}. "
        "Please retry with a valid workspace-relative path or corrected arguments."
    )


def record_tool_error(messages, tool_call_id, content, memory=None):
    append_message(messages, create_tool_message(tool_call_id, content), memory=memory)


def execute_tool_call(tool_handler, tool_call):
    return tool_handler.execute(tool_call.function.name, tool_call.function.arguments)


def process_tool_call(conversation, tool_call):
    tool_name = tool_call.function.name
    try:
        result = execute_tool_call(conversation.tool_handler, tool_call)
        logger.debug("Tool result for %s:\n%s", tool_name, pretty_json(result))
        append_message(conversation.messages, create_tool_message(tool_call.id, result), memory=conversation.memory)
        return False
    except (ValueError, TypeError, OSError) as exc:
        record_tool_error(
            conversation.messages,
            tool_call.id,
            execution_error_message(tool_name, exc),
            memory=conversation.memory,
        )
        return True


def process_tool_calls(conversation, tool_calls):
    for tool_call in tool_calls:
        if process_tool_call(conversation, tool_call):
            return True

    return False


def build_reply(conversation, completion):
    message = completion.choices[0].message
    assistant_message = create_assistant_message(message)
    append_message(conversation.messages, assistant_message, memory=conversation.memory)

    tool_calls = getattr(message, "tool_calls", None) or []
    if not tool_calls:
        return (message.content or "").strip(), False

    tool_round_failed = process_tool_calls(conversation, tool_calls)
    return None, tool_round_failed


class Conversation:
    def __init__(self, tools_path=None, max_tool_rounds=5):
        self.messages = []
        self.memory = ConversationMemory()
        self.tool_handler = ToolHandler(tools_path=tools_path)
        self.tools = self.tool_handler.load_tools()
        self.max_tool_rounds = max_tool_rounds

    def reply(self, transcript):
        current_user_message = create_user_message(transcript)
        relevent_facts = [f.raw_text.strip() for f in self.memory.search_facts(current_user_message["content"], 5)]
        current_user_message["content"] += "\n\nRELEVANT FACTS:\n" + pretty_json(relevent_facts)
        print("current_user_message", current_user_message)
        append_message(self.messages, current_user_message, memory=self.memory)
        # append_message(self.messages, create_fact_message(relevent_facts), memory=self.memory)
        completed_tool_rounds = 0
        print("relevent facts", relevent_facts)

        while completed_tool_rounds < self.max_tool_rounds:
            completion = generate_response(self.messages, tools=self.tools)
            response_text, tool_round_failed = build_reply(self, completion)

            if response_text is not None:
                return response_text

            if not tool_round_failed:
                completed_tool_rounds += 1

        raise RuntimeError("Tool call limit reached before the model produced a final response.")

    def post_conversation(self):
        condensed_convo = self.memory.condense_conversation(self.messages)
        return self.memory.extract_and_store_semantic_memory(condensed_convo)