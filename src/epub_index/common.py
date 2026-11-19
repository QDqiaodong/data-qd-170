"""Bounded input validation and atomic JSON publication."""
import json
import os
import tempfile
from pathlib import Path


def fields(obj, names):
    if not isinstance(obj, dict) or set(obj) != set(names.split()):
        raise ValueError('expected fields: ' + names)


def text(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid identifier')
    return value


def integer(value, low=0, high=10**9):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('integer outside contract')
    return value


def array(value, limit=10000):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('bounded array required')
    return value


def read_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('input exceeds 8MB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite number: ' + value)
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def publish(path, value):
    dest = Path(path)
    if dest.is_symlink():
        raise ValueError('output symlink rejected')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=dest.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            stream.write('\n')
        os.replace(temporary, dest)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
