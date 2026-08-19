"""Focused regression checks for the cross-game NPC viewer plumbing.

Run from the repository root with::

    PYTHONPATH=src python3 src/Utils/_npc_viewer_selftest.py

The checks use synthetic strings and BA2 data; no installed game is required.
"""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from Utils import archive_lookup  # noqa: E402
from Utils.ba2_writer import write_ba2  # noqa: E402
from Utils.facegen_tint import _decode_color_form, apply_face_tint  # noqa: E402
from Utils.fo4_facegen import apply_face_morphs, tri_morph_names  # noqa: E402
from Utils.mesh_catalog import _data_archives  # noqa: E402
from Utils.npc_body import _texture_lighting_tint  # noqa: E402
from Utils.npc_catalog import FACEGEN_PREFIX, _StringsCache  # noqa: E402
from Utils.plugin_loader import get_builtin_wizard_tools_for_game  # noqa: E402


def _strings(entries: dict[int, str]) -> bytes:
    """Build the small unprefixed STRINGS container used by the tests."""
    directory = bytearray()
    payload = bytearray()
    for string_id, value in entries.items():
        directory += struct.pack("<II", string_id, len(payload))
        payload += value.encode("cp1252") + b"\0"
    return struct.pack("<II", len(entries), len(payload)) + directory + payload


def test_fallout_gate() -> None:
    ids = {tool.id for tool in get_builtin_wizard_tools_for_game("Fallout4")}
    assert "npc_viewer" in ids
    print("✓ Fallout 4 tool gate")


def test_string_language_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        strings = data / "Strings"
        strings.mkdir()
        (strings / "Fallout4_en.STRINGS").write_bytes(
            _strings({0x1234: "Moe Cronin"}))
        got = _StringsCache(data, ("en", "english")).get("Fallout4.esm")
        assert got[0x1234] == "Moe Cronin"

        # Mods occasionally use the long Skyrim spelling even in FO4. The
        # secondary candidate must still be reached when *_en is absent.
        (strings / "Other_english.STRINGS").write_bytes(
            _strings({0x4321: "Fallback Name"}))
        got = _StringsCache(data, ("en", "english")).get("Other.esp")
        assert got[0x4321] == "Fallback Name"
    print("✓ Fallout 4 string suffix + fallback")


def _face_model(bs_version: int):
    return SimpleNamespace(
        header=SimpleNamespace(bs_version=bs_version),
        shapes=[SimpleNamespace(
            name="MaleHeadHuman", diffuse="actors/character/malehead.dds",
            vertices=[(0.0, 0.0, 0.0)])],
    )


def test_game_specific_face_tint() -> None:
    rel = ("meshes/actors/character/facegendata/facegeom/"
           "fallout4.esm/00002cb2.nif")
    fo4 = _face_model(130)
    assert not apply_face_tint(fo4, rel)
    assert not hasattr(fo4.shapes[0], "tint_overlay")

    sse = _face_model(100)
    assert apply_face_tint(sse, rel)
    assert sse.shapes[0].tint_overlay.endswith(
        "facetint/fallout4.esm/00002cb2.dds")
    print("✓ game-specific FaceTint behavior")


def test_fallout_colour_encodings() -> None:
    """FO4 CLFM floats and bright QNAM multipliers must not become RGB/clamp."""
    rgb, remap = _decode_color_form(struct.pack("<f", 0.707), 0x3)
    assert rgb is None and abs(remap - 0.707) < 0.0001
    rgb, remap = _decode_color_form(bytes((213, 10, 4, 255)), 0x1)
    assert rgb == (213 / 255.0, 10 / 255.0, 4 / 255.0) and remap is None

    tint = _texture_lighting_tint(struct.pack("<4f", 0.9, 0.8, 0.7, 1.0))
    assert all(abs(got - want) < 0.0001
               for got, want in zip(tint, (1.8, 1.6, 1.4)))
    print("✓ Fallout 4 palette colour + unclamped skin lighting")


def test_facegeom_archive_scope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source"
        face = source / FACEGEN_PREFIX / "fallout4.esm" / "00000001.nif"
        other = source / "meshes" / "architecture" / "house.nif"
        face.parent.mkdir(parents=True)
        other.parent.mkdir(parents=True)
        face.write_bytes(b"face")
        other.write_bytes(b"house")
        write_ba2(root / "Example - Main.ba2", source,
                  game_id="Fallout4", compress=False)

        archive_lookup._INDEX_CACHE.clear()
        entries = _data_archives(root, FACEGEN_PREFIX, (".nif",))
        assert [entry.rel_key for entry in entries] == [
            FACEGEN_PREFIX + "fallout4.esm/00000001.nif"]
        assert archive_lookup._INDEX_CACHE
        assert all(key[3] == FACEGEN_PREFIX
                   for key in archive_lookup._INDEX_CACHE)
    print("✓ FaceGeom-only archive index")


def test_fallout_chargen_tri() -> None:
    """An ESP slider delta deforms the baked head exactly once."""
    name = b"TestMorph\0"
    header = b"FRTRI003"
    header += struct.pack("<14I", 1, 0, 0, 0, 0, 0, 1, 1,
                          0, 0, 0, 0, 0, 0)
    tri = (header + struct.pack("<3f", 0.0, 0.0, 0.0)
           + struct.pack("<I", len(name)) + name
           + struct.pack("<f3h", 0.1, 10, -20, 30))
    assert tri_morph_names(tri) == ("TestMorph",)
    model = SimpleNamespace(shapes=[SimpleNamespace(
        name="FemaleHeadHuman", diffuse="femalehead_d.dds",
        vertices=[(1.0, 2.0, 3.0)])])
    assert apply_face_morphs(model, tri, {"TestMorph": 0.5}) == (1, 1)
    got = model.shapes[0].vertices[0]
    assert all(abs(a - b) < 0.0001
               for a, b in zip(got, (1.5, 1.0, 4.5)))
    print("✓ Fallout 4 chargen TRI slider delta")


def main() -> None:
    test_fallout_gate()
    test_string_language_fallback()
    test_game_specific_face_tint()
    test_fallout_colour_encodings()
    test_facegeom_archive_scope()
    test_fallout_chargen_tri()
    print("All NPC viewer self-tests passed.")


if __name__ == "__main__":
    main()
