import json as stdjson
import os
import re
from pathlib import Path

JSONDecodeError = stdjson.JSONDecodeError

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE = re.compile(r"\s*```$")


def extract_json_object(text):
    """Best-effort extraction of a JSON object from a model response.

    The object counterpart of `extract_json_array` in memory/extraction.py, and
    it exists for the same reason: models routinely wrap structured output in
    code fences or introduce it with a sentence of prose, and a response that is
    otherwise correct should not be discarded over a stray "Here you go:".

    Raises ValueError if no object can be recovered, so callers can decide
    whether to degrade or fail.
    """
    text = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text.strip())).strip()

    try:
        return stdjson.loads(text)
    except stdjson.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stdjson.loads(text[start : end + 1])

    raise ValueError(f"Could not parse a JSON object from model output:\n{text}")


def read_json(path, encoding="utf-8"):
    with Path(path).open("r", encoding=encoding) as handle:
        return stdjson.load(handle)


def write_json(path, payload, *, encoding="utf-8", indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")

    with temp_path.open("w", encoding=encoding) as handle:
        stdjson.dump(payload, handle, ensure_ascii=False, indent=indent)
        handle.write("\n")

    os.replace(temp_path, path)


def parse_json(text):
    return stdjson.loads(text)


def serialize_json(value, *, indent=None, ensure_ascii=False, sort_keys=False, default=None):
    return stdjson.dumps(
        value,
        indent=indent,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        default=default,
    )


def pretty_json(value):
    if isinstance(value, str):
        try:
            value = parse_json(value)
        except JSONDecodeError:
            return value

    return serialize_json(value, indent=2, ensure_ascii=False, sort_keys=True, default=str)
