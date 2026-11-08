"""Strict JSON-schema validation for tool arguments (the subset the tools use).

Supported: type (object, string, integer, boolean), properties, required,
additionalProperties: false, enum, minLength, maxLength, pattern (full match),
minimum, maximum.
Returns a list of plain-text problems; an empty list means valid.
"""

from __future__ import annotations

import re

_TYPE_NAMES = {"object": "an object", "string": "a string", "integer": "an integer", "boolean": "true or false"}


def _type_ok(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    raise ValueError(f"unsupported schema type: {expected}")


def _check_value(name: str, value, spec: dict) -> list[str]:
    expected = spec.get("type")
    if expected and not _type_ok(value, expected):
        return [f"'{name}' must be {_TYPE_NAMES[expected]}"]
    problems = []
    if "enum" in spec and value not in spec["enum"]:
        problems.append(f"'{name}' must be one of {spec['enum']}")
    if isinstance(value, str):
        if len(value) < spec.get("minLength", 0):
            problems.append(f"'{name}' must not be empty" if spec["minLength"] == 1 else f"'{name}' is too short")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            problems.append(f"'{name}' must be at most {spec['maxLength']} characters")
        elif "pattern" in spec and value and not re.fullmatch(spec["pattern"], value):
            problems.append(f"'{name}' has an invalid format")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in spec and value < spec["minimum"]:
            problems.append(f"'{name}' must be >= {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            problems.append(f"'{name}' must be <= {spec['maximum']}")
    return problems


def validate(schema: dict, args) -> list[str]:
    if not isinstance(args, dict):
        return ["arguments must be a JSON object"]
    props = schema.get("properties", {})
    problems = []
    for name in schema.get("required", []):
        if name not in args:
            problems.append(f"'{name}' is required")
    if schema.get("additionalProperties") is False:
        for name in args:
            if name not in props:
                allowed = ", ".join(f"'{p}'" for p in props) or "none"
                problems.append(f"unexpected argument '{name}' (allowed: {allowed})")
    for name, value in args.items():
        if name in props:
            problems.extend(_check_value(name, value, props[name]))
    return problems
