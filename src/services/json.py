import json as stdjson
import os
from pathlib import Path


JSONDecodeError = stdjson.JSONDecodeError


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