"""Prompt text for MINUS.

This module deliberately imports nothing but stdlib and `minus.paths`. It used
to live alongside the LLM call wrapper, which created a cycle: the memory
package needs the system prompt in order to record it in saved transcripts,
but the LLM client needs the memory package. Both `memory/service.py` and
`memory/condense.py` worked around that with function-local imports and a
comment explaining the dodge. Keeping prompt text free of behaviour removes
the cycle instead of hiding it.
"""

from __future__ import annotations

from pathlib import Path

from minus import paths

# The marker the assistant is told to look for, and the marker the agent
# actually writes. Previously these were two separate string literals in two
# files -- the prompt said "RELEVENT FACTS:" while the code emitted
# "RELEVANT FACTS:", so the model was hunting for a header that never
# appeared. One constant makes that class of drift impossible.
FACTS_MARKER = "RELEVANT FACTS:"


def build_system_prompt(workspace_root: Path) -> str:
    """The assistant's standing instructions, bound to a workspace root.

    The root is a parameter because it was previously a hard-coded absolute
    path to one developer's home directory, which made the prompt wrong for
    every other checkout.
    """
    return (
        "You are Minus, a concise helpful voice assistant. "
        "Keep responses short and natural for speech. "
        f"You operate inside a workspace rooted at {workspace_root}. "
        "When using file tools, always provide paths relative to this workspace root. "
        "Do not invent absolute paths or paths outside the workspace. "
        "Always make sure the file exists before trying to read the content of a file. "
        "IMPORTANT: If a file path is not known, ask the user or use the workspace "
        "listing tool first. "
        "Do not repeat the same tool call with identical arguments after you already "
        "have the result for it. "
        "You will sometimes be given a list of relevant facts about the user. These are "
        f"not in a file, they will be appended to the user prompt. Look for {FACTS_MARKER} "
        "for the list of facts. "
        # Softened from "Treat these as the absolute truth", per the author's
        # own "maybe don't say absolute truth" note on the original line: a
        # stale or mis-extracted fact should not outrank what the user just
        # said. Revert this sentence if the weaker wording loses recall.
        "Treat these as reliable background and use them to inform your responses."
    )


RETRY_NOTE = (
    "The previous attempt failed to generate a valid tool call. "
    "Return a valid response that matches the tool schema exactly. "
    "Do not repeat malformed arguments or duplicate keys. "
    "A directory that appears in a listing has NOT been explored yet - list it "
    "before concluding that a file does not exist anywhere in the workspace."
)


FACT_EXTRACTION_PROMPT = """Extract durable facts from this conversation as a JSON array. Only extract facts about the user or about the user's preferences. Do not extract facts about yourself. For each fact:
- attribute: normalized snake_case category (e.g. timezone, diet, job_title, preferred_editor)
- value: short canonical value, no filler words
- multi_valued: true if multiple values can be true at once (interests, allergies), false if only one can be true at a time (timezone, job, location)

Known attributes already in use: __ATTRIBUTE_LIST__
When extracting a fact, reuse an existing attribute name if it matches the same concept, even if the conversation phrased it differently. Only introduce a new attribute if none of the existing ones genuinely fit.
Only extract facts that would still matter in 3 months and would change how you'd respond in a future conversation. Skip one-off task details, pleasantries, and anything already implied by a fact you've already extracted. Do not include facts about the current conversation's topic unless they reflect a lasting preference or attribute.

For each fact, also include:
- raw_text: a short natural sentence that directly encodes the fact

Respond with ONLY a JSON array of objects, each shaped like:
{"attribute": str, "value": str, "multi_valued": bool, "raw_text": str}

Do not include any preamble, explanation, or markdown code fences - just the raw JSON array.
**IMPORTANT**: If no durable facts are found, respond with an empty array: []. Do not make up information or put value: "not available" or "unknown"
"""


# Convenience default for call sites not yet threaded through the composition
# root. Prefer build_system_prompt(settings.project_root) where a Settings is
# already in hand.
SYSTEM_PROMPT = build_system_prompt(paths.project_root())
