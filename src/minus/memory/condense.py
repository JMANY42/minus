import logging
from datetime import UTC, datetime
from pathlib import Path

from minus.services.json import write_json

logger = logging.getLogger(__name__)


def _filter_condensable_messages(messages):
    """Keep only user/assistant conversation turns; drop tool calls and tool results."""
    filtered = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue

        content = message.get("content")
        if not content:
            # Assistant turns that only carry tool_calls have no content to condense.
            continue

        filtered.append({"role": role, "content": content})

    return filtered


def _build_condensable_conversation(messages):
    # Lazy import avoids pulling in the LLM client module just to read the system prompt.
    from minus.core.prompts import SYSTEM_PROMPT

    return [{"role": "system", "content": SYSTEM_PROMPT}, *_filter_condensable_messages(messages)]


def condense_conversation(messages, conversation_id, source_conversation_file, condensed_base_dir):
    if not messages:
        logger.info("Skipping post-conversation condensation because no messages were recorded.")
        return None

    condensed_conversation = _build_condensable_conversation(messages)
    if len(condensed_conversation) <= 1:
        logger.info("Skipping post-conversation condensation because no user/assistant turns were recorded.")
        return None

    logger.info(
        "Condensed conversation %s: %d message(s) -> %d turn(s) (tool calls removed)",
        conversation_id, len(messages), len(condensed_conversation),
    )

    condensed_payload = {
        "conversation_id": conversation_id,
        "source_conversation_file": str(source_conversation_file),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "condensed_conversation": condensed_conversation,
    }
    condensed_path = Path(condensed_base_dir) / f"{conversation_id}.json"

    try:
        write_json(condensed_path, condensed_payload)
        logger.info("Saved condensed conversation to %s", condensed_path)
        return condensed_payload
    except OSError:
        logger.exception("Failed to persist condensed conversation to %s", condensed_path)
        return None
