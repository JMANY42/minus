import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from services.json import JSONDecodeError, parse_json, serialize_json, write_json


logger = logging.getLogger(__name__)
DEFAULT_MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory" / "conversations"
DEFAULT_CONDENSED_MEMORY_DIR = Path(__file__).resolve().parents[1] / "memory" / "condensed_conversations"
CONDENSE_MODEL = "llama-3.1-8b-instant"
CONDENSED_CONVERSATION_KEY = "conversation"


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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

    return {CONDENSED_CONVERSATION_KEY: normalized_conversation}


def _parse_condensed_json(content):
    text = (content or "").strip()
    if not text:
        raise ValueError("Groq returned an empty condensed conversation.")

    parsed = parse_json(text)
    return _normalize_condensed_json(parsed)


@dataclass
class ConversationMemory:
    base_dir: Path = DEFAULT_MEMORY_DIR
    condensed_base_dir: Path = DEFAULT_CONDENSED_MEMORY_DIR
    conversation_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])

    def __post_init__(self):
        self.base_dir = Path(self.base_dir)
        self.condensed_base_dir = Path(self.condensed_base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = datetime.now(timezone.utc)
        self.file_path = self.base_dir / f"{self.conversation_id}.json"
        self._state = {
            "conversation_id": self.conversation_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "updated_at": self.started_at.isoformat(timespec="seconds"),
            "messages": [],
        }
        self._persist()

    def _persist(self):
        try:
            write_json(self.file_path, self._state)
        except OSError:
            logger.exception("Failed to persist conversation memory to %s", self.file_path)

    def save(self, messages):
        self._state["updated_at"] = _utc_timestamp()
        self._state["messages"] = list(messages)
        self._persist()

    def condense_conversation(self, messages):
        if not messages:
            logger.info("Skipping post-conversation condensation because no messages were recorded.")
            return None

        payload = _build_condense_payload(messages)

        try:
            completion = _groq_call(**payload)
            message = completion.choices[0].message
            condensed_json = _parse_condensed_json(getattr(message, "content", ""))
        except JSONDecodeError:
            logger.exception("Groq returned invalid JSON for condensed conversation %s", self.conversation_id)
            return None
        except ValueError:
            logger.exception("Failed to condense conversation %s", self.conversation_id)
            return None
        except Exception:
            logger.exception("Failed to condense conversation %s", self.conversation_id)
            return None

        condensed_payload = {
            "conversation_id": self.conversation_id,
            "source_conversation_file": str(self.file_path),
            "created_at": _utc_timestamp(),
            "condensed_conversation": condensed_json,
        }
        condensed_path = self.condensed_base_dir / f"{self.conversation_id}.json"

        try:
            write_json(condensed_path, condensed_payload)
            logger.info("Saved condensed conversation to %s", condensed_path)
            return condensed_path
        except OSError:
            logger.exception("Failed to persist condensed conversation to %s", condensed_path)
            return None

