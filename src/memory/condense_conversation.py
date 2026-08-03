import logging
from datetime import datetime, timezone
from pathlib import Path

from services.json import JSONDecodeError, parse_json, serialize_json, write_json


logger = logging.getLogger(__name__)
DEFAULT_CONDENSED_MEMORY_DIR = Path(__file__).resolve().parents[2] / "memory" / "condensed_conversations"
CONDENSE_MODEL = "llama-3.1-8b-instant"
CONDENSED_CONVERSATION_KEY = "conversation"


def _groq_call(**payload):
    # Lazy import keeps memory tests independent from Groq client module wiring.
    from services.groq import groq_call

    return groq_call(**payload)


def _build_condense_payload(messages):
    return {
        "model": CONDENSE_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You condense assistant conversations into a cleaned conversation transcript. "
                    "Return JSON only with this exact schema: "
                    "{\"conversation\": [{\"role\": \"user\"|\"assistant\", \"content\": string}]}. "
                    "Keep the back-and-forth format, but remove greetings, tool calls, "
                    "duplicate content, and repetitive statements. Preserve only the important dialogue. "
                    "Do not include any extra keys."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Condense this conversation transcript into a shorter conversation-format transcript. "
                    "Return valid JSON only, with no markdown fences:\n\n"
                    f"{serialize_json(messages, ensure_ascii=False)}"
                ),
            },
        ],
    }


def _normalize_condensed_json(parsed):
    if not isinstance(parsed, dict):
        raise ValueError("Condensed conversation payload must be a JSON object.")

    conversation = parsed.get(CONDENSED_CONVERSATION_KEY, [])
    if not isinstance(conversation, list):
        raise ValueError("Condensed conversation payload must contain a conversation list.")

    normalized_conversation = []
    last_item = None

    for item in conversation:
        if not isinstance(item, dict):
            continue

        role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue

        normalized_item = {"role": role, "content": content}
        if normalized_item == last_item:
            continue

        normalized_conversation.append(normalized_item)
        last_item = normalized_item

    if not normalized_conversation:
        raise ValueError("Condensed conversation payload must contain at least one user or assistant turn.")

    return normalized_conversation


def _parse_condensed_json(content):
    text = (content or "").strip()
    if not text:
        raise ValueError("Groq returned an empty condensed conversation.")

    parsed = parse_json(text)
    return _normalize_condensed_json(parsed)


def condense_conversation(messages, conversation_id, source_conversation_file, condensed_base_dir=DEFAULT_CONDENSED_MEMORY_DIR):
    if not messages:
        logger.info("Skipping post-conversation condensation because no messages were recorded.")
        return None

    payload = _build_condense_payload(messages)

    max_errors = 3
    for attempt in range(max_errors):
        try:
            completion = _groq_call(**payload)
            message = completion.choices[0].message
            condensed_json = _parse_condensed_json(getattr(message, "content", ""))
            break
        except JSONDecodeError:
            logger.exception("Groq returned invalid JSON for condensed conversation %s. Retrying... (%d/%d)", conversation_id, attempt, max_errors)
        except ValueError:
            logger.exception("Failed to condense conversation %s. Retrying... (%d/%d)", conversation_id, attempt, max_errors)
        except Exception:
            logger.exception("Failed to condense conversation %s. Retrying... (%d/%d)", conversation_id, attempt, max_errors)
    else:
        logger.error("Failed to condense conversation %s after %s attempts", conversation_id, max_errors)
        return None

    condensed_payload = {
        "conversation_id": conversation_id,
        "source_conversation_file": str(source_conversation_file),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "condensed_conversation": condensed_json,
    }
    condensed_path = Path(condensed_base_dir) / f"{conversation_id}.json"

    try:
        write_json(condensed_path, condensed_payload)
        logger.info("Saved condensed conversation to %s", condensed_path)
        return condensed_payload
    except OSError:
        logger.exception("Failed to persist condensed conversation to %s", condensed_path)
        return None
