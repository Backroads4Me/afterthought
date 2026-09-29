"""Check a model's JSON output against the schema it was asked for.

Small on purpose: it enforces exactly ``type``, ``enum``, ``minLength``, ``maxLength``,
``required``, ``properties``, ``items``, ``minItems`` and ``maxItems``, and ignores
everything else a schema may carry, such as ``description`` or
``additionalProperties``. That keeps a schema free to hold prose, and keeps this
module dependency-free (BUILD-PLAN 1, no new runtime dependencies).

A schema that misuses one of these keywords is reported as a schema problem naming that
keyword, whatever the answer looks like, so a typo in a committed schema never surfaces
as some unrelated exception.
"""

from __future__ import annotations

from typing import Any

# The keywords this module reads and the shape each must have. A schema that carries one
# must carry it in that shape: a wrong one is a schema bug, reported as one before any
# answer is judged against it, so it never surfaces as some other exception type. A
# bound written ``true`` is refused with them because bool is a subclass of int, and only
# ``type`` reads null as "any".
SHAPES = {
    "enum": (list, tuple),
    "required": (list, tuple),
    "properties": (dict,),
    "items": (dict,),
    "minLength": (int,),
    "maxLength": (int,),
    "minItems": (int,),
    "maxItems": (int,),
}

# Nothing answers to it, so probing ``_is_type`` with it asks every name in a ``type``
# list without any of them matching first.
UNMATCHABLE = object()


class SchemaError(Exception):
    pass


def check(schema: Any, path: str = "$") -> None:
    """Raise ``SchemaError`` for the first way ``schema`` misuses a keyword, whatever the
    answer turns out to be. Nested schemas are walked as well, so a defect in one is
    found before a call is made with it rather than only where an answer reaches.
    """
    if not isinstance(schema, dict):
        raise SchemaError(f"the schema for {path} is {schema!r}, not an object")
    for keyword, kinds in SHAPES.items():
        if keyword not in schema:
            continue
        declared = schema[keyword]
        if declared is None or isinstance(declared, bool) or not isinstance(declared, kinds):
            raise SchemaError(f"the schema for {path} declares {keyword} as {declared!r}")
    want = schema.get("type")
    if want is not None:
        _is_type(UNMATCHABLE, want)
    for key, subschema in schema.get("properties", {}).items():
        check(subschema, f"{path}.{key}")
    if "items" in schema:
        check(schema["items"], f"{path}[]")


def validate(value: Any, schema: dict, path: str = "$") -> None:
    """Raise ``SchemaError`` describing the first problem, else return None."""
    check(schema, path)

    want = schema.get("type")
    if want is not None and not _is_type(value, want):
        raise SchemaError(f"{path} should be {_say(want)}, found {_found(value)}")

    enum = schema.get("enum")
    if enum is not None and not any(value == item for item in enum):
        raise SchemaError(f"{path} should be one of {enum}, found {value!r}")

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
        if items is not None:
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
