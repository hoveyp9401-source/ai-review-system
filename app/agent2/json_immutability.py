from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping


JsonScalar = str | int | float | bool | None
FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def freeze_json_value(value: Any, *, path: str = "json") -> FrozenJson:
    """Recursively detach and freeze a JSON-compatible value."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJson] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            frozen[key] = freeze_json_value(nested, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_json_value(nested, path=f"{path}[{index}]")
            for index, nested in enumerate(value)
        )
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def thaw_json_value(value: Any) -> Any:
    """Return a detached mutable JSON representation of a frozen value."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json_value(nested) for nested in value]
    return value
