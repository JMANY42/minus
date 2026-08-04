#!/usr/bin/env python3
"""
Extract durable facts from a conversation transcript using an LLM.

Input: a JSON file containing:
{
  "conversation": [
    {"role": "user" | "assistant", "content": "..."},
    ...
  ]
}

Output: a JSON array of fact objects:
[
  {
    "attribute": "timezone",
    "value": "PST",
    "multi_valued": false,
    "raw_text": "I'm on Pacific time"
  },
  ...
]

Usage:
    python extract_facts.py transcript.json
    python extract_facts.py transcript.json -o facts.json
    cat transcript.json | python extract_facts.py -
"""

import argparse
import json
import logging
import os
import sys
import re
import time


logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract durable facts from this conversation as a JSON array. Only extract facts about the user or about the user's preferences. Do not extract facts about yourself. For each fact:
- attribute: normalized snake_case category (e.g. timezone, diet, job_title, preferred_editor)
- value: short canonical value, no filler words
- multi_valued: true if multiple values can be true at once (interests, allergies), false if only one can be true at a time (timezone, job, location)

Known attributes already in use: __ATTRIBUTE_LIST__
When extracting a fact, reuse an existing attribute name if it matches the same concept, even if the conversation phrased it differently. Only introduce a new attribute if none of the existing ones genuinely fit.
Only extract facts that would still matter in 3 months and would change how you'd respond in a future conversation. Skip one-off task details, pleasantries, and anything already implied by a fact you've already extracted. Do not include facts about the current conversation's topic unless they reflect a lasting preference or attribute.

For each fact, also include:
- raw_text: a short natural sentance that directly encodes the fact

Respond with ONLY a JSON array of objects, each shaped like:
{"attribute": str, "value": str, "multi_valued": bool, "raw_text": str}

Do not include any preamble, explanation, or markdown code fences — just the raw JSON array. 
**IMPORTANT**: If no durable facts are found, respond with an empty array: []. Do not make up information or put value: "not available" or "unknown"
"""


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


def _generate_completion(**kwargs):
    # Lazy import keeps this script usable in contexts that do not need the LLM client.
    from services.llm import generate_completion

    return generate_completion(**kwargs)


def extract_facts_from_conversation(conversation, known_attributes, model=None):
    """Given a conversation (list of {role, content} dicts), return a list of fact dicts."""
    logger.info("Extracting semantic facts from conversation (%d turn(s))", len(conversation))
    transcript_text = format_transcript(conversation)

    full_extraction_prompt = EXTRACTION_PROMPT.replace("__ATTRIBUTE_LIST__", known_attributes)
    logger.debug("full_extraction_prompt: %s", full_extraction_prompt)
    completion_kwargs = {
        "messages": [
            {
                "role": "user",
                "content": f"{full_extraction_prompt}\n\nCONVERSATION:\n{transcript_text}",
            }
        ],
    }
    if model:
        completion_kwargs["model"] = model

    start = time.monotonic()
    completion = _generate_completion(**completion_kwargs)
    logger.info("Fact extraction LLM call took %.2fs", time.monotonic() - start)

    message = completion.choices[0].message
    raw_text = getattr(message, "content", "") or ""

    facts = extract_json_array(raw_text)
    valid_facts = validate_facts(facts)
    logger.info("Extracted %d fact(s) from conversation", len(valid_facts))
    return valid_facts


def main():
    from services.llm import DEFAULT_MODEL

    parser = argparse.ArgumentParser(description="Extract durable facts from a conversation transcript.")
    parser.add_argument(
        "input",
        help="Path to a JSON file with a top-level 'conversation' key, or '-' to read from stdin.",
    )
    parser.add_argument(
        "-o", "--output",
        help="Path to write the resulting facts JSON array. Defaults to stdout.",
        default=None,
    )
    parser.add_argument(
        "--model",
        help=f"Model to use (default: {DEFAULT_MODEL})",
        default=None,
    )
    args = parser.parse_args()

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            raw = f.read()

    data = json.loads(raw)
    conversation = data.get("conversation")
    if conversation is None:
        print("Error: input JSON must have a top-level 'conversation' key.", file=sys.stderr)
        sys.exit(1)


    facts = extract_facts_from_conversation(conversation, model=args.model)
    output_json = json.dumps(facts, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json + "\n")
        print(f"Wrote {len(facts)} fact(s) to {args.output}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()