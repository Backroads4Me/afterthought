"""Check a model's JSON output against the schema it was asked for.

Small on purpose: it enforces exactly ``type``, ``enum``, ``minLength``, ``maxLength``,
``required``, ``properties``, ``items``, ``minItems`` and ``maxItems``, and ignores
everything else a schema may carry, such as ``description`` or
``additionalProperties``. That keeps a schema free to hold prose, and keeps this
module dependency-free (BUILD-PLAN 1, no new runtime dependencies).
"""

from __future__ import annotations

from typing import Any


class SchemaError(Exception):
    pass


def validate(value: Any, schema: dict, path: str = "$") -> None:
    """Raise ``SchemaError`` describing the first problem, else return None."""
    want = schema.get("type")
    if want is not None and not _is_type(value, want):
        raise SchemaError(f"{path} should be {_say(want)}, found {_found(value)}")

    if "enum" in schema and not any(value == item for item in schema["enum"]):
        raise SchemaError(f"{path} should be one of {schema['enum']}, found {value!r}")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise SchemaError(f"{path} should be at least {minimum} characters, found {len(value)}")
        if maximum is not None and len(value) > maximum:
            raise SchemaError(f"{path} should be at most {maximum} characters, found {len(value)}")

    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                raise SchemaError(f"{path} is missing required key {key}")
        for key, subschema in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], subschema, path=f"{path}.{key}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise SchemaError(f"{path} should have at least {minimum} items, found {len(value)}")
        if maximum is not None and len(value) > maximum:
            raise SchemaError(f"{path} should have at most {maximum} items, found {len(value)}")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                validate(item, items, path=f"{path}[{index}]")


def _is_type(value: Any, want: Any) -> bool:
    """JSON Schema allows ``type`` to name one type or a list of them."""
    if isinstance(want, (list, tuple)):
        return any(_is_type(value, one) for one in want)
    if want == "object":
        return isinstance(value, dict)
    if want == "array":
        return isinstance(value, list)
    if want == "string":
        return isinstance(value, str)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "integer":
        # bool is a subclass of int, so it has to be refused explicitly.
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "null":
        return value is None
    raise SchemaError(f"schema declares an unsupported type {want!r}")


def _say(want: Any) -> str:
    if isinstance(want, (list, tuple)):
        return " or ".join(str(one) for one in want)
    return str(want)


def _found(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"
