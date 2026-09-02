"""
GUI-neutral core of the Fallout New Vegas 4GB Patch wizard.

Pure-Python port of the FNV 4GB Patcher (FalloutNVpatch.cpp): identifies
FalloutNV.exe by SHA-1, applies the large-address-aware + NVSE-loader byte
patches in place, and keeps FalloutNV_backup.exe for restoring the original.
Moved out of wizards/fnv_4gb_patch.py (which imports customtkinter) so the
Qt wizard view can share it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

EXE_NAME = "FalloutNV.exe"
BACKUP_NAME = "FalloutNV_backup.exe"

# SHA-1 (uppercase hex) of known unpatched exes → (version index, variant label)
_KNOWN_EXES = {
    "D068F394521A67C6E74FE572F59BD1BE71E855F3": (0, "US"),
    "3980940522F0264ED9AF14AEA1773BB19F5160AB": (1, "DE"),
    "5394B94A18FFA6FA846E1D6033AD7F81919F13AC": (2, "RU"),
    "07AFFDA66C89F09B0876A50C77759640BC416673": (0, "US"),
    "F65049B0957D83E61ECCCACC730015AE77FB4C8B": (1, "DE"),
    "ACA83D5A12A64AF8854E381752FE989692D46E04": (2, "RU"),
    "946D2EABA04A75FF361B8617C7632B49F1EDE9D3": (3, "GOG"),
}

_PATCHED_HASHES = {
    "0021023E37B1AF143305A61B7B29A1811CC7C5FB",
    "37CAE4E713B6B182311F66E31668D5005D1B9F5B",
    "600CD576CDE7746FB2CD152FDD24DB97453ED135",
    "34B65096CAEF9374DD6AA39AF855E43308B417F2",
}

# ---------------------------------------------------------------------------
# Patch byte tables (verbatim from FalloutNVpatch.cpp)
# ---------------------------------------------------------------------------

_PATCH1 = bytes([0x22])
_PATCH2 = bytes.fromhex("20 A6 07")
_PATCH3 = [  # per-version (US / DE / RU)
    bytes.fromhex("3E F9 FC"),
    bytes.fromhex("88 C4 FC"),
    bytes.fromhex("15 43 FD"),
]
_PATCH4 = bytes.fromhex("E8 04 FD 06 00 90")
_PATCH5 = bytes.fromhex("E9 56 FC 06 00")
_PATCH6 = bytes.fromhex(
    "90 50 50 8B C4 50 B8 40"
    "00 00 00 50 B8 04 00 00"
    "00 50 8B 85 0C FC FF FF"
    "05 0C 02 00 00 50 FF 55"
    "9C 8B 85 0C FC FF FF 05"
    "0C 02 00 00 C6 00 74 8B"
    "C4 50 8B 44 24 04 50 B8"
    "04 00 00 00 50 8B 85 0C"
    "FC FF FF 05 0C 02 00 00"
    "50 FF 55 9C 58 58 FF A5"
    "0C FC FF FF 00 00 00 00"
    "00 00 00 00 00 00 00 00"
    "50 68 00 A6 47 01 FF 15"
    "B0 F0 FD 00 58 5D 5F 5E"
    "5A 59 5B FF E0 00 00 00"
    "00 00 00 00 00 00 00 00"
) + b"nvse_steam_loader.dll"
_PATCH7 = bytes.fromhex(
    "60 68 C8 A6 47 01 68 E0"
    "A6 47 01 FF 15 F4 F1 FD"
    "00 68 C8 A6 47 01 68 D0"
    "A6 47 01 FF 15 F4 F1 FD"
    "00 61 E9 A7 EC F8 FF"
)
_PATCH8 = [  # per-version Steam AppId string
    b"2238", b"2238", b"2249",
]
_PATCH9 = b"0\x00\x00\x00SteamAppId\x00\x00\x00\x00\x00\x00SteamGameId"

_PATCH_G1 = bytes.fromhex("90 E5 BD")
_PATCH_G2 = bytes.fromhex("D0 4B F6")
_PATCH_G3 = bytes.fromhex(
    "68 A0 E5 FD 00 FF 15 B0"
    "F0 FD 00 E9 3B DF EE FF"
) + b"nvse_steam_loader.dll"


def _patch_list(version: int) -> list[tuple[int, bytes]]:
    """Return (offset, bytes) pairs for the given exe version index."""
    if version == 3:  # GOG
        return [
            (0x00000148, _PATCH_G1),
            (0x00000178, _PATCH_G2),
            (0x00BDD990, _PATCH_G3),
        ]
    return [
        (0x00000136, _PATCH1),
        (0x00000148, _PATCH2),
        (0x00000178, _PATCH3[version]),
        (0x00F57277, _PATCH4),
        (0x00F57385, _PATCH5),
        (0x00FC6F80, _PATCH6),
        (0x00FC7020, _PATCH7),
        (0x00FC70C8, _PATCH8[version]),
        (0x00FC70CC, _PATCH9),
    ]


def inspect_exe(game_root: Path) -> dict:
    """Hash FalloutNV.exe and report its patch state."""
    exe = game_root / EXE_NAME
    backup = game_root / BACKUP_NAME
    info = {
        "exe": exe,
        "backup": backup,
        "backup_exists": backup.is_file(),
        "state": "missing",
        "variant": None,
        "hash": None,
    }
    if not exe.is_file():
        return info
    digest = hashlib.sha1(exe.read_bytes()).hexdigest().upper()
    info["hash"] = digest
    if digest in _PATCHED_HASHES:
        info["state"] = "patched"
    elif digest in _KNOWN_EXES:
        info["state"] = "patchable"
        info["variant"] = _KNOWN_EXES[digest][1]
    else:
        info["state"] = "unknown"
    return info


def apply_4gb_patch(game_root: Path) -> str:
    """Patch FalloutNV.exe in place, keeping FalloutNV_backup.exe. Returns the variant label."""
    exe = game_root / EXE_NAME
    if not exe.is_file():
        raise RuntimeError(f"{EXE_NAME} not found in {game_root}")

    data = bytearray(exe.read_bytes())
    digest = hashlib.sha1(data).hexdigest().upper()
    if digest in _PATCHED_HASHES:
        raise RuntimeError(f"{EXE_NAME} is already patched.")
    known = _KNOWN_EXES.get(digest)
    if known is None:
        raise RuntimeError(
            f"Unrecognised {EXE_NAME} version (SHA-1 {digest}). "
            "The exe may already be modified - verify game files and try again."
        )
    version, variant = known

    for offset, blob in _patch_list(version):
        data[offset:offset + len(blob)] = blob

    backup = game_root / BACKUP_NAME
    tmp = game_root / (EXE_NAME + ".mm_tmp")
    tmp.write_bytes(data)
    try:
        shutil.copystat(exe, tmp)
    except OSError:
        pass
    if not backup.exists():
        os.replace(exe, backup)
    os.replace(tmp, exe)
    return variant


def restore_backup(game_root: Path) -> None:
    """Replace FalloutNV.exe with FalloutNV_backup.exe."""
    backup = game_root / BACKUP_NAME
    if not backup.is_file():
        raise RuntimeError(f"{BACKUP_NAME} not found in {game_root}")
    os.replace(backup, game_root / EXE_NAME)
