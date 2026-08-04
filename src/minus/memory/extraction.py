"""Extract durable facts from a conversation transcript using an LLM.

Returns a list of fact dicts shaped like::

    {"attribute": "timezone", "value": "PST",
     "multi_valued": False, "raw_text": "I'm on Pacific time"}

The model is passed in rather than imported; see extract_facts_from_conversation.
"""

import json
import logging
import re
import time

from minus.core.prompts import FACT_EXTRACTION_PROMPT

logger = logging.getLogger(__name__)
EXTRACTION_PROMPT = FACT_EXTRACTION_PROMPT



def format_transcript(conversation):
    """Turn the conversation list into a plain-text transcript for the prompt."""
    logger.debug("conversation: %s", conversation)
    lines = []
    for turn in conversation:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        lines.append(f"{role.upper()}: {content}")
    return "\n\n".join(lines)


def extract_json_array(text):
    """Best-effort extraction of a JSON array from a model response,
    in case it wraps the array in code fences or adds stray text."""
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: find the first '[' and the matching last ']'
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        return json.loads(candidate)

    raise ValueError(f"Could not parse JSON array from model output:\n{text}")


def validate_facts(facts):
    """Filter/normalize facts to match the expected schema; drop malformed entries."""
    valid = []
    required_keys = {"attribute", "value", "multi_valued", "raw_text"}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if not required_keys.issubset(fact.keys()):
            continue
        if not isinstance(fact["attribute"], str) or not isinstance(fact["value"], str):
            continue
        if not isinstance(fact["multi_valued"], bool):
            continue
        if not isinstance(fact["raw_text"], str):
            continue
        valid.append({
            "attribute": fact["attribute"].strip(),
            "value": fact["value"].strip(),
            "multi_valued": fact["multi_valued"],
            "raw_text": fact["raw_text"].strip(),
        })
    return valid


def extract_facts_from_conversation(conversation, known_attributes, model, model_name=None):
    """Given a conversation (list of {role, content} dicts), return a list of fact dicts.

    Args:
        model: A ChatModel. Injected so extraction can be exercised without a
            network call, and so the roadmap's cheaper extraction tier is a
            caller's choice rather than a hard-coded import.
    """
    logger.info("Extracting semantic facts from conversation (%d turn(s))", len(conversation))
    transcript_text = format_transcript(conversation)

    full_extraction_prompt = EXTRACTION_PROMPT.replace("__ATTRIBUTE_LIST__", known_attributes)
    logger.debug("full_extraction_prompt: %s", full_extraction_prompt)
    messages = [
        {
            "role": "user",
            "content": f"{full_extraction_prompt}\n\nCONVERSATION:\n{transcript_text}",
        }
    ]

    start = time.monotonic()
    completion = model.complete(messages, model=model_name)
    logger.info("Fact extraction LLM call took %.2fs", time.monotonic() - start)

    message = completion.choices[0].message
    raw_text = getattr(message, "content", "") or ""

    facts = extract_json_array(raw_text)
    valid_facts = validate_facts(facts)
    logger.info("Extracted %d fact(s) from conversation", len(valid_facts))
    return valid_facts


