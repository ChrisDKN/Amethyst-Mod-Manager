"""BodySlide/RaceMenu body presets and BODYTRI vertex/UV deltas."""
from __future__ import annotations

import json
import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path


def load_presets(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Preset file is too large")
    if path.suffix.lower() == ".xml":
        root = ET.fromstring(data)
        out = {}
        for preset in root.iter("Preset"):
            sliders = {}
            for slider in preset.findall("SetSlider"):
                name, size = slider.get("name", ""), slider.get("size", "")
                value = float(slider.get("value", "0")) / 100
                if name and size in ("small", "big") and math.isfinite(value):
                    sliders.setdefault(name, {})[size] = value
            out[preset.get("name", path.stem)] = {
                name: (values.get("small", 0), values.get("big", 0))
                for name, values in sliders.items()}
        return out
    root = json.loads(data)
    sliders = {}
    for morph in root.get("bodyMorphs", ()):
        values = [float(k["value"]) for k in morph.get("keys", ())]
        value = sum(values) if values else float(morph.get("value", 0))
        if math.isfinite(value):
            sliders[morph["name"]] = (value, value)
    if not sliders:
        raise ValueError("No body morphs in this preset")
    return {path.stem: sliders}


def read_body_tri(data):
    packed = data[:4] in (b"PIRT", b"TRIP")
    if not packed and data[:4] not in (b"\0IRT", b"TRI\0"):
        raise ValueError("Not a body TRI file")
    pos = 4

    def unpack(fmt):
        nonlocal pos
        size = struct.calcsize(fmt)
        if pos + size > len(data):
            raise ValueError("Truncated body TRI")
        values = struct.unpack_from(fmt, data, pos)
        pos += size
        return values

    def name():
        nonlocal pos
        length, = unpack("<B")
        if pos + length > len(data):
            raise ValueError("Truncated body TRI name")
        value = data[pos:pos + length].decode("utf-8", "replace")
        pos += length
        return value

    out = {}
    for dimensions in (3, 2):
        if dimensions == 2 and (not packed or pos == len(data)):
            break
        count, = unpack("<H" if packed else "<I")
        for _ in range(count):
            shape = name()
            if not packed:
                unpack("<I")
            morphs, = unpack("<H" if packed else "<I")
            for _ in range(morphs):
                morph = name()
                if packed:
                    multiplier, count = unpack("<fH")
                else:
                    unpack("<I")
                    count, = unpack("<I")
                    multiplier = 1.0
                if not math.isfinite(multiplier):
                    raise ValueError("Invalid body TRI multiplier")
                rows = []
                for _ in range(count):
                    index, *delta = unpack("<H" + "h" * dimensions if packed else "<Ifff")
                    values = tuple(v * multiplier for v in delta)
                    if not all(math.isfinite(v) for v in values):
                        raise ValueError("Invalid body TRI delta")
                    rows.append((index, values))
                out[(shape, morph, dimensions)] = rows
    return out


def apply_body_morphs(model, data, weights):
    morphs = read_body_tri(data)
    changed, unknown = set(), set(weights)
    updates = []
    for shape in model.shapes:
        for dimensions, field in ((3, "vertices"), (2, "uvs")):
            source = getattr(shape, field)
            result = None
            for name, weight in weights.items():
                rows = morphs.get((shape.name, name, dimensions))
                if rows is None:
                    continue
                unknown.discard(name)
                if not math.isfinite(weight):
                    raise ValueError("Invalid body morph weight")
                if not weight:
                    continue
                if any(index >= len(source) for index, _ in rows):
                    raise ValueError(f"Body TRI vertex count mismatch: {shape.name}")
                if result is None:
                    result = list(source)
                for index, delta in rows:
                    result[index] = tuple(v + weight * d for v, d in zip(result[index], delta))
                changed.add(shape.name)
            if result is not None:
                updates.append((shape, field, result))
    for shape, field, result in updates:
        setattr(shape, field, result)
        if field == "vertices":
            shape.normals = []
            shape.tangents = []
    return len(changed), sorted(unknown)
