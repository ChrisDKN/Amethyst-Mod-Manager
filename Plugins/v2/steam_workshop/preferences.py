import json
from pathlib import Path

from Utils.atomic_write import write_atomic_text


def preferences():
    try:
        result = json.loads((Path(__file__).parent / "data.json").read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preference(key, value):
    data = preferences()
    data[key] = value
    write_atomic_text(Path(__file__).parent / "data.json", json.dumps(data, indent=2) + "\n")


def load_workshop_page_size():
    value = preferences().get("page_size", 50)
    return value if type(value) is int and value in {20, 30, 40, 50, 100} else 50


def save_workshop_page_size(value):
    if value not in {20, 30, 40, 50, 100}:
        raise ValueError("Invalid page size")
    save_preference("page_size", value)
