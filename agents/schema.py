"""A small JSON Schema checker for model output (standard library only).

Supports the subset FaultOrbit's prompts use: type (incl. a list of types),
properties, required, additionalProperties: false, items, enum, minimum,
maximum, minItems, maxItems and minLength. Unknown keywords are ignored.
"""

from __future__ import annotations

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _type_ok(value, expected):
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(expected)
    return py is not None and isinstance(value, py)


def validate(schema, data, path="$"):
    """Return a list of human-readable errors; an empty list means valid."""
    errors = []
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(data, t) for t in options):
            return [f"{path}: expected {' or '.join(options)}, got {type(data).__name__}"]

    if "enum" in schema and data not in schema["enum"]:
        errors.append(f"{path}: {data!r} is not one of {schema['enum']}")

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errors.append(f"{path}: {data} < minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errors.append(f"{path}: {data} > maximum {schema['maximum']}")

    if isinstance(data, str) and len(data) < schema.get("minLength", 0):
        errors.append(f"{path}: shorter than {schema['minLength']} characters")

    if isinstance(data, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props:
                    errors.append(f"{path}: unexpected key {key!r}")
        for key, sub in props.items():
            if key in data:
                errors.extend(validate(sub, data[key], f"{path}.{key}"))

    if isinstance(data, list):
        if len(data) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(data):
                errors.extend(validate(schema["items"], item, f"{path}[{i}]"))

    return errors
