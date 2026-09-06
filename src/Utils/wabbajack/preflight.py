from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from .archive_build import check_archive_state
from .games import matches_game, token
from .hashes import XXHash, verify_file
from .models import Check, PreflightReport
from .paths import WabbajackError, existing_parent, source_path


def _reusable(request, stop, check):
    import sqlite3
    from .hashes import file_hash
    from .paths import within
    from .reconstruct import signature
    directory = request.directory
    old, completed, root_reuse, stage_reuse = {}, {}, set(), set()
    if not (directory / "state.sqlite").is_file():
        return root_reuse, stage_reuse, old
    try:
        with sqlite3.connect((directory / "state.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            old = {p: (sig, digest) for p, sig, digest in db.execute("SELECT path,signature,authored_hash FROM outputs")}
            completed = {p: (sig, digest) for p, sig, digest in db.execute("SELECT path,signature,actual_hash FROM completed")}
        for directive in request.package.directives:
            sig = signature(directive, request)
            previous = old.get("root/" + directive.path)
            staged = completed.get(directive.path)
            for row, base, found in ((previous, directory / "root", root_reuse),
                                     (staged, directory / "work" / "output", stage_reuse)):
                if row and row[0] == sig:
                    path = within(base, directive.path)
                    if path.is_file() and file_hash(path, stop) == row[1]:
                        found.add(directive.path)
    except (OSError, sqlite3.Error, WabbajackError) as exc:
        check("warning", "Reusable outputs", f"Existing content must be revalidated during installation: {exc}")
    return root_reuse, stage_reuse, old


def preflight(request, stop=None) -> PreflightReport:
    package = request.package
    report = PreflightReport()
    def check(status, name, detail):
        report.checks.append(Check(status, name, str(detail)))
    if request.game.get_deploy_active():
        check("error", "Deployment", "Restore the deployed game before installing or updating a modlist")
    try:
        from Utils.filegraph.service import require_native
        require_native()
    except Exception as exc:
        check("error", "File catalog", f"The native Filegraph component is required: {exc}")
    try:
        XXHash()
        with zipfile.ZipFile(package.path) as archive:
            for member in archive.infolist():
                with archive.open(member) as stream:
                    while stream.read(1024 * 1024):
                        if stop is not None and stop.is_set():
                            raise InterruptedError("Preflight stopped")
        check("pass", "Package", f"{package.name} {package.version}; {len(package.directives):,} output files")
    except InterruptedError:
        raise
    except (WabbajackError, zipfile.BadZipFile, OSError) as exc:
        check("error", "Package integrity", exc)
    if not matches_game(request.game, package.game):
        check("error", "Game", f"This list requires {package.game}; selected {request.game.name}")
    if request.mode not in {"install", "resume", "repair", "update"}:
        check("error", "Operation", "Unknown installation operation")
    for required_game in package.metadata.get("OtherGames", []):
        if not any(token(n) == token(required_game) for n in request.game_roots):
            check("error", "Additional game", f"Configure the required {required_game} game and its original source directory")
    from .manifest import stock_folder
    from .adapters import adapter_for
    stock = ""
    adapter = None
    try:
        adapter = adapter_for(package, request.game)
        stock = stock_folder(package)
        if stock:
            check("pass", "Stock game launch", f"Launch the reconstructed game at {request.directory / 'root' / stock}; retain the original game for source verification")
    except WabbajackError as exc:
        check("error", "Game layout", exc)
    if adapter:
        deployed = {}
        for directive in package.directives:
            dest = adapter.root_destination(directive.path)
            if dest:
                previous = deployed.setdefault(dest.casefold(), directive.path)
                if previous != directive.path:
                    check("error", "Game-root conflict", f"{previous} and {directive.path} both deploy to {dest}")
        if deployed and not getattr(request.game, "root_folder_deploy_enabled", True):
            check("error", "Game-root deployment", "The selected game handler does not support this package's root payload layout")
    unknown_folders = {d.path.split("/")[0] for d in package.directives
                       if any(word in d.path.split("/")[0].casefold() for word in ("manual install", "root mods"))}
    for folder in sorted(unknown_folders):
        check("manual", "Author instructions", f"Review the files in {folder}. Their purpose is not declared as a game-root layout; follow the list's README before launching.")
    root = Path(request.game.get_profile_root()).resolve()
    directory = request.directory.resolve()
    if directory.parent != root / ".wabbajack" or request.directory.is_symlink():
        check("error", "Installation directory", "Choose a directory directly inside the game's .wabbajack directory")
    for name in ("root", "work", "backups", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm", "install.lock"):
        if (directory / name).is_symlink():
            check("error", "Installation directory", f"Managed entry cannot be a symbolic link: {name}")
    from .store import installation_info
    info = installation_info(directory)
    if request.mode == "install" and directory.exists() and any(directory.iterdir()):
        check("error", "Installation directory", "Choose an empty managed directory, or use Resume, Repair or Update")
    elif request.mode != "install":
        if not info:
            check("error", "Installation", "No managed installation exists at this location")
        elif request.mode in {"resume", "repair"} and package.identity != info.get("pending_package", info.get("package_identity")):
            check("error", "Package identity", "Resume and Repair require the saved package. Use Update for a different authored version.")
        else:
            from .profiles import referenced_profiles
            affected = referenced_profiles(directory, root)
            check("warning", "Affected profiles", ", ".join(p.name for p in affected) or "No published profiles")
            from .planning import plan_update
            plan = plan_update(request)
            check("pass", "Update preview", f"{len(plan.added):,} added, {len(plan.changed):,} changed, {len(plan.removed):,} obsolete authored files. Local changes are compared before publication.")
            old_profiles = set(info.get("selected_profiles", []))
            new_profiles = set(request.profiles)
            if old_profiles != new_profiles:
                check("warning", "Authored profile changes", f"Added: {', '.join(sorted(new_profiles - old_profiles)) or 'none'}; removed: {', '.join(sorted(old_profiles - new_profiles)) or 'none'}. Removed profiles remain available for review.")
    for game_root in request.game_roots.values():
        for dest in (directory, request.downloads.resolve()):
            game_root = game_root.resolve()
            if dest == game_root or dest.is_relative_to(game_root) or game_root.is_relative_to(dest):
                check("error", "Path overlap", f"{dest} overlaps the game directory")
    if directory == request.downloads.resolve() or directory.is_relative_to(request.downloads.resolve()) or request.downloads.resolve().is_relative_to(directory):
        check("error", "Path overlap", "Downloads and managed installation must be separate")
    if package.profiles and (not request.profiles or any(p not in package.profiles for p in request.profiles)):
        check("error", "Profiles", "Select at least one authored profile")
    if any(d.kind == "RemappedInlineFile" for d in package.directives):
        from .runtime import windows_path
        try:
            for path in [directory / "root", request.downloads, *request.game_roots.values()]:
                windows_path(request.game, path)
        except WabbajackError as exc:
            check("error", "Windows path mapping", exc)
    provided_mods = {d.path.split("/")[1].casefold() for d in package.directives if d.path.startswith("mods/")}
    profile_lists = {}
    with zipfile.ZipFile(package.path) as archive:
        for directive in package.directives:
            parts = directive.path.split("/")
            member = directive.data.get("SourceDataID")
            if not member:
                continue
            is_list = (len(parts) == 3 and parts[0] == "profiles" and parts[1] in request.profiles
                       and parts[2].casefold() in {"modlist.txt", "plugins.txt"})
            if directive.kind != "RemappedInlineFile" and not is_list:
                continue
            if archive.getinfo(member).file_size > 8 * 1024 * 1024:
                check("error", "Configuration file", f"{directive.path} exceeds the 8 MiB configuration limit")
                continue
            try:
                lines = archive.read(member).decode("utf-8-sig").splitlines()
            except UnicodeError:
                check("error", "Configuration file", f"{directive.path} is not UTF-8 text")
                continue
            if is_list:
                profile_lists[(parts[1], parts[2].casefold())] = lines
        for (profile, kind), lines in profile_lists.items():
            if kind != "modlist.txt":
                continue
            for line in lines:
                if not line.startswith("+") or line.casefold().endswith("_separator"):
                    continue
                name = line[1:]
                if name.casefold() not in provided_mods:
                    try:
                        existing = source_path(directory / "root" / "mods", name)
                        available = existing.is_dir() and any(existing.iterdir())
                    except (OSError, WabbajackError):
                        available = False
                    if not available:
                        check("error", "Required external mod", f"Profile {profile} enables '{name}', which this package does not provide. Follow the author's external setup requirements or deselect this profile.")
    vanilla = {p.casefold() for p in [*getattr(request.game, "vanilla_plugins", []),
                                     *getattr(request.game, "vanilla_dlc_plugins", [])]}
    provided_plugins = {Path(d.path).name.casefold() for d in package.directives if Path(d.path).suffix.casefold() in {".esm", ".esp", ".esl"}}
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
                except (OSError, WabbajackError):
                    pass
            if not available:
                check("error", "Required DLC or game plugin", f"Profile {profile} enables {name}, which is missing from the original game and reconstructed files")
    from .manifest import archive_path, required_directives, dependency_paths
    root_reuse, stage_reuse, old_outputs = _reusable(request, stop, check)
    reusable = root_reuse | stage_reuse
    required = required_directives(package, reusable)
    pending = [d for d in package.directives if d.path in required and d.path not in reusable]
    required_archives = {archive_path(d.data)[0] for d in pending
                         if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}}
    from Utils.downloads.core import get_scan_dirs
    scan_dirs = [request.downloads, *get_scan_dirs(request.game.name)]
    by_size = {}
    for folder in scan_dirs:
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_file() and not path.name.endswith((".part", ".tmp")):
                    by_size.setdefault(path.stat().st_size, []).append(path)
    hashes = {}
    from .hashes import file_hash
    for archive in package.archives.values():
        if archive.key not in required_archives:
            continue
        if stop is not None and stop.is_set():
            raise InterruptedError("Preflight stopped")
        if archive.kind == "GameFileSource":
            name = str(archive.state.get("Game", archive.state.get("GameName", package.game)))
            game_root = next((p for n, p in request.game_roots.items() if token(n) == token(name)), None)
            rel = archive.state.get("GameFile", archive.name)
            found = None
            if game_root:
                for candidate in (rel, "Data/" + rel):
                    try:
                        path = source_path(game_root, candidate)
                        if verify_file(path, archive.key, archive.size, stop):
                            found = path
                            break
                    except (OSError, WabbajackError):
                        pass
            if found:
                report.game_files[archive.key] = found
            else:
                check("error", "Required game file", f"{name}: {rel} is missing or differs from the required version. Check store, game version, language and DLC.")
            continue
        for path in by_size.get(archive.size, []):
            if path not in hashes:
                hashes[path] = file_hash(path, stop)
            if hashes[path] == archive.key:
                report.cached[archive.key] = path
                break
        if archive.key not in report.cached:
            report.download_bytes += archive.size
            if archive.kind not in {"Http", "HTTP", "WabbajackCDN"} and not (archive.kind == "Nexus" and request.premium):
                check("manual", "Manual download", archive.name)
    for directive in package.directives:
        report.install_bytes += directive.size
        if directive.embedded_hash:
            check("warning", "Compiled profile selections", f"{directive.path}: the embedded profile differs from the original-file metadata. Import and verify the packaged selections exactly ({directive.output_size:,} bytes).")
        if directive.kind == "CreateBSA":
            try:
                check_archive_state(directive.data["State"], directive.data["FileStates"])
            except (KeyError, ValueError) as exc:
                check("error", "Archive reconstruction", f"{directive.path}: {exc}")
    textures = [d for d in pending if d.kind == "TransformedTexture"]
    if textures:
        from .textures import probe_texture_tool, texture_parameters
        try:
            for directive in textures:
                texture_parameters(directive.data["ImageState"])
            probe_texture_tool(request, stop)
            check("pass", "Texture conversion", f"Texconv is ready for {len(textures):,} textures")
        except Exception as exc:
            check("error", "Texture conversion", exc)
    from .runtime import adjustments
    for item in adjustments(package, request.game):
        check("pass" if item.id in request.fixes else "error" if item.required else "warning",
              "Runtime adjustment", item.label + (" (accepted)" if item.id in request.fixes else " — review this option in setup"))
    if any(d.path.lower().endswith(".exe") for d in package.directives):
        prefix = request.game.get_prefix_path() if hasattr(request.game, "get_prefix_path") else None
        if not prefix or not Path(prefix).is_dir():
            check("error", "Game runtime", "Configure the selected game's Wine/Proton prefix before installation")
    archive_names = []
    for d in pending:
        if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
            key, members = archive_path(d.data)
            if members:
                archive_names.extend([package.archives[key].name, *members[:-1]])
    if any(not name.lower().endswith((".zip", ".bsa", ".ba2", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")) for name in archive_names):
        if not any(shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za")):
            check("error", "Archive extraction", "Install 7-Zip to extract the required source archives")
    devices = {}
    reused_bytes = sum(d.size for d in package.directives if d.path in reusable)
    if reusable:
        check("pass", "Reusable outputs", f"{reused_bytes / 1024 ** 3:.1f} GiB verified for reuse; {len(package.archives) - len(required_archives):,} source archives are no longer needed")
    staged_bytes = sum(d.size for d in pending)
    final_bytes = sum(d.size for d in package.directives if d.path not in root_reuse and d.path.split("/")[0].casefold() != "temp_bsa_files")
    profile_bytes = sum(d.size for d in package.directives if d.path.startswith("profiles/")
                        and d.path.split("/")[1] in request.profiles)
    profile_bytes += max(1, len(request.profiles)) * sum(d.size for d in package.directives if
        (adapter and adapter.root_destination(d.path)) or d.path.casefold().startswith("overwrite/"))
    backups = 0
    for key in old_outputs:
        if key.startswith("root/") and key[5:] in root_reuse:
            continue
        path = directory / key if key.startswith("root/") else root / key
        if path.is_file() and not path.is_symlink():
            backups += path.stat().st_size
    extracted = {}
    for d in pending:
        if d.kind in {"FromArchive", "PatchedFromArchive", "TransformedTexture"}:
            key, members = archive_path(d.data)
            if members:
                extracted[key] = extracted.get(key, 0) + d.size
    estimates = []
    for key, outputs in extracted.items():
        archive = package.archives[key]
        estimate = max(outputs, archive.size * 3)
        cached = report.cached.get(key, report.game_files.get(key))
        if cached and zipfile.is_zipfile(cached):
            with zipfile.ZipFile(cached) as source:
                estimate = max(estimate, sum(i.file_size for i in source.infolist()))
                if any(i.flag_bits & 1 for i in source.infolist()):
                    check("error", "Archive extraction", f"Password-protected source archive is unsupported: {archive.name}")
                if any(i.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA} for i in source.infolist()):
                    if not any(shutil.which(n) for n in ("7zzs", "7zz", "7z", "7za")):
                        check("error", "Archive extraction", f"Install 7-Zip to decode the ZIP compression used by {archive.name}")
        estimates.append(estimate)
    by_path = {d.path.casefold(): d for d in package.directives}
    merge_bytes = max((sum(by_path[p.casefold()].size for p in dependency_paths(d))
                       for d in pending if d.kind == "MergedPatch"), default=0)
    temporary = max(sum(sorted(estimates, reverse=True)[:3]), merge_bytes)
    multipart = sum(a.size for a in package.archives.values() if a.key in required_archives and a.kind == "WabbajackCDN" and a.key not in report.cached)
    sizes = [(request.downloads, report.download_bytes + multipart, "downloads and multipart assembly"),
             (directory, staged_bytes + final_bytes + 2 * profile_bytes + backups, "installation, staging and update backups"),
             (directory, temporary, "estimated temporary extraction")]
    for path, count, label in sizes:
        parent = existing_parent(path)
        try:
            stat = parent.stat()
            device = devices.setdefault(stat.st_dev, [parent, 0, []])
            device[1] += count
            device[2].append(label)
            if not os.access(parent, os.W_OK | os.X_OK):
                check("error", "Permissions", f"Cannot write to {path}")
        except OSError as exc:
            check("error", "Filesystem", exc)
    for parent, count, labels in devices.values():
        available = shutil.disk_usage(parent).free
        reserve = max(512 * 1024 ** 2, int(count * 0.1))
        check("pass" if available >= count + reserve else "error", "Disk space",
              f"{', '.join(labels)}: need {(count + reserve) / 1024 ** 3:.1f} GiB including reserve; {available / 1024 ** 3:.1f} GiB available at {parent}")
    try:
        parent = existing_parent(request.directory)
        with tempfile.TemporaryDirectory(prefix=".amethyst-preflight-", dir=parent) as tmp:
            p = Path(tmp)
            (p / "source").write_bytes(b"ok")
            os.link(p / "source", p / "hardlink")
            (p / "link").symlink_to("source")
            if (p / "link").read_bytes() != b"ok":
                raise OSError("Symbolic links are unavailable")
            (p / "Case").write_bytes(b"a")
            if (p / "case").exists():
                check("warning", "Filesystem", "Case-insensitive installation filesystem")
    except OSError as exc:
        check("error", "Filesystem capabilities", exc)
    check("warning", "Linux compatibility", "Read the author's requirements. Successful file reconstruction does not verify every Windows mod under Proton.")
    return report
