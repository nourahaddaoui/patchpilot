from agent.tools.schema import validate

SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1, "maxLength": 10},
        "n": {"type": "integer", "minimum": 1, "maximum": 5},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def test_valid_arguments_pass():
    assert validate(SCHEMA, {"path": "x", "n": 3, "mode": "a"}) == []


def test_arguments_must_be_an_object():
    assert validate(SCHEMA, ["path"]) == ["arguments must be a JSON object"]
    assert validate(SCHEMA, None) == ["arguments must be a JSON object"]


def test_missing_required():
    assert "'path' is required" in validate(SCHEMA, {})


def test_unexpected_argument_rejected():
    problems = validate(SCHEMA, {"path": "x", "file": "y"})
    assert any("unexpected argument 'file'" in p for p in problems)


def test_wrong_type_and_bool_is_not_integer():
    assert validate(SCHEMA, {"path": 3}) == ["'path' must be a string"]
    assert validate(SCHEMA, {"path": "x", "n": True}) == ["'n' must be an integer"]


def test_bounds_and_enum():
    assert validate(SCHEMA, {"path": ""}) == ["'path' must not be empty"]
    assert validate(SCHEMA, {"path": "x" * 11}) == ["'path' must be at most 10 characters"]
    assert validate(SCHEMA, {"path": "x", "n": 9}) == ["'n' must be <= 5"]
    assert validate(SCHEMA, {"path": "x", "mode": "c"}) == ["'mode' must be one of ['a', 'b']"]


def test_pattern_is_a_full_match():
    schema = {"type": "object", "properties": {"v": {"type": "string", "pattern": r"[0-9][0-9.]*"}}}
    assert validate(schema, {"v": "2.32.4"}) == []
    assert validate(schema, {"v": "2.32.4; rm -rf /"}) == ["'v' has an invalid format"]
