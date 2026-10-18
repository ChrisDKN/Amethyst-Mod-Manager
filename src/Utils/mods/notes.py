from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from Utils.mods.state import ModStateError


MAX_NOTE_LENGTH = 65536
MAX_CHANGES = 4096
MAX_NOTE_PAYLOAD = 1048576


def validate_changes(changes, append=False):
    if (not isinstance(changes, dict) or not 1 <= len(changes) <= MAX_CHANGES
            or not isinstance(append, bool)):
        raise ModStateError("invalid_mod_notes", "Supply a non-empty notes object and boolean append flag.")
    size = 0
    for name, text in changes.items():
        if (not isinstance(name, str) or not name or name in (".", "..")
                or Path(name).name != name or any(char in name for char in "\x00\r\n")):
            raise ModStateError("invalid_mod_notes", "A requested mod name is invalid.")
        if text is not None:
            if not isinstance(text, str):
                raise ModStateError("invalid_mod_notes", "Notes must be text or null.", {"mod": name})
            if len(text) > MAX_NOTE_LENGTH:
                raise ModStateError("invalid_mod_notes", f"A note cannot exceed {MAX_NOTE_LENGTH} characters.",
                                    {"mod": name, "maximum_length": MAX_NOTE_LENGTH})
            if "\x00" in text or any(0xd800 <= ord(char) <= 0xdfff for char in text):
                raise ModStateError("invalid_mod_notes", "Notes must be text or null, without NULs or invalid Unicode.",
                                    {"mod": name, "maximum_length": MAX_NOTE_LENGTH})
            size += len(text.encode("utf-8"))
    if size > MAX_NOTE_PAYLOAD:
        raise ModStateError("invalid_mod_notes", "The notes request is too large.",
                            {"maximum_bytes": MAX_NOTE_PAYLOAD})


def _state(profile):
    from Utils.profiles import state as shared

    path = profile / "profile_state.json"
    if not path.exists():
        shared.read_profile_state(profile)
    raw = path.read_bytes() if path.exists() else None
    try:
        state = json.loads(raw) if raw is not None else {}
        notes = state.get("mod_notes", {}) if isinstance(state, dict) else None
        if (not isinstance(state, dict) or not isinstance(notes, dict)
                or any(not isinstance(name, str) or not isinstance(text, str)
                       for name, text in notes.items())):
            raise ValueError()
    except (ValueError, UnicodeError) as exc:
        raise ModStateError("invalid_mod_notes_state",
                            "The saved profile or note state is invalid; it was left unchanged.") from exc
    return state, raw


@contextmanager
def _locked(profile):
    from Utils.mods.modlist import modlist_lock, parse_modlist_text
    from Utils.operations import acquire_resource_operation
    from Utils.profiles import state as shared

    profile = Path(profile)
    if profile.is_symlink() or not profile.is_dir():
        raise ModStateError("profile_not_found", "The selected profile is unavailable.")
    with acquire_resource_operation(f"mod-notes:{profile.resolve()}", "Mod notes edit"):
        with modlist_lock(profile / "modlist.txt"), shared._lock_for(profile):
            if (profile.is_symlink() or not profile.is_dir()
                    or any((profile / filename).is_symlink()
                           for filename in ("modlist.txt", "profile_state.json"))):
                raise ModStateError("unsafe_profile_path", "The profile or one of its state files is unsafe.")
            path = profile / "modlist.txt"
            try:
                raw = path.read_bytes() if path.exists() else b""
                state, state_raw = _state(profile)
            except OSError as exc:
                raise ModStateError("resource_unavailable", "Could not read the profile note state.",
                                    {"profile": profile.name}) from exc
            entries = parse_modlist_text(raw.decode("utf-8", errors="surrogateescape"))
            yield profile, raw, state, state_raw, entries


def _selection(entries, names):
    by_name = {}
    for entry in entries:
        by_name.setdefault(entry.name, []).append(entry)
    if names is None:
        names = [entry.name for entry in entries if not entry.is_separator
                 and not entry.is_group_header and not entry.name.startswith("@group:")]
    if len(set(names)) != len(names):
        raise ModStateError("ambiguous_mod", "The note selection contains duplicate mod names.")
    for name in names:
        matches = by_name.get(name, [])
        if len(matches) != 1:
            raise ModStateError("ambiguous_mod" if matches else "mod_not_found",
                                "A requested mod is not unique in the profile.",
                                {"mod": name, "matches": len(matches)})
        if matches[0].is_separator or matches[0].is_group_header or name.startswith("@group:"):
            raise ModStateError("mod_not_noteable", "Notes apply to real mods, not separators or group headers.",
                                {"mod": name})
    return names


def _inventory(profile, raw, state, entries, names):
    from Utils.profiles.state import read_mod_notes

    names = _selection(entries, names)
    notes = read_mod_notes(profile, {"mod_notes": state.get("mod_notes", {})})
    payload = json.dumps(state.get("mod_notes", {}), sort_keys=True, ensure_ascii=True).encode("ascii")
    digest = hashlib.sha256(str(profile.resolve()).encode() + b"\0" + raw + b"\0" + payload)
    return {"profile": profile.name, "scope": "profile", "revision": digest.hexdigest(),
            "mods": [{"name": name, "note": notes.get(name, "")} for name in names],
            "counts": {"mods": len(names), "noted": sum(name in notes for name in names)}}


def get_mod_notes(profile, mods=None):
    with _locked(profile) as (profile, raw, state, state_raw, entries):
        return _inventory(profile, raw, state, entries, mods)


def edit_mod_notes(profile, changes, *, append=False, revision=None):
    from Utils.atomic_write import write_atomic
    from Utils.profiles.state import write_profile_state

    validate_changes(changes, append)
    with _locked(profile) as (profile, raw, state, state_raw, entries):
        names = list(changes)
        current = _inventory(profile, raw, state, entries, names)
        if revision is not None and current["revision"] != revision:
            raise ModStateError("stale_mod_notes", "The mod list or notes changed; read the notes again.",
                                {"revision": current["revision"]})
        original = state.get("mod_notes", {})
        notes = dict(original)
        for name, text in changes.items():
            text = text.strip() if text is not None else None
            if append and text:
                existing = notes.get(name, "").rstrip()
                text = f"{existing}\n{text}" if existing else text
            elif append and text == "":
                continue
            if text and len(text) > MAX_NOTE_LENGTH:
                raise ModStateError("invalid_mod_notes", f"A saved note cannot exceed {MAX_NOTE_LENGTH} characters.",
                                    {"mod": name, "maximum_length": MAX_NOTE_LENGTH})
            if text:
                notes[name] = text
            else:
                notes.pop(name, None)
        changed = [name for name in names if notes.get(name) != original.get(name)]
        if changed:
            updated = dict(state)
            if notes:
                updated["mod_notes"] = notes
            else:
                updated.pop("mod_notes", None)
            path = profile / "profile_state.json"
            try:
                write_profile_state(profile, updated)
                saved, _ = _state(profile)
                if saved != updated:
                    raise OSError("The saved note state did not match the requested update.")
            except Exception as exc:
                try:
                    if state_raw is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, state_raw)
                except OSError as rollback:
                    raise ModStateError("mod_notes_rollback_failed",
                                        "Profile state could not be restored after a failed note save.") from rollback
                raise ModStateError("mod_notes_save_failed", "The notes could not be saved.") from exc
            state = updated
        return {**_inventory(profile, raw, state, entries, names),
                "ok": True, "changed": bool(changed), "changed_mods": changed}
