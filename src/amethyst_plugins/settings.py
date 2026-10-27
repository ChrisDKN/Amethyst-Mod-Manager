from __future__ import annotations

import re


TYPES = {"boolean", "text", "integer", "number", "choice", "file", "directory"}


def validate_value(setting, value):
    kind = setting["type"]
    valid = (type(value) is bool if kind == "boolean" else
             type(value) is int if kind == "integer" else
             type(value) in (int, float) and -1e12 <= value <= 1e12 if kind == "number" else
             isinstance(value, str))
    if not valid:
        raise ValueError(f"Invalid value type for setting '{setting['key']}'.")
    if kind in ("integer", "number"):
        bound = 2147483647 if kind == "integer" else 1e12
        if not setting.get("minimum", -bound) <= value <= setting.get("maximum", bound):
            raise ValueError(f"Setting '{setting['key']}' is outside its allowed range.")
    elif kind != "boolean":
        if len(value) > setting.get("max_length", 4096):
            raise ValueError(f"Setting '{setting['key']}' is too long.")
        if kind == "choice" and value not in setting["choices"]:
            raise ValueError(f"Setting '{setting['key']}' must use a declared choice.")
    return value


def validate_schema(settings, limit):
    if not isinstance(settings, list) or len(settings) > limit:
        raise ValueError(f"Expected at most {limit} settings.")
    keys = set()
    for item in settings:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", item["key"])
                or item["key"] in keys or not isinstance(item.get("label"), str)
                or not isinstance(item.get("type"), str) or item["type"] not in TYPES):
            raise ValueError("Invalid plugin setting.")
        kind = item["type"]
        if "description" in item and (not isinstance(item["description"], str) or len(item["description"]) > 4000):
            raise ValueError("Setting descriptions must be text of at most 4000 characters.")
        if "max_length" in item and (kind in ("boolean", "integer", "number")
                or type(item["max_length"]) is not int or not 1 <= item["max_length"] <= 4096):
            raise ValueError("max_length must be between 1 and 4096 for text settings.")
        bound = 2147483647 if kind == "integer" else 1e12
        for name in ("minimum", "maximum"):
            if name in item and (kind not in ("integer", "number")
                    or type(item[name]) not in ((int,) if kind == "integer" else (int, float))
                    or not -bound <= item[name] <= bound):
                raise ValueError("Invalid numeric setting bounds.")
        if item.get("minimum", -bound) > item.get("maximum", bound):
            raise ValueError("Setting minimum exceeds maximum.")
        if kind == "choice":
            choices = item.get("choices")
            if (not isinstance(choices, list) or not 1 <= len(choices) <= 128
                    or any(not isinstance(value, str) or len(value) > 4096 for value in choices)
                    or len(set(choices)) != len(choices)):
                raise ValueError("Choice settings need 1 to 128 unique text choices.")
            for choice in choices:
                validate_value(item, choice)
        if "default" not in item:
            raise ValueError("Plugin settings require a default.")
        validate_value(item, item["default"])
        keys.add(item["key"])
