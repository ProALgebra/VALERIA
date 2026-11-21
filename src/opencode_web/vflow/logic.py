"""Pure, deterministic JSON logic for Transform and If.

No eval/exec, no wildcards, no filters and no user-defined functions. The
limited path grammar is exactly: ``$``, ``.field``, ``['field']`` and ``[index]``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

PATH_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
IF_OPERATORS = {
    "eq", "ne", "gt", "ge", "lt", "le",
    "contains", "in", "exists", "is_null",
}


class LogicError(ValueError):
    pass


@dataclass(frozen=True)
class _Missing:
    pass


MISSING = _Missing()


def parse_path(path: str) -> tuple[str | int, ...]:
    if not isinstance(path, str) or not path.startswith("$"):
        raise LogicError(f"Path must start with '$': {path!r}")
    if path == "$":
        return ()
    parts: list[str | int] = []
    index = 1
    while index < len(path):
        char = path[index]
        if char == ".":
            match = PATH_FIELD_RE.match(path, index + 1)
            if match is None:
                raise LogicError(f"Invalid field in path: {path!r}")
            parts.append(match.group(0))
            index = match.end()
            continue
        if char == "[":
            if index + 1 < len(path) and path[index + 1] in ("'", '"'):
                quote = path[index + 1]
                end = path.find(quote, index + 2)
                if end < 0 or end + 1 >= len(path) or path[end + 1] != "]":
                    raise LogicError(f"Invalid quoted key in path: {path!r}")
                parts.append(path[index + 2:end])
                index = end + 2
                continue
            end = path.find("]", index + 1)
            if end < 0:
                raise LogicError(f"Unclosed index in path: {path!r}")
            raw = path[index + 1:end]
            if not raw.isdigit():
                raise LogicError(f"Only non-negative indexes are supported: {path!r}")
            parts.append(int(raw))
            index = end + 1
            continue
        raise LogicError(f"Unsupported path syntax: {path!r}")
    return tuple(parts)


def get_path(data: Any, path: str) -> Any:
    current = data
    for part in parse_path(path):
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return MISSING
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                return MISSING
            current = current[part]
    return current


def _new_container(next_part: str | int) -> Any:
    return [] if isinstance(next_part, int) else {}


def set_path(data: Any, path: str, value: Any, *, create_missing: bool = False) -> Any:
    parts = parse_path(path)
    if not parts:
        return copy.deepcopy(value)
    current = data
    for position, part in enumerate(parts[:-1]):
        next_part = parts[position + 1]
        if isinstance(part, int):
            if not isinstance(current, list):
                raise LogicError(f"Cannot index non-list at {path!r}")
            if part > len(current):
                raise LogicError(f"List index out of range at {path!r}")
            if part == len(current):
                if not create_missing:
                    raise LogicError(f"Missing list index at {path!r}")
                current.append(_new_container(next_part))
            child = current[part]
            if not isinstance(child, (dict, list)):
                if not create_missing:
                    raise LogicError(f"Cannot traverse scalar at {path!r}")
                child = _new_container(next_part)
                current[part] = child
            current = child
        else:
            if not isinstance(current, dict):
                raise LogicError(f"Cannot use key on non-object at {path!r}")
            if part not in current:
                if not create_missing:
                    raise LogicError(f"Missing object key at {path!r}")
                current[part] = _new_container(next_part)
            child = current[part]
            if not isinstance(child, (dict, list)):
                if not create_missing:
                    raise LogicError(f"Cannot traverse scalar at {path!r}")
                child = _new_container(next_part)
                current[part] = child
            current = child

    last = parts[-1]
    if isinstance(last, int):
        if not isinstance(current, list):
            raise LogicError(f"Cannot index non-list at {path!r}")
        if last > len(current):
            raise LogicError(f"List index out of range at {path!r}")
        if last == len(current):
            if not create_missing:
                raise LogicError(f"Missing list index at {path!r}")
            current.append(copy.deepcopy(value))
        else:
            current[last] = copy.deepcopy(value)
    else:
        if not isinstance(current, dict):
            raise LogicError(f"Cannot use key on non-object at {path!r}")
        current[last] = copy.deepcopy(value)
    return data


def delete_path(data: Any, path: str) -> Any:
    parts = parse_path(path)
    if not parts:
        raise LogicError("Cannot remove the root value")
    current = data
    for part in parts[:-1]:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                raise LogicError(f"Missing path: {path!r}")
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                raise LogicError(f"Missing path: {path!r}")
            current = current[part]
    last = parts[-1]
    if isinstance(last, int):
        if not isinstance(current, list) or last >= len(current):
            raise LogicError(f"Missing path: {path!r}")
        del current[last]
    else:
        if not isinstance(current, dict) or last not in current:
            raise LogicError(f"Missing path: {path!r}")
        del current[last]
    return data


def _resolve_value(data: Any, value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"from_path"}:
        source = get_path(data, str(value["from_path"]))
        if source is MISSING:
            raise LogicError(f"Missing from_path: {value['from_path']!r}")
        return copy.deepcopy(source)
    return copy.deepcopy(value)


def apply_transform(data: Any, config: dict[str, Any]) -> Any:
    operations = config.get("operations")
    if not isinstance(operations, list) or not operations:
        raise LogicError("Transform requires a non-empty operations list")
    create_missing = bool(config.get("create_missing", False))
    result = copy.deepcopy(data)

    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise LogicError(f"Operation #{index} must be an object")
        name = operation.get("op")
        if name == "set":
            result = set_path(
                result,
                str(operation["path"]),
                _resolve_value(result, operation.get("value")),
                create_missing=create_missing,
            )
        elif name in ("copy", "move"):
            source = get_path(result, str(operation["from"]))
            if source is MISSING:
                raise LogicError(f"Missing source path: {operation['from']!r}")
            result = set_path(
                result,
                str(operation["to"]),
                source,
                create_missing=create_missing,
            )
            if name == "move":
                result = delete_path(result, str(operation["from"]))
        elif name == "remove":
            result = delete_path(result, str(operation["path"]))
        elif name == "select":
            selected = get_path(result, str(operation["path"]))
            if selected is MISSING:
                raise LogicError(f"Missing select path: {operation['path']!r}")
            result = copy.deepcopy(selected)
        elif name == "merge_object":
            target = get_path(result, str(operation["path"]))
            if target is MISSING:
                if not create_missing:
                    raise LogicError(f"Missing merge target: {operation['path']!r}")
                result = set_path(
                    result, str(operation["path"]), {}, create_missing=True
                )
                target = get_path(result, str(operation["path"]))
            if not isinstance(target, dict):
                raise LogicError(f"Merge target is not an object: {operation['path']!r}")
            patch = operation.get("object")
            if not isinstance(patch, dict):
                raise LogicError("merge_object requires an object")
            target.update(copy.deepcopy(patch))
        elif name == "append":
            target = get_path(result, str(operation["path"]))
            if target is MISSING:
                if not create_missing:
                    raise LogicError(f"Missing append target: {operation['path']!r}")
                result = set_path(
                    result, str(operation["path"]), [], create_missing=True
                )
                target = get_path(result, str(operation["path"]))
            if not isinstance(target, list):
                raise LogicError(f"Append target is not a list: {operation['path']!r}")
            target.append(_resolve_value(result, operation.get("value")))
        elif name == "rename":
            source = get_path(result, str(operation["from"]))
            if source is MISSING:
                raise LogicError(f"Missing rename source: {operation['from']!r}")
            result = set_path(
                result,
                str(operation["to"]),
                source,
                create_missing=create_missing,
            )
            result = delete_path(result, str(operation["from"]))
        else:
            raise LogicError(f"Unsupported transform operation: {name!r}")
    return result


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise LogicError(f"Value is not JSON-serializable: {type(value).__name__}")


def _typed_equal(left: Any, right: Any) -> bool:
    if _json_type(left) != _json_type(right):
        return False
    return left == right


def evaluate_if(data: Any, config: dict[str, Any]) -> bool:
    operator = config.get("operator")
    if operator not in IF_OPERATORS:
        raise LogicError(f"Unsupported if operator: {operator!r}")
    path = str(config.get("path", ""))
    actual = get_path(data, path)

    if operator == "exists":
        return actual is not MISSING
    if operator == "is_null":
        return actual is not MISSING and actual is None

    if actual is MISSING:
        missing = config.get("missing", "error")
        if missing == "error":
            raise LogicError(f"Missing path for if: {path!r}")
        return missing == "true"

    expected = config.get("value")
    if operator == "eq":
        return _typed_equal(actual, expected)
    if operator == "ne":
        return not _typed_equal(actual, expected)
    if operator in ("gt", "ge", "lt", "le"):
        if _json_type(actual) != _json_type(expected):
            return False
        if _json_type(actual) not in ("number", "string"):
            raise LogicError(f"Operator {operator!r} requires number or string values")
        if operator == "gt":
            return actual > expected
        if operator == "ge":
            return actual >= expected
        if operator == "lt":
            return actual < expected
        return actual <= expected
    if operator == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return any(_typed_equal(item, expected) for item in actual)
        raise LogicError("contains requires a string or array")
    if operator == "in":
        if not isinstance(expected, list):
            raise LogicError("in requires an array value")
        return any(_typed_equal(actual, item) for item in expected)
    raise LogicError(f"Unsupported if operator: {operator!r}")
