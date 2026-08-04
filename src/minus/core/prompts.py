import logging

from minus.llm.client import DEFAULT_MODEL, generate_completion

logger = logging.getLogger(__name__)
SYSTEM_PROMPT = (
    "You are Minus, a concise helpful voice assistant. Keep responses short and natural for speech. "
    "You operate inside a workspace rooted at /home/jmany42/Projects/minus. "
    "When using file tools, always provide paths relative to this workspace root. "
    "Do not invent absolute paths or paths outside the workspace. "
    "Always make sure the file exists before trying to read the content of a file."
    "IMPORTANT: If a file path is not known, ask the user or use the workspace listing tool first. "
    "Do not repeat the same tool call with identical arguments after you already have the result for it."
    # maybe don't say absolute truth
    "You will sometimes be given a list of relevant facts about the user. These are not in a file, "
    "they will be appended to the user prompt. Look for RELEVENT FACTS: for the list of facts."
    "Treat these as the absolute truth and use them to inform your responses. "
)
MAX_RETRIES = 3
RETRY_NOTE = (
    "The previous attempt failed to generate a valid tool call. "
    "Return a valid response that matches the tool schema exactly. "
    "Do not repeat malformed arguments or duplicate keys. "
    "A directory that appears in a listing has NOT been explored yet - list it "
    "before concluding that a file does not exist anywhere in the workspace."
)


def generate_response(messages, tools=None, model=DEFAULT_MODEL, max_retries=MAX_RETRIES):
    return generate_completion(
        messages,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        max_retries=max_retries,
        retry_note=RETRY_NOTE,
    )
