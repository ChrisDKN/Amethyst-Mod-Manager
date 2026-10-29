from pathlib import Path
from types import SimpleNamespace


def inspect_install(context):
    root = Path(context["extract_dir"])
    if context["installer_kind"] == "scripted_fomod":
        from Utils.fomod.parser import detect_scripted_fomod
        folder = Path(detect_scripted_fomod(str(root)))
        script = next((p for p in folder.iterdir() if p.name.lower() == "script.cs"), None)
        if script is None:
            context["log"]("Compiled script.dll installers are not supported by this plugin.")
            return None
        state = {"kind": "fomod", "source": folder.parent.relative_to(root).as_posix(),
                 "script": script.relative_to(root).as_posix()}
    elif context["installer_kind"] in {"omod", "plain"}:
        archive = Path(context["archive_path"])
        from .runtime import detect_omods
        sources = detect_omods(archive, root, context["files"])
        if not sources:
            return None
        state = {"kind": "omod", "source": None if archive.suffix.lower() == ".omod"
                 else sources[0].relative_to(root).as_posix(), "script": None}
        if len(sources) > 1:
            state["packages"] = [source.relative_to(root).as_posix() for source in sources]
    else:
        return None
    return {"interactive": True, "state": state}


def run_install(context, state):
    from .runtime import run_installer
    root = Path(context["extract_dir"])
    source = root / state["source"] if state["source"] is not None else Path(context["archive_path"])
    prepared = SimpleNamespace(game=context["game_handler"], profile_dir=Path(context["profile_path"]),
        extract_dir=root, mod_name=context["mod_name"],
        legacy_installer=(state["kind"], source, root / state["script"] if state["script"] else None))
    prepared.legacy_packages = [root / path for path in state.get("packages", [])]
    result = run_installer(prepared, log=context["log"], cancel=context["cancel"],
                           work=Path(context["work_dir"]))
    state["runtime"] = result
    state["prepared"] = prepared
    return {"files": [{"source": source, "destination": destination} for source, destination, _folder in result[1]]}


def validate_install(context, state, phase, *, replacing=False):
    if "runtime" in state:
        state["runtime"][4].validate(ini_only=phase == "commit" or phase == "stage" and replacing)


def commit_install(context, state):
    from .runtime import apply_settings
    _root, _files, edits, plugins, _reader = state["runtime"]
    state["writes"] = {}
    apply_settings(state["prepared"], edits, plugins, context["log"],
                   reader=_reader, writes=state["writes"], work=Path(context["work_dir"]))


def rollback_install(context, state):
    import uuid
    from Utils.atomic_write import write_atomic
    writes = state.get("writes", {})
    for path, (before, after) in list(writes.items()):
        current = path.read_bytes() if path.is_file() else None
        if current == before:
            writes.pop(path)
            continue
        if current != after:
            backup = path.with_name(path.name + ".legacy-backup-" + uuid.uuid4().hex)
            write_atomic(backup, before)
            context["log"](f"Kept externally changed {path}; original settings saved to {backup}.")
            writes.pop(path)
            continue
        write_atomic(path, before)
        writes.pop(path)
