from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from .archive_build import check_archive_state
from .archive_io import utf8_chunks
from .games import matches_game, token
from .hashes import XXHash
from .hosts import automatic_source
from .checks import make_check
from .models import PreflightReport
from .paths import WabbajackError, existing_parent, source_path, within
from .diagnostics import bind, emit, emit_exception, log_request, url_host
from .verification import parallel_verify


def _check_lengths(request, adapter, check, archive_paths, stop):
    import hashlib
    import json
    from dataclasses import asdict
    from .paths import check_path_length, path_limits
    from .verification import verified_read
    roots = [request.directory / "work/output", request.directory / "root",
             request.directory / "work/extract" / ("0" * 64) / "999999"]
    limits = {root: path_limits(root) for root in roots}
    configuration = json.dumps({"paths": [(str(root), limits[root]) for root in roots],
                                "adapter": asdict(adapter) if adapter else None},
                               sort_keys=True, default=sorted)
    key = ("path-limits-1", request.package.identity,
           hashlib.sha256(configuration.encode()).hexdigest())
    from .manifest import excluded_directives, required_directives
    required = required_directives(request.package, (), excluded_directives(request, adapter)) if adapter else None
    key += (tuple(request.profiles), tuple(sorted(request.fixes)))
    def inspect():
        failures = set()
        for directive in request.package.directives:
            if required is not None and directive.path not in required:
                continue
            if stop is not None and stop.is_set():
                raise InterruptedError("Preflight stopped")
            paths = [(roots[0], directive.path)]
            published = adapter.installed_path(directive.path) if adapter else directive.path
            if published:
                paths.append((roots[1], published))
            if directive.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
                paths.extend((roots[2], member) for member in archive_paths[directive.index][1])
            for root, relative in paths:
                try:
                    check_path_length(root, relative, limits[root])
                except WabbajackError as exc:
                    failures.add(str(exc))
                    if len(failures) >= 5:
                        break
            if len(failures) >= 5:
                break
        return sorted(failures)
    for detail in verified_read(request.package.path, key, inspect):
        check("error", "Filesystem path limits", detail)


def _game_file_category(game, relative):
    name = str(relative).replace("\\", "/").casefold()
    leaf = name.rsplit("/", 1)[-1]
    game_name = token(game)
    bethesda_kit = game_name in {
        "skyrimspecialedition", "skyrimvr", "fallout4", "fallout4vr", "starfield",
    }
    if bethesda_kit and (
        name.startswith(("tools/", "papyrus compiler/", "lex/"))
        or any(part in name for part in (
            "creationkit", "scriptcompile", "lipgen", "assetwatcher",
            "havokbehaviorpostprocess", "skyrimreservedaddonindexes",
            "p4com64", "flowchartx64",
        ))
    ):
        return "creation_kit"
    if leaf in {"debug.log", "installscript.vdf", "scripts.zip"}:
        return "support"
    if game_name in {"skyrimspecialedition", "skyrimvr", "fallout4", "fallout4vr"} and (
        (name.startswith("data/cc") and leaf.endswith((".bsa", ".ba2", ".esm", ".esl", ".esp")))
        or leaf.startswith("_resourcepack.")
        or leaf in {"skyrim.ccc", "fallout4.ccc", "marketplacetextures.bsa"}
    ):
        return "creation_content"
    return "game"


def _game_version_source(game, relative):
    leaf = str(relative).replace("\\", "/").casefold().rsplit("/", 1)[-1]
    anchors = {
        "skyrim": {"tesv.exe"},
        "skyrimspecialedition": {"skyrimse.exe"},
        "skyrimvr": {"skyrimvr.exe"},
        "fallout3": {"fallout3.exe"},
        "fallout3goty": {"fallout3.exe"},
        "falloutnewvegas": {"falloutnv.exe"},
        "fallout4": {"fallout4.exe"},
        "fallout4vr": {"fallout4vr.exe"},
        "oblivion": {"oblivion.exe"},
        "morrowind": {"morrowind.exe"},
        "starfield": {"starfield.exe"},
    }
    return leaf in anchors.get(token(game), set())


def _emit_game_file_problems(package, problems, check, readme="", ignored=False):
    classified = [(game, relative, present, archive,
                   _game_file_category(game, relative))
                  for game, relative, present, archive in problems]
    kit_games = {token(game) for game, _, _, _, category in classified
                 if category == "creation_kit"}
    grouped = {}
    for game, relative, present, archive, category in classified:
        if category == "support" and str(relative).replace("\\", "/").casefold().rsplit("/", 1)[-1] == "debug.log" and token(game) in kit_games:
            category = "creation_kit"
        grouped.setdefault((game, category), []).append((relative, present, archive))
    readme = str(package.metadata.get("Readme", "")).strip() or str(readme).strip()
    for category in ("creation_kit", "game", "support", "creation_content"):
        for (game, group), entries in grouped.items():
            if group != category:
                continue
            missing = sum(present is None for _, present, _ in entries)
            different = len(entries) - missing
            states = []
            if missing:
                states.append(f"{missing:,} missing")
            if different:
                states.append(f"{different:,} with a different size or hash")
            items = []
            for relative, present, archive in sorted(entries, key=lambda row: str(row[0]).casefold()):
                if present is None:
                    state = f"missing; expected {archive.size:,} bytes"
                else:
                    try:
                        actual = present.stat().st_size
                        state = f"does not match; expected {archive.size:,} bytes, found {actual:,} bytes"
                    except OSError:
                        state = f"could not be read; expected {archive.size:,} bytes"
                items.append(f"{relative} — {state}; expected xxHash64 {archive.key}")
            versions = sorted({str(a.state.get("GameVersion", "")).strip()
                               for _, _, a in entries if a.state.get("GameVersion")})
            installed_versions = set()
            if category == "game":
                from Utils.executables.icon import extract_exe_version
                installed_versions = {extract_exe_version(present)
                                      for relative, present, _ in entries
                                      if present is not None and _game_version_source(game, relative)}
                installed_versions.discard("")
            files = "ignored supporting files" if ignored else "required files"
            detail = f"{game}: {len(entries):,} {files} do not match ({', '.join(states)})."
            if versions and category != "creation_kit":
                detail += " Package game-source snapshot: " + ", ".join(versions) + "."
            if installed_versions:
                detail += " Detected installed executable version: " + ", ".join(sorted(installed_versions)) + "."
            if category == "creation_kit":
                name = "Creation Kit files"
                detail += " Install the matching Creation Kit through Steam, select Proton for it if needed, launch it once, close it, and recheck."
            elif category == "creation_content":
                name = "Creation content"
                detail += " The package uses these as reconstruction sources even if its README does not mention them. Install the exact Creations or Creation Club variants required by the author. Rare Curios can have different Steam and in-game versions. Every affected file is listed below."
            elif category == "support":
                if ignored:
                    name = "Ignored supporting files"
                    detail += " These logs, store metadata or script sources and editor files are not needed to run the game. Amethyst will omit their supporting output and continue. Every affected file is listed below."
                else:
                    name = "Required supporting files"
                    detail += " These logs, store metadata or script-source archives are used to reconstruct required output. Their absence or differing contents do not mean that the game executable has the wrong version. Every affected file is listed below."
            elif different and any(_game_version_source(game, relative)
                                   for relative, _, _ in entries):
                name = "Game version"
                detail += " Use the exact game build, store and language required by the author; files from a different build cannot substitute for it."
            else:
                name = "Required game files"
                detail += " Verify the original game location and required content. This alone does not prove that the game executable version is wrong."
            if readme:
                detail += " Author requirements: " + readme
            check("warning" if ignored else "error", name, detail, items)


def _reusable(request, stop, check, adapter, log=None, *, required=None):
    import sqlite3
    from .hashes import file_hash
    from .paths import within
    from .reconstruct import reusable_signature
    from .store import installation_info
    from .verification import cached_files
    directory = request.directory
    old, completed, root_reuse, stage_reuse = {}, {}, set(), set()
    if not (directory / "state.sqlite").is_file():
        return root_reuse, stage_reuse, old
    previous = installation_info(directory, log)
    def candidates():
        for directive in request.package.directives:
            if required is not None and directive.path not in required:
                continue
            if stop is not None and stop.is_set():
                raise InterruptedError("Preflight stopped")
            published = adapter.installed_path(directive.path) if adapter else directive.path
            rows = [(old.get("root/" + rel), directory / "root", rel, rel == published)
                    for rel in dict.fromkeys((published, directive.path)) if rel]
            rows.append((completed.get(directive.path), directory / "work/output", directive.path, False))
            rows = [row for row in rows if row[0]]
            if not rows:
                continue
            for row, base, rel, published in rows:
                if reusable_signature(directive, request, row[0], row[1], previous):
                    yield directive.path, base, rel, row[1], published, directive.output_size
    def verify(candidate):
        directive_path, base, rel, expected, published, _ = candidate
        path = within(base, rel)
        valid = path.is_file() and file_hash(path, stop) == expected
        return directive_path, published, valid
    try:
        with sqlite3.connect((directory / "state.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            old = {p: (sig, digest) for p, sig, digest in db.execute("SELECT path,signature,authored_hash FROM outputs")}
            completed = {p: (sig, digest) for p, sig, digest in db.execute("SELECT path,signature,actual_hash FROM completed")}
        pending = {}
        for candidate in candidates():
            pending.setdefault(candidate[1], {}).setdefault(candidate[2], []).append(candidate)
        for base, paths in pending.items():
            for relative, digest, _ in cached_files(base, paths, verify=True):
                for path, _, _, expected, published, _ in paths.pop(relative):
                    if digest == expected:
                        (root_reuse if published else stage_reuse).add(path)
        remaining = (candidate for paths in pending.values()
                     for rows in paths.values() for candidate in rows)
        for path, published, valid in parallel_verify(verify, remaining, stop,
                                                      size=lambda item: item[-1]):
            if valid:
                (root_reuse if published else stage_reuse).add(path)
    except InterruptedError:
        raise
    except (OSError, sqlite3.Error, WabbajackError) as exc:
        emit_exception(log, "preflight.reuse.failed", exc,
                       directory=request.directory)
        check("warning", "Reusable outputs", f"Existing content must be revalidated during installation: {exc}")
    return root_reuse, stage_reuse, old


def _verify_game_source(request, archive, stop, log):
    from .game_files import plan_game_file
    package = request.package
    name = str(archive.state.get("Game", archive.state.get("GameName", package.game)))
    game_root = next((p for n, p in request.game_roots.items() if token(n) == token(name)), None)
    rel = archive.state.get("GameFile", archive.name)
    found = None
    present = None
    candidates = []
    if game_root:
        for candidate in (rel, "Data/" + rel):
            try:
                path = source_path(game_root, candidate, expected=archive.key, size=archive.size, stop=stop)
                found = path
                break
            except InterruptedError:
                raise
            except (OSError, WabbajackError) as exc:
                emit(log, "preflight.game_file.candidate_rejected",
                     archive=archive.name, game_root=game_root,
                     candidate=candidate, exception_type=type(exc).__name__,
                     exception=str(exc))
                try:
                    candidate_path = source_path(game_root, candidate)
                    if candidate_path.is_file():
                        present = candidate_path
                        candidates.append(candidate_path)
                except (OSError, WabbajackError) as fallback_exc:
                    emit(log, "preflight.game_file.candidate_unavailable",
                         archive=archive.name, game_root=game_root,
                         candidate=candidate,
                         exception_type=type(fallback_exc).__name__,
                         exception=str(fallback_exc))
    preparation = plan_game_file(archive, candidates, stop, log) if found is None else None
    return name, rel, found, present, preparation


def preflight(request, stop=None, *, cache=None, progress=None, log=None) -> PreflightReport:
    from .verification import VERIFICATION_WORKERS, VerificationCache, verification_scope
    log = bind(log, request.diagnostic_id)
    timings = {}
    stage, started, emitted = "", time.monotonic(), 0.0
    overall_started = started
    progress_lock = threading.Lock()
    log_request(log, request, "preflight.request")
    emit(log, "preflight.verification.workers", workers=VERIFICATION_WORKERS,
         max_pending=VERIFICATION_WORKERS * 2)
    def notify(name, detail=""):
        nonlocal stage, started, emitted
        if stop is not None and stop.is_set():
            raise InterruptedError("Preflight stopped")
        with progress_lock:
            now = time.monotonic()
            changed = name != stage
            if changed:
                if stage:
                    timings[stage] = timings.get(stage, 0) + now - started
                    emit(log, "preflight.stage.completed", name=stage,
                         elapsed_seconds=round(now - started, 3))
                stage, started = name, now
                emit(log, "preflight.stage.started", name=name, detail=detail)
            if progress and (changed or now - emitted >= 0.1):
                progress(name, 0, 0, detail)
                emitted = now
    if cache is None:
        cache = VerificationCache(directory=request.downloads / ".wabbajack-checks")
    try:
        with verification_scope(cache, stop,
                                lambda path: notify(stage, path.name), log):
            report = _preflight(request, stop, notify, log)
    except BaseException as exc:
        emit_exception(log, "preflight.failed", exc,
                       diagnostic_id=request.diagnostic_id, stage=stage,
                       elapsed_seconds=round(time.monotonic() - overall_started, 3))
        raise
    if stage:
        timings[stage] = timings.get(stage, 0) + time.monotonic() - started
        emit(log, "preflight.stage.completed", name=stage,
             elapsed_seconds=round(time.monotonic() - started, 3))
    report.timings = timings
    emit(log, "preflight.completed", diagnostic_id=request.diagnostic_id,
         ok=report.ok, checks=len(report.checks),
         passed=sum(item.status == "pass" for item in report.checks),
         warnings=sum(item.status == "warning" for item in report.checks),
         manual=sum(item.status == "manual" for item in report.checks),
         errors=sum(item.status == "error" for item in report.checks),
         cached_archives=len(report.cached), game_files=len(report.game_files),
         prepared_game_files=len(report.prepared_game_files),
         required_archives=len(report.required_archives or []),
         download_bytes=report.download_bytes, install_bytes=report.install_bytes,
         timings=timings,
         elapsed_seconds=round(time.monotonic() - overall_started, 3))
    return report


def _verify_package(package, stop):
    from .hashes import package_hash
    if package_hash(package.path) != package.identity:
        raise WabbajackError("Modlist package changed after inspection; reopen it before checking requirements")
    with zipfile.ZipFile(package.path) as archive:
        for member in archive.infolist():
            with archive.open(member) as stream:
                while stream.read(1024 * 1024):
                    if stop is not None and stop.is_set():
                        raise InterruptedError("Preflight stopped")


def _preflight(request, stop, notify, log=None):
    package = request.package
    report = PreflightReport()
    def check(status, name, detail, items=()):
        result = make_check(status, name, detail, items)
        report.checks.append(result)
        emit(log, "preflight.check", status=result.status, name=result.name,
             detail=result.detail, explanation=result.explanation,
             resolution=result.resolution, items=len(result.items))
        for index, item in enumerate(result.items):
            emit(log, "preflight.check.item", check=result.name,
                 index=index + 1, total=len(result.items), detail=item)
    notify("Checking installation requirements")
    if request.game.get_deploy_active():
        check("error", "Deployment", "Restore the deployed game before installing or updating a modlist")
    try:
        from Utils.filegraph.service import require_native
        require_native()
    except Exception as exc:
        emit_exception(log, "preflight.filegraph.failed", exc)
        check("error", "File catalog", f"The native Filegraph component is required: {exc}")
    root = Path(request.game.get_profile_root()).resolve()
    directory = request.directory.resolve()
    if not matches_game(request.game, package.game):
        check("error", "Game", f"This list requires {package.game}; selected {request.game.name}")
    if request.mode not in {"install", "resume", "repair", "update"}:
        check("error", "Operation", "Unknown installation operation")
    if directory.parent != root / ".wabbajack" or request.directory.is_symlink():
        check("error", "Installation directory", "Choose a directory directly inside the game's .wabbajack directory")
    if request.mode == "install" and directory.exists() and any(directory.iterdir()):
        check("error", "Installation directory", "Choose an empty managed directory, or use Resume, Repair or Update")
    if package.profiles and (not request.profiles or any(p not in package.profiles for p in request.profiles)):
        check("error", "Profiles", "Select at least one authored profile")
    downloads = request.downloads.resolve()
    pairs = [(directory, downloads)]
    pairs.extend((dest, path.resolve()) for path in request.game_roots.values() for dest in (directory, downloads))
    for source, target in pairs:
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            check("error", "Path overlap", f"The installation, downloads and original games must use separate directories: {source}")
    for name in ("root", "work", "backups", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm", "install.lock"):
        if (directory / name).is_symlink():
            check("error", "Installation directory", f"Managed entry cannot be a symbolic link: {name}")
    from .store import installation_info
    info = installation_info(directory, log)
    if request.mode != "install":
        if not info:
            check("error", "Installation", "No managed installation exists at this location")
        elif request.mode in {"resume", "repair"} and package.identity != info.get("pending_package", info.get("package_identity")):
            check("error", "Package identity", "Resume and Repair require the saved package. Use Update for a different authored version.")
    if not report.ok:
        return report
    hardlinks = True
    notify("Checking filesystem capabilities")
    try:
        parent = existing_parent(request.directory)
        with tempfile.TemporaryDirectory(prefix=".amethyst-preflight-", dir=parent) as tmp:
            p = Path(tmp)
            (p / "source").write_bytes(b"ok")
            try:
                os.link(p / "source", p / "hardlink")
            except OSError as exc:
                hardlinks = False
                check("warning", "Filesystem", f"Hard links are unavailable; installation will use copies and reserve space for them: {exc}")
            (p / "link").symlink_to("source")
            if (p / "link").read_bytes() != b"ok":
                raise OSError("Symbolic links are unavailable")
            (p / "Case").write_bytes(b"a")
            case_sensitive = not (p / "case").exists()
            if not case_sensitive:
                check("warning", "Filesystem", "Case-insensitive installation filesystem")
            emit(log, "preflight.filesystem_probe", path=parent,
                 hardlinks=hardlinks, symlinks=True,
                 case_sensitive=case_sensitive)
    except OSError as exc:
        emit_exception(log, "preflight.filesystem_probe.failed", exc,
                       path=parent)
        check("error", "Filesystem capabilities", exc)
    if not report.ok:
        return report
    notify("Verifying package integrity")
    try:
        XXHash()
        from .verification import verified_read
        verified_read(package.path, ("package", package.identity), lambda: _verify_package(package, stop))
        check("pass", "Package", f"{package.name} {package.version}; {len(package.directives):,} output files")
    except InterruptedError:
        raise
    except (WabbajackError, zipfile.BadZipFile, OSError) as exc:
        emit_exception(log, "preflight.package.failed", exc, path=package.path)
        check("error", "Package integrity", exc)
        return report
    notify("Checking game layout and paths")
    from .manifest import archive_path
    archive_paths = {d.index: archive_path(d.data) for d in package.directives
                     if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}}
    from .manifest import stock_folder
    from .adapters import ROOT_MOD_NAME, adapter_for
    stock = ""
    adapter = None
    try:
        adapter = adapter_for(package, request.game,
                              store=request.setup_options.get("store", ""), log=log)
        stock = stock_folder(package)
        emit(log, "preflight.adapter", adapter=type(adapter).__name__,
             mo2=adapter.mo2, bethesda=adapter.bethesda,
             store=adapter.store, stock_folder=stock)
        if stock:
            check("pass", "Stock game launch", f"Launch the reconstructed game at {request.directory / 'root' / stock}; retain the original game for source verification")
    except WabbajackError as exc:
        emit_exception(log, "preflight.adapter.failed", exc)
        check("error", "Game layout", exc)
    _check_lengths(request, adapter, check, archive_paths, stop)
    from .manifest import excluded_directives, required_directives
    planned_paths = required_directives(package, (), excluded_directives(request, adapter)) if adapter else {d.path for d in package.directives}
    if adapter:
        from .adapters import STORE_ROOT_FOLDERS
        if adapter.bethesda and any(d.path.split("/")[0].casefold().strip("_ ") in STORE_ROOT_FOLDERS for d in package.directives):
            check("pass" if adapter.store in {"steam-gog", "epic"} else "error", "Store-specific root files",
                  f"Use the {adapter.store} payload" if adapter.store else "Select Steam/GOG or Epic in setup so the correct root payload is installed")
        deployed = {}
        root_mod_files = []
        for directive in package.directives:
            dest = adapter.root_destination(directive.path)
            if dest:
                previous = deployed.setdefault(dest.casefold(), directive.path)
                if previous != directive.path:
                    check("error", "Game-root conflict", f"{previous} and {directive.path} both deploy to {dest}")
            if adapter.root_mod_destination(directive.path):
                root_mod_files.append(directive.path)
        if root_mod_files:
            check("pass", "Game-root mod", f"Install {len(root_mod_files):,} files as the enabled {ROOT_MOD_NAME} mod, preserving game-root paths including Data/")
            if any(d.path.casefold().startswith(f"mods/{ROOT_MOD_NAME}/".casefold()) for d in package.directives):
                check("error", "Game-root mod", f"The authored list already contains the reserved mod {ROOT_MOD_NAME}")
            meta = request.directory / "root" / "mods" / ROOT_MOD_NAME / "meta.ini"
            if meta.parent.exists():
                import sqlite3
                owned = False
                database = request.directory / "state.sqlite"
                if database.is_file():
                    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                        owned = db.execute("SELECT 1 FROM outputs WHERE path=?", (f"root/mods/{ROOT_MOD_NAME}/meta.ini",)).fetchone() is not None
                if not owned:
                    check("error", "Game-root mod", f"Move or rename the existing unowned {ROOT_MOD_NAME} mod before installing")
            if any(adapter.root_mod_destination(p).casefold() == "meta.ini" for p in root_mod_files):
                check("error", "Game-root mod", "The root payload includes meta.ini, which is reserved for mod metadata")
        if deployed and not getattr(request.game, "root_folder_deploy_enabled", True):
            check("error", "Game-root deployment", "The selected game handler does not support this package's root payload layout")
    unknown_folders = {d.path.split("/")[0] for d in package.directives
                       if any(word in d.path.split("/")[0].casefold() for word in ("manual install", "root mods"))
                       and not (adapter and adapter.application_file(d.path))
                       and not (adapter and adapter.root_destination(d.path))}
    for folder in sorted(unknown_folders):
        check("manual", "Author instructions", f"Review the files in {folder}. Their purpose is not declared as a game-root layout; follow the list's README before launching.")
    if adapter and adapter.store == "epic" and any(d.path.split("/")[0].casefold().strip("_ ") == "epic only files requiring manual install" for d in package.directives):
        check("manual", "Author instructions", "The Epic root payload is selected. Apply the author's Epic executable patch before launch; the Steam/GOG 4GB patch does not convert the Epic executable.")
    if any(d.path.rsplit("/", 1)[-1].casefold() == "fnvpatch.exe" and not (adapter and adapter.application_file(d.path)) for d in package.directives):
        if getattr(request.game, "auto_4gb_patch", False):
            check("pass", "Game patch", "The game handler applies the New Vegas 4GB patch during deployment")
        else:
            check("manual", "Author instructions", "Enable automatic 4GB patching in the game's settings or apply the provided FNV patcher before launching")
    from .bsa_setup import preflight_setup, requirement as bsa_requirement
    notify("Checking BSA setup")
    bsa_bytes, bsa_reuse = preflight_setup(request, check, stop, log=log, hardlinks=hardlinks)
    bsa = bsa_requirement(package)
    for folder in sorted({d.path.split("/")[0] for d in package.directives
                          if "patcher" in d.path.split("/")[0].casefold()}):
        if bsa and folder in bsa.folders:
            continue
        check("manual", "Author instructions", f"The tool in {folder} is retained. Follow the author's instructions to run it; copying its files does not apply its patches.")
    notify("Checking installation and profiles")
    if request.mode != "install":
        from .profiles import referenced_profiles
        affected = referenced_profiles(directory, root, log)
        check("warning", "Affected profiles", ", ".join(p.name for p in affected) or "No published profiles")
        from .planning import plan_update
        plan = plan_update(request, log=log)
        check("pass", "Update preview", f"{len(plan.added):,} added, {len(plan.changed):,} changed, {len(plan.removed):,} obsolete authored files. Local changes are compared before publication.")
        old_profiles = set(info.get("selected_profiles", []))
        new_profiles = set(request.profiles)
        if old_profiles != new_profiles:
            check("warning", "Authored profile changes", f"Added: {', '.join(sorted(new_profiles - old_profiles)) or 'none'}; removed: {', '.join(sorted(old_profiles - new_profiles)) or 'none'}. Removed profiles remain available for review.")
    if any(d.kind == "RemappedInlineFile" and d.path in planned_paths for d in package.directives):
        from .runtime import windows_path
        try:
            for path in [directory / "root", request.downloads, *request.game_roots.values()]:
                windows_path(request.game, path)
        except WabbajackError as exc:
            emit_exception(log, "preflight.path_mapping.failed", exc)
            check("error", "Windows path mapping", exc)
    from .requirements import profile_configuration
    from .setup_tasks import preflight_tasks
    configuration = profile_configuration(package, request.profiles)
    emit(log, "preflight.profile_configuration", profiles={name: {
        "mods": len(config.mods), "plugins": len(config.plugins),
        "outputs": config.outputs} for name, config in configuration.items()})
    enabled = {mod.casefold() for config in configuration.values() for mod in config.mods}
    active_paths = [d.path.casefold() for d in package.directives
                    if not d.path.startswith("mods/") or d.path.split("/")[1].casefold() in enabled]
    if any(path.endswith("/jcontainers64.dll") for path in active_paths):
        check("manual", "Author instructions", "JContainers is included. Review its build against the selected Skyrim runtime and Proton compatibility; Amethyst preserves the authored DLL and does not substitute an unverified version.")
    if any(path.rsplit("/", 1)[-1] == "synthesis.exe" for path in active_paths):
        check("manual", "Author instructions", "Synthesis is included. Its patchers may require a .NET SDK and NuGet setup in the tool runtime. These are not configured automatically; follow the author's tool instructions before generating patches.")
    setup_reuse = set()
    notify("Checking additional setup")
    tasks, setup_bytes = preflight_tasks(request, check, stop,
        configuration=configuration, reusable=setup_reuse, log=log, hardlinks=hardlinks)
    from .post_install import preflight_post_install
    setup_bytes += preflight_post_install(request, check, stop,
                                          reusable=setup_reuse, log=log, hardlinks=hardlinks)
    report.setup_tasks = tasks
    notify("Checking authored profiles")
    provided_mods = {d.path.split("/")[1].casefold() for d in package.directives if d.path.startswith("mods/")}
    provided_mods.update(task.mod.casefold() for task in tasks)
    for profile, config in configuration.items():
        if config.outputs:
            check("pass", "Output mods", f"{profile}: create and retain {', '.join(sorted(set(config.outputs.values())))} for authored tool output")
            check("manual", "Tool output configuration", f"{profile}: authored output folders are retained. Pandora and PGPatcher are configured at launch; select the listed output folder in other tools before generating files.")
    profile_lists = {}
    with zipfile.ZipFile(package.path) as archive:
        for directive in package.directives:
            parts = directive.path.split("/")
            if directive.path not in planned_paths:
                continue
            member = directive.data.get("SourceDataID")
            if not member:
                continue
            is_list = (len(parts) == 3 and parts[0] == "profiles" and parts[1] in request.profiles
                       and parts[2].casefold() in {"modlist.txt", "plugins.txt"})
            if directive.kind != "RemappedInlineFile" and not is_list:
                continue
            try:
                if is_list:
                    if archive.getinfo(member).file_size > 8 * 1024 * 1024:
                        check("error", "Configuration file", f"{directive.path} exceeds the 8 MiB profile-list limit")
                        continue
                    lines = archive.read(member).decode("utf-8-sig").splitlines()
                else:
                    with archive.open(member) as source:
                        for _ in utf8_chunks(source, stop):
                            pass
            except UnicodeError:
                check("error", "Configuration file", f"{directive.path} is not UTF-8 text")
                continue
            if is_list:
                profile_lists[(parts[1], parts[2].casefold())] = lines
        for (profile, kind), lines in profile_lists.items():
            if kind != "modlist.txt":
                continue
            available_mods = provided_mods | {n.casefold() for n in configuration[profile].outputs.values()}
            for line in lines:
                if not line.startswith("+") or line.casefold().endswith("_separator"):
                    continue
                name = line[1:]
                normalized = name.casefold()
                if normalized in available_mods:
                    continue
                event = ("preflight.development_mod.skipped" if normalized.startswith("[dev]")
                         else "preflight.profile_mod.unrepresented")
                emit(log, event, profile=profile, mod=name)
    vanilla = {p.casefold() for p in [*getattr(request.game, "vanilla_plugins", []),
                                     *getattr(request.game, "vanilla_dlc_plugins", [])]}
    provided_plugins = {d.path.rsplit("/", 1)[-1].casefold() for d in package.directives if d.path.casefold().endswith((".esm", ".esp", ".esl"))}
    for (profile, kind), lines in profile_lists.items():
        if kind != "plugins.txt":
            continue
        starred = any(line.startswith("*") for line in lines)
        for line in lines:
            name = line.strip().removeprefix("*")
            if not name or name.startswith("#") or (starred and not line.startswith("*")) or name.casefold() not in vanilla or name.casefold() in provided_plugins:
                continue
            available = False
            for game_root in request.game_roots.values():
                try:
                    available |= source_path(game_root, "Data/" + name).is_file()
                except (OSError, WabbajackError) as exc:
                    emit(log, "preflight.game_plugin.probe_failed",
                         profile=profile, plugin=name, game_root=game_root,
                         exception_type=type(exc).__name__, exception=str(exc))
            if not available:
                check("error", "Required DLC or game plugin", f"Profile {profile} enables {name}, which is missing from the original game and reconstructed files")
    from .manifest import required_directives, dependency_paths, optional_game_file_directives, excluded_directives
    notify("Verifying reusable installation files")
    root_reuse, stage_reuse, old_outputs = _reusable(
        request, stop, check, adapter, log, required=planned_paths)
    reusable = root_reuse | stage_reuse
    optional = optional_game_file_directives(package)
    ignored_directives = excluded_directives(request, adapter) if adapter else optional
    required = required_directives(package, reusable, ignored_directives)
    pending = [d for d in package.directives if d.path in required and d.path not in reusable]
    required_archives = {archive_paths[d.index][0] for d in pending if d.index in archive_paths}
    ignored_archives = {archive_paths[d.index][0] for d in package.directives
                        if d.path in optional and d.index in archive_paths} - required_archives
    report.required_archives = sorted(required_archives)
    emit(log, "preflight.reuse", root_outputs=len(root_reuse),
         staged_outputs=len(stage_reuse), prior_outputs=len(old_outputs),
         pending_directives=len(pending), ignored_directives=len(ignored_directives),
         required_archives=len(required_archives), ignored_archives=len(ignored_archives))
    from Utils.downloads.core import get_scan_dirs
    from .acquire import ArchiveCacheIndex
    scan_dirs = [request.downloads, *get_scan_dirs(request.game.name)]
    notify("Finding cached downloads")
    sizes_needed = {a.size for a in package.archives.values() if a.key in required_archives and a.kind != "GameFileSource"}
    emit(log, "preflight.cache_scan.started", directories=scan_dirs,
         required_sizes=len(sizes_needed))
    report.cache_index = ArchiveCacheIndex(scan_dirs, sizes_needed, log=log)
    report.cache_index.refresh(stop, force=True)
    by_size = report.cache_index.groups()
    for folder in report.cache_index.roots:
        candidates = sum(path.parent == folder for paths in by_size.values() for path in paths)
        emit(log, "preflight.cache_scan.directory", path=folder,
             exists=folder.is_dir(), candidates=candidates)
    from .hashes import file_hash
    notify("Verifying cached downloads")
    expected_by_size = {}
    for archive in package.archives.values():
        if archive.key in required_archives and archive.kind != "GameFileSource":
            expected_by_size.setdefault(archive.size, set()).add(archive.key)
    def verify_downloads(item):
        size, paths = item
        needed = expected_by_size[size].copy()
        found = {}
        for path in paths:
            try:
                key = file_hash(path, stop)
            except InterruptedError:
                raise
            except (OSError, WabbajackError) as exc:
                emit_exception(log, "preflight.cache.unreadable", exc, path=path)
                continue
            if key in needed:
                found[key] = path
                needed.remove(key)
                if not needed:
                    break
        return found
    for found in parallel_verify(verify_downloads, by_size.items(), stop,
                                 size=lambda item: item[0]):
        report.cached.update(found)
    notify("Verifying required game files")
    def verify_game(archive):
        return archive.key, _verify_game_source(request, archive, stop, log)
    game_sources = dict(parallel_verify(verify_game,
        (archive for archive in package.archives.values()
         if archive.kind == "GameFileSource" and archive.key in required_archives | ignored_archives),
        stop, size=lambda archive: archive.size))
    loverslab_available = False
    from .hosts import is_loverslab_url, source_url
    if any(a.key in required_archives and a.key not in report.cached
           and is_loverslab_url(source_url(a)) for a in package.archives.values()):
        from Utils.loverslab.credentials import load_loverslab_credentials, CredentialStorageError
        try:
            loverslab_available = load_loverslab_credentials() is not None
        except CredentialStorageError as exc:
            emit(log, "loverslab.credentials.unavailable", reason=str(exc))
    game_file_problems = []
    ignored_game_file_problems = []
    for archive in package.archives.values():
        if archive.key not in required_archives | ignored_archives:
            continue
        if stop is not None and stop.is_set():
            raise InterruptedError("Preflight stopped")
        if archive.kind == "GameFileSource":
            name, rel, found, present, preparation = game_sources[archive.key]
            if found:
                if archive.key in required_archives:
                    report.game_files[archive.key] = found
                emit(log, "preflight.archive", archive=archive.name,
                     kind=archive.kind, bytes=archive.size, hash=archive.key,
                     route="game-file", path=found)
            else:
                if preparation and archive.key in required_archives:
                    report.prepared_game_files[archive.key] = preparation
                    emit(log, "preflight.archive", archive=archive.name,
                         kind=archive.kind, bytes=archive.size, hash=archive.key,
                         route="prepared-game-file", path=preparation.source,
                         preparation=preparation.kind)
                    check("pass", "Game file preparation",
                          f"{rel}: create the author's required 4 GB/LAA executable in the managed "
                          "installation; the original game file remains unchanged")
                elif not preparation:
                    target = (game_file_problems if archive.key in required_archives
                              else ignored_game_file_problems)
                    target.append((name, rel, present, archive))
                    emit(log, "preflight.archive", archive=archive.name,
                         kind=archive.kind, bytes=archive.size, hash=archive.key,
                         route="missing-game-file", relative=rel, present=present)
            continue
        if archive.key not in report.cached:
            report.download_bytes += archive.size
            automatic = automatic_source(archive, request.premium,
                                         loverslab_available=loverslab_available)
            emit(log, "preflight.archive", archive=archive.name,
                 kind=archive.kind, host=url_host(archive.state.get("Url", "")),
                 bytes=archive.size, hash=archive.key,
                 route="automatic" if automatic else "manual")
            if not automatic:
                check("manual", "Manual download", archive.name)
        else:
            emit(log, "preflight.archive", archive=archive.name,
                 kind=archive.kind, bytes=archive.size, hash=archive.key,
                 route="cache", path=report.cached[archive.key])
    _emit_game_file_problems(package, game_file_problems, check,
                             request.gallery_metadata.get("readme", ""))
    _emit_game_file_problems(package, ignored_game_file_problems, check,
                             request.gallery_metadata.get("readme", ""), ignored=True)
    notify("Checking reconstruction requirements")
    for directive in package.directives:
        if directive.path not in required:
            continue
        report.install_bytes += directive.size
        if directive.embedded_hash:
            check("pass", "Profile selections", f"{directive.path}: Amethyst will import and verify the author's packaged selections automatically. No action needed.")
        if directive.kind == "CreateBSA" and directive.path not in reusable:
            try:
                check_archive_state(directive.data["State"], directive.data["FileStates"])
            except (KeyError, ValueError) as exc:
                emit_exception(log, "preflight.archive_state.failed", exc,
                               path=directive.path)
                check("error", "Archive reconstruction", f"{directive.path}: {exc}")
    textures = [d for d in pending if d.kind == "TransformedTexture"]
    if textures:
        notify("Checking texture conversion")
        from .textures import probe_texture_tool, texture_parameters
        try:
            formats = {texture_parameters(directive.data["ImageState"])[3] for directive in textures}
            probe_texture_tool(request, stop, sorted(formats), log=log)
            check("pass", "Texture conversion", f"Converted and verified sample DDS files for {len(formats)} formats using {request.proton.parent.name}; ready for {len(textures):,} textures")
        except InterruptedError:
            raise
        except Exception as exc:
            emit_exception(log, "preflight.texture.failed", exc,
                           textures=len(textures))
            check("error", "Texture conversion", exc)
    from .runtime import adjustments, uses_native_runtime
    notify("Checking runtime requirements")
    native_runtime = uses_native_runtime(request.game)
    runtime_paths = required - ignored_directives
    for item in adjustments(package, request.game, request.profiles, configuration=configuration, paths=runtime_paths):
        check("pass" if item.id in request.fixes else "error" if item.required else "warning",
              "Runtime adjustment", item.label + (" (accepted)" if item.id in request.fixes else " — review this option in setup"))
    if native_runtime:
        check("pass", "Game runtime", "Native Linux runtime; no Wine/Proton prefix required")
    elif any(d.path in runtime_paths and d.path.split("/")[0].casefold() != "temp_bsa_files"
             and d.path.lower().endswith(".exe") for d in package.directives):
        prefix = request.game.get_prefix_path() if hasattr(request.game, "get_prefix_path") else None
        if not prefix or not Path(prefix).is_dir():
            check("error", "Game runtime", "Configure the selected game's Wine/Proton prefix before installation")
    archive_names = []
    for d in pending:
        if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
            key, members = archive_paths[d.index]
            if members:
                archive_names.extend([package.archives[key].name, *members[:-1]])
    if any(not name.lower().endswith((".zip", ".bsa", ".ba2", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")) for name in archive_names):
        if not any(shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za")):
            check("error", "Archive extraction", "Install 7-Zip to extract the required source archives")
    devices = {}
    notify("Calculating disk space")
    reused_bytes = sum(d.size for d in package.directives if d.path in reusable)
    if reusable:
        check("pass", "Reusable outputs", f"{reused_bytes / 1024 ** 3:.1f} GiB verified for reuse; {len(package.archives) - len(required_archives):,} source archives are no longer needed")
    staged_bytes = sum(d.size for d in pending)
    from .store import publication_copy_required
    final_bytes = 0
    linked_reuse = copied_reuse = 0
    for directive in package.directives:
        published = adapter.installed_path(directive.path) if adapter else directive.path
        if (directive.path not in required or directive.path in root_reuse
                or directive.path.split("/")[0].casefold() == "temp_bsa_files"
                or not published):
            continue
        if directive.path in stage_reuse:
            source = within(directory / "work" / "output", directive.path)
            target = within(directory / "root", published)
            if hardlinks and not publication_copy_required(source, target, directory / "work"):
                linked_reuse += 1
                continue
            copied_reuse += 1
        final_bytes += directive.size
    emit(log, "preflight.space.publication", required_bytes=final_bytes,
         linked_reusable_outputs=linked_reuse,
         copied_reusable_outputs=copied_reuse)
    profile_bytes = sum(d.size for d in package.directives if d.path.startswith("profiles/")
                        and d.path.split("/")[1] in request.profiles)
    profile_bytes += max(1, len(request.profiles)) * sum(d.size for d in package.directives if
        (adapter and adapter.root_destination(d.path) and not adapter.root_mod_destination(d.path))
        or d.path.casefold().startswith("overwrite/"))
    backups = 0
    reused_targets = {rel for path in root_reuse if (rel := adapter.installed_path(path))} if adapter else root_reuse
    for key in old_outputs:
        if key in bsa_reuse or key in setup_reuse:
            continue
        if key.startswith("root/") and key[5:] in reused_targets:
            continue
        path = directory / key if key.startswith("root/") else root / key
        if path.is_file() and not path.is_symlink():
            backups += path.stat().st_size
    extracted = {}
    selected_members = {}
    nested_outputs = {}
    nested_members = {}
    for d in pending:
        if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
            key, members = archive_paths[d.index]
            if members:
                extracted[key] = extracted.get(key, 0) + d.size
                selected_members.setdefault(key, set()).add(members[0].casefold())
                if len(members) > 1:
                    nested_outputs[key] = nested_outputs.get(key, 0) + d.size
                    nested_members.setdefault(key, set()).add(members[0].casefold())
    estimates = []
    for key, outputs in extracted.items():
        archive = package.archives[key]
        estimate = max(outputs, archive.size * 3)
        cached = report.cached.get(key, report.game_files.get(key))
        if cached and zipfile.is_zipfile(cached):
            with zipfile.ZipFile(cached) as source:
                selected = [i for i in source.infolist()
                            if i.filename.replace("\\", "/").casefold() in selected_members[key]]
                estimate = sum(i.file_size for i in selected)
                if key in nested_outputs:
                    packed = sum(i.file_size for i in selected
                                 if i.filename.replace("\\", "/").casefold() in nested_members[key])
                    estimate += max(nested_outputs[key], packed * 3)
                if any(i.flag_bits & 1 for i in source.infolist()):
                    check("error", "Archive extraction", f"Password-protected source archive is unsupported: {archive.name}")
                if any(i.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA} for i in selected):
                    if not any(shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za")):
                        check("error", "Archive extraction", f"Install 7-Zip to decode the ZIP compression used by {archive.name}")
        estimates.append(estimate)
    by_path = {d.path.casefold(): d for d in package.directives}
    merge_bytes = max((sum(by_path[p.casefold()].size for p in dependency_paths(d))
                       for d in pending if d.kind == "MergedPatch"), default=0)
    from Utils.ui.config import load_collection_settings
    workers = max(1, load_collection_settings()["max_extract_workers"])
    temporary = max(sum(sorted(estimates, reverse=True)[:workers]), merge_bytes)
    missing = [a for a in package.archives.values() if a.key in required_archives
               and a.key not in report.cached and a.kind != "GameFileSource"]
    from .acquire import partial_download_space
    partial_bytes = sum(partial_download_space(a, request.downloads, stop, log) for a in missing)
    emit(log, "preflight.space.partial_downloads", reusable_bytes=partial_bytes)
    if request.clear_archives:
        from .archive_cache import download_budget, download_space
        report.archive_budget_bytes = download_budget(missing)
        download_bytes = min(report.archive_budget_bytes,
                             max(0, sum(download_space(a) for a in missing) - partial_bytes))
        download_label = "bounded downloads and multipart assembly"
        check("pass", "Archive cleanup",
              "Clear archive after install is enabled. Newly acquired archives are removed after their required outputs are verified and saved. "
              f"Downloads awaiting processing have a {report.archive_budget_bytes / 1024 ** 3:.1f} GiB working allowance, including assembly and failed-transfer replacements. "
              "Original local archives are kept; repairs or updates may need downloads again.")
    else:
        multipart = sum(a.size for a in missing if a.kind == "WabbajackCDN")
        download_bytes = max(0, report.download_bytes + multipart - partial_bytes)
        download_label = "downloads and multipart assembly"
    emit(log, "preflight.space.archive_cache", clear_archives=request.clear_archives,
         required_bytes=download_bytes, download_bytes=report.download_bytes)
    sizes = [(request.downloads, download_bytes, download_label),
             (directory, setup_bytes, "additional setup, staging and generated mods"),
             (directory, bsa_bytes, "vanilla BSA setup and audio conversion"),
             (directory, (max(staged_bytes, final_bytes) if hardlinks else staged_bytes + final_bytes) + 2 * profile_bytes + backups, "installation working set and update backups"),
             (directory, temporary, "estimated temporary extraction")]
    for path, count, label in sizes:
        parent = existing_parent(path)
        try:
            stat = parent.stat()
            device = devices.setdefault(stat.st_dev, [parent, 0, []])
            device[1] += count
            device[2].append(label)
            emit(log, "preflight.space.component", path=path,
                 existing_parent=parent, bytes=count, label=label,
                 device=stat.st_dev)
            if not os.access(parent, os.W_OK | os.X_OK):
                check("error", "Permissions", f"Cannot write to {path}")
        except OSError as exc:
            emit_exception(log, "preflight.space.failed", exc, path=path,
                           bytes=count, label=label)
            check("error", "Filesystem", exc)
    for parent, count, labels in devices.values():
        available = shutil.disk_usage(parent).free
        reserve = max(512 * 1024 ** 2, int(count * 0.1))
        emit(log, "preflight.space.device", path=parent, required_bytes=count,
             reserve_bytes=reserve, available_bytes=available,
             components=labels)
        check("pass" if available >= count + reserve else "error", "Disk space",
              f"{', '.join(labels)}: need {(count + reserve) / 1024 ** 3:.1f} GiB including reserve; {available / 1024 ** 3:.1f} GiB available at {parent}")
    check("warning", "Linux compatibility", "Amethyst reminder: successful file reconstruction does not verify that every mod or bundled tool supports native Linux. This is not an author-supplied warning."
          if native_runtime else "Amethyst reminder: successful file reconstruction does not verify every Windows mod or tool under Proton. This is not an author-supplied warning.")
    return report
