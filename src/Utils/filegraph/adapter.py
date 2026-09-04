"""Turn game rules and raw mod manifests into filegraph candidate batches.

All filesystem and game-specific interpretation lives here.  Reconciliation
after an enable/disable/reorder submits only a compact ``ProfileIntent`` to
Rust; it never walks mod directories or serializes a complete winner map.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from Utils.filegraph.models import FileGraphCancelled, RefreshProgress
from Utils.filegraph.constants import OVERWRITE_NAME, ROOT_FOLDER_NAME
from Utils.filegraph.archives import owning_plugin, pak_name_rank, scan_mod_archives
from Utils.filegraph.identities import (
    bg3_uuid_conflicts_enabled, is_multipart_pak,
)
from Utils.games.frameworks import framework_exe_candidates

# Candidate flags consumed directly by ConflictSummary/UI filters.
FLAG_PRE_RTX = 1 << 0
FLAG_ROOT_RULE = 1 << 1
FLAG_PLUGIN = 1 << 2
FLAG_ARCHIVE = 1 << 3
FLAG_FRAMEWORK = 1 << 4
FLAG_TEXT = 1 << 5
# Raw-inventory-only marker: the retired mod index retained this file after
# the game's install-extension filter.  Candidate flags never need it, but
# inventory filters and wizard/plugin scans use it to preserve index parity
# while the catalog still keeps a complete raw manifest.
FLAG_INDEXED = 1 << 6
# Raw-inventory identity for a plugin that the game's routing places at the
# top of its plugin directory.  This is intentionally independent of profile
# exclusions: plugin-state cleanup uses it to distinguish a disabled mod's
# plugin from a genuinely stale plugins.txt entry.
FLAG_STAGED_PLUGIN = 1 << 7
# The retired index's normal/root partition for raw-inventory consumers.  This
# does not activate the file when profile exclusions suppress its candidate.
FLAG_INDEX_ROOT = 1 << 8

_TEXT_EXTENSIONS = frozenset({
    ".ini", ".json", ".toml", ".txt", ".cfg", ".conf", ".config",
    ".yaml", ".yml", ".xml", ".log", ".md",
})


@dataclass(frozen=True, slots=True)
class RawFile:
    relative: bytes
    display: str
    size: int
    mtime_ns: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class _Route:
    target: str
    destination_key: bytes
    destination_display: str
    legacy_rel: str
    root: bool = False
    root_rule: bool = False
    # Whether BaseGame's destination-only path/extension remaps still need to
    # be applied. Handler, UE, custom, and separator routes already describe
    # their final domain and must not be remapped a second time.
    deploy_remap: bool = True


def _wire_path(value: str) -> bytes:
    return value.replace("\\", "/").lower().encode(
        "utf-8", "surrogateescape")


def _display_path(value: bytes) -> str:
    return os.fsdecode(value).replace("\\", "/")


def _normalise_mod_key(name: str) -> str:
    return name.lower()


def _cancelled(cancel) -> bool:
    if cancel is None:
        return False
    fn = getattr(cancel, "is_cancelled", None)
    return bool(fn()) if callable(fn) else bool(getattr(cancel, "cancelled", False))


def _check_cancel(cancel) -> None:
    if _cancelled(cancel):
        raise FileGraphCancelled("filegraph operation cancelled")


def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(map(str, value))
    if hasattr(value, "__dict__"):
        return {
            key: item for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _hash_payload(value) -> bytes:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8", "surrogateescape")
    return hashlib.blake2b(payload, digest_size=20).digest()


class GameCandidateAdapter:
    """Build complete per-mod candidates using one whole-manifest pass."""

    def __init__(self, game, profile_dir: Path, *, log_fn=None,
                 staging_dir: Path | None = None):
        self.game = game
        self.profile_dir = Path(profile_dir)
        self.staging = (Path(staging_dir) if staging_dir is not None
                        else Path(game.get_effective_mod_staging_path()))
        self.library_root = self.staging.parent
        self.overwrite = self.library_root / "overwrite"
        self.root_folder = self.library_root / "Root_Folder"
        self.log = log_fn or (lambda _message: None)
        self._root_mods: set[str] = set()
        self._raw_root_files: dict[str, set[str]] = {}
        self._raw_excluded: dict[str, set[str]] = {}
        self._per_mod_strips: dict[str, list[str]] = {}
        self._archive_units: list[tuple[tuple, str]] = []
        self._archive_order: list[str] = []
        self._plugin_rank: dict[str, int] = {}
        self._mod_low_rank: dict[str, int] = {}
        self._top_level_exempt: set[str] = set()
        self._normalize_folder_case = True
        self._per_mod_deploy: dict[str, Path] = {}
        self._handler_mod_deploy: dict[str, tuple] = {}
        self._raw_route_mods: set[str] = set()
        self._rules_hash_cache: bytes | None = None
        self._refresh_profile_rules()

    def _refresh_profile_rules(self) -> None:
        self._rules_hash_cache = None
        modlist = self.profile_dir / "modlist.txt"
        try:
            from Nexus.nexus_meta import collect_root_flagged_mods
            self._root_mods = collect_root_flagged_mods(
                modlist, self.staging, log_fn=self.log)
        except Exception as exc:
            self.log(f"Root-folder rule scan warning: {exc}")
            self._root_mods = set()
        try:
            from Utils.mods.files import excluded_raw_by_mod
            self._raw_excluded = excluded_raw_by_mod(self.profile_dir)
        except Exception:
            self._raw_excluded = {}
        try:
            from Utils.profiles.state import read_root_mod_files
            self._raw_root_files = {
                name: {str(path).replace("\\", "/").lower() for path in paths}
                for name, paths in read_root_mod_files(
                    self.profile_dir, None).items()
            }
        except Exception:
            self._raw_root_files = {}
        try:
            from Utils.deployment import load_per_mod_strip_prefixes
            self._per_mod_strips = load_per_mod_strip_prefixes(self.profile_dir)
        except Exception:
            self._per_mod_strips = {}
        try:
            from Utils.deployment.shared import (
                expand_separator_deploy_paths, expand_separator_raw_deploy,
                load_separator_deploy_paths,
            )
            from Utils.mods.modlist import read_modlist
            separators = load_separator_deploy_paths(self.profile_dir)
            entries = (read_modlist(self.profile_dir / "modlist.txt")
                       if separators else [])
            self._per_mod_deploy = expand_separator_deploy_paths(
                separators, entries)
            self._raw_route_mods = expand_separator_raw_deploy(
                separators, entries)
        except Exception:
            self._per_mod_deploy = {}
            self._raw_route_mods = set()
            entries = []
        try:
            if not entries:
                from Utils.mods.modlist import read_modlist
                entries = read_modlist(self.profile_dir / "modlist.txt")
            hook = getattr(self.game, "mod_deploy_specs", None)
            self._handler_mod_deploy = (
                dict(hook(self.profile_dir, self.staging,
                          [e.name for e in entries if not e.is_separator]) or {})
                if callable(hook) else {}
            )
            for name in self._per_mod_deploy:
                self._handler_mod_deploy.pop(name, None)
        except Exception as exc:
            self.log(f"Handler destination scan warning: {exc}")
            self._handler_mod_deploy = {}
        self._top_level_exempt = self._top_level_exempt_mods()
        try:
            from Utils.ui.config import load_normalize_folder_case
            self._normalize_folder_case = bool(
                getattr(self.game, "normalize_folder_case", True)
                and load_normalize_folder_case())
        except Exception:
            self._normalize_folder_case = bool(
                getattr(self.game, "normalize_folder_case", True))

    def rules_hash(self) -> bytes:
        if self._rules_hash_cache is not None:
            return self._rules_hash_cache
        game = self.game
        from Utils.filegraph.native import ENGINE_REVISION, RULES_REVISION
        relevant = {
            "engine_revision": ENGINE_REVISION,
            "rules_revision": RULES_REVISION,
            "game_id": getattr(game, "game_id", getattr(game, "name", "")),
            "strip": getattr(game, "mod_folder_strip_prefixes", ()),
            "post_strip": getattr(game, "mod_folder_strip_prefixes_post", ()),
            "extensions": getattr(game, "mod_install_extensions", ()),
            "exclude_dirs": getattr(game, "filemap_exclude_dirs", ()),
            "ignore_files": getattr(game, "conflict_ignore_filenames", ()),
            "ignore_folders": getattr(game, "conflict_ignore_foldernames", ()),
            "exclude_loose": getattr(game, "excluded_loose_filenames", ()),
            "required_top": getattr(game, "mod_required_top_level_folders", ()),
            "filter_top": getattr(game, "filemap_exclude_unknown_top_level", False),
            "routing": getattr(game, "custom_routing_rules", ()),
            "ue_routing": getattr(game, "ue5_routing_rules", ()),
            "ue_default": getattr(game, "ue5_default_dest", ""),
            "deploy_path_remap": getattr(game, "mod_deploy_path_remap", {}),
            "deploy_extension_remap": getattr(
                game, "pak_hash_extension_remap", {}),
            "casing": getattr(game, "filemap_casing", "upper"),
            "pins": getattr(game, "filemap_casing_pins", None),
            "normalize": self._normalize_folder_case,
            "archive_extensions": getattr(game, "archive_extensions", ()),
            "plugin_extensions": getattr(game, "plugin_extensions", ()),
            "frameworks": getattr(game, "frameworks", {}),
            "root_mods": sorted(self._root_mods),
            "root_files": self._raw_root_files,
            "excluded": self._raw_excluded,
            "per_mod_strips": self._per_mod_strips,
            "per_mod_deploy": self._per_mod_deploy,
            "handler_mod_deploy": self._handler_mod_deploy,
            "raw_route_mods": sorted(self._raw_route_mods),
        }
        self._rules_hash_cache = _hash_payload(relevant)
        return self._rules_hash_cache

    def variant_key(self, mod_name: str) -> str:
        per_mod = {
            "rules": self.rules_hash().hex(),
            "strip": self._per_mod_strips.get(mod_name, ()),
            "root": mod_name in self._root_mods,
            "root_files": sorted(self._raw_root_files.get(mod_name, ())),
            "excluded": sorted(self._raw_excluded.get(mod_name, ())),
            "handler_mod_deploy": self._handler_mod_deploy.get(mod_name),
        }
        return _hash_payload(per_mod).hex()

    def _scan_root(self, root: Path, cancel=None) -> list[RawFile]:
        """Byte-preserving, non-symlink-following manifest walk."""
        from Utils.filegraph.paths import EXCLUDE_NAMES, is_macos_junk

        excluded_dirs = {
            str(name).lower()
            for name in (getattr(self.game, "filemap_exclude_dirs", None) or ())
        }
        root_bytes = os.fsencode(root)
        pending: list[tuple[bytes, bytes]] = [(b"", root_bytes)]
        rows: list[tuple[bytes, int, int]] = []
        while pending:
            _check_cancel(cancel)
            prefix, current = pending.pop()
            try:
                iterator = os.scandir(current)
            except OSError as exc:
                self.log(f"Manifest scan skipped unreadable folder {os.fsdecode(current)}: {exc}")
                continue
            with iterator:
                for entry in iterator:
                    _check_cancel(cancel)
                    try:
                        name = bytes(entry.name)
                        display_name = os.fsdecode(name)
                        if entry.is_dir(follow_symlinks=False):
                            lower = display_name.lower()
                            if (lower in excluded_dirs or is_macos_junk(display_name)
                                    or display_name.startswith("prefix_")
                                    or display_name == ".mm_bundle"):
                                continue
                            pending.append((prefix + name + b"/", bytes(entry.path)))
                        elif entry.is_file(follow_symlinks=False):
                            if display_name in EXCLUDE_NAMES or is_macos_junk(display_name):
                                continue
                            stat = entry.stat(follow_symlinks=False)
                            rows.append((
                                prefix + name,
                                int(stat.st_size),
                                int(stat.st_mtime_ns),
                            ))
                    except OSError:
                        continue
        rows.sort(key=lambda row: row[0].lower())
        return [
            RawFile(relative=relative, display=_display_path(relative),
                    size=size, mtime_ns=mtime_ns, ordinal=index)
            for index, (relative, size, mtime_ns) in enumerate(rows)
        ]

    def _strip(self, mod_name: str, relative: str) -> str:
        if mod_name in self._root_mods or mod_name in self._raw_route_mods:
            return relative
        path = relative.replace("\\", "/")
        configured = self._per_mod_strips.get(mod_name, ())
        full_paths = sorted(
            (item for item in configured if "/" in item), key=len, reverse=True)
        lower = path.lower()
        for prefix in full_paths:
            prefix_lower = prefix.lower().strip("/")
            if lower == prefix_lower or lower.startswith(prefix_lower + "/"):
                path = path[len(prefix.strip("/")):].lstrip("/")
                break
        segments = {
            str(item).lower() for item in configured if "/" not in item
        }
        segments.update(
            str(item).lower()
            for item in (getattr(self.game, "mod_folder_strip_prefixes", None) or ())
        )
        while "/" in path:
            head, tail = path.split("/", 1)
            if head.lower() not in segments:
                break
            path = tail
        return path

    def _top_level_exempt_mods(self) -> set[str]:
        if not getattr(self.game, "filemap_exclude_unknown_top_level", False):
            return set()
        hook = getattr(self.game, "filemap_top_level_exempt_mods", None)
        if not callable(hook):
            return set()
        try:
            return set(hook(self.profile_dir / "modlist.txt", self.staging) or ())
        except Exception as exc:
            self.log(f"Top-level exemption check warning: {exc}")
            return set()

    def _accept(self, mod_name: str, raw_lower: str, routed_rel: str) -> bool:
        routed_lower = routed_rel.lower()
        if raw_lower in self._raw_excluded.get(mod_name, ()):
            return False
        allowed_extensions = {
            str(item).lower()
            for item in (getattr(self.game, "mod_install_extensions", None) or ())
        }
        if allowed_extensions:
            filename = routed_lower.rsplit("/", 1)[-1]
            if not any(filename.endswith(ext) and len(filename) > len(ext)
                       for ext in allowed_extensions):
                return False
        filename = routed_lower.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(filename, str(pattern).lower())
               for pattern in (getattr(self.game, "conflict_ignore_filenames", None) or ())):
            return False
        if "/" not in routed_lower and any(
                fnmatch.fnmatchcase(filename, str(pattern).lower())
                for pattern in (getattr(self.game, "excluded_loose_filenames", None) or ())):
            return False
        folder_patterns = tuple(
            str(pattern).lower()
            for pattern in (getattr(self.game, "conflict_ignore_foldernames", None) or ())
        )
        if folder_patterns and "/" in routed_lower:
            if any(fnmatch.fnmatchcase(segment, pattern)
                   for segment in routed_lower.rsplit("/", 1)[0].split("/")
                   for pattern in folder_patterns):
                return False
        if (getattr(self.game, "filemap_exclude_unknown_top_level", False)
                and mod_name not in self._top_level_exempt
                and "/" in routed_lower):
            allowed = {
                str(folder).lower()
                for folder in (getattr(
                    self.game, "mod_required_top_level_folders", None) or ())
            }
            if allowed and routed_lower.split("/", 1)[0] not in allowed:
                return False
        handler_spec = self._handler_mod_deploy.get(mod_name)
        if handler_spec is not None:
            allowed_extensions = tuple(
                str(item).casefold() for item in
                (handler_spec[2] if len(handler_spec) > 2 else ()))
            if (allowed_extensions
                    and not filename.endswith(allowed_extensions)):
                return False
        allow_default = getattr(self.game, "filegraph_allow_default_path", None)
        if (handler_spec is None
                and mod_name not in self._root_mods
                and raw_lower not in self._raw_root_files.get(mod_name, ())
                and mod_name not in self._per_mod_deploy
                and callable(allow_default)
                and not allow_default(mod_name, routed_rel)):
            return False
        return True

    def _default_domain(self) -> tuple[str, str]:
        try:
            game_root = self.game.get_game_path()
            data_root = self.game.get_mod_data_path()
        except Exception:
            game_root = data_root = None
        if data_root is None:
            return "game", ""
        if game_root is not None:
            try:
                relative = Path(data_root).relative_to(Path(game_root))
            except ValueError:
                pass
            else:
                prefix = "" if relative == Path(".") else relative.as_posix()
                return "game", prefix
        try:
            canonical = str(Path(data_root).resolve(strict=False))
        except OSError:
            canonical = str(data_root)
        return "custom:" + canonical, ""

    @staticmethod
    def _join(prefix: str, relative: str) -> str:
        prefix = prefix.replace("\\", "/").strip("/")
        relative = relative.replace("\\", "/").strip("/")
        return f"{prefix}/{relative}" if prefix and relative else prefix or relative

    def _routes_for_manifest(
        self, mod_name: str, staged: list[str], roots: set[str], *,
        root_rule_mod: bool | None = None,
    ) -> dict[str, list[_Route]]:
        target, data_prefix = self._default_domain()
        out: dict[str, list[_Route]] = {}
        normal = [path for path in staged if path.lower() not in roots]

        # The root-rule icon means "this mod matched an explicit rule whose
        # destination is the game root".  It must not be guessed from the
        # resolved path: games that normally deploy to their root (Elden Ring
        # among them) would otherwise flag every mod, while handler-remapped
        # script extenders could lose the flag entirely.
        if root_rule_mod is None:
            root_rule_mod = self._matches_root_rule(mod_name, staged)

        # Handler-specific deploy remaps belong here, before destination
        # identity is interned. Raw-deploy separator groups explicitly bypass
        # them and retain their staged layout.
        route_hook = getattr(self.game, "filegraph_route_path", None)
        if callable(route_hook) and mod_name not in self._raw_route_mods:
            for path in normal:
                try:
                    destination, final_rel = route_hook(path)
                except Exception as exc:
                    self.log(f"Candidate route warning for {mod_name}: {exc}")
                    continue
                full = self._join(destination, final_rel)
                out[path.lower()] = [_Route(
                    "game", _wire_path(full), full, full,
                    deploy_remap=False)]

        # UE handlers already expose the exact whole-manifest sibling rules.
        resolver = getattr(self.game, "_resolve_filemap_entries", None)
        if callable(resolver) and normal:
            try:
                resolved = resolver([(path, mod_name) for path in normal])
            except Exception as exc:
                self.log(f"Candidate routing warning for {mod_name}: {exc}")
                resolved = ()
            for staged_rel, _owner, destination, final_rel in resolved:
                if destination == getattr(self.game, "_PREFIX_SKIP_DEST", object()):
                    route_target = "prefix"
                    destination = ""
                else:
                    route_target = "game"
                full = self._join(destination, final_rel)
                out[staged_rel.lower()] = [_Route(
                    route_target, _wire_path(full), full, staged_rel,
                    deploy_remap=False)]

        # Generic custom routing uses the same matcher as deployment.  The
        # identity key is exact; display spelling remains the candidate's own
        # spelling until the Rust casing pass chooses a winner-dependent form.
        rules = getattr(self.game, "custom_routing_rules", None) or ()
        if rules:
            try:
                from Utils.deployment.custom_rules import (
                    compute_routed_destinations)
                custom_routes = compute_routed_destinations(
                    normal, list(rules))
            except Exception:
                custom_routes = {}
            for path in normal:
                key = path.lower()
                if key in out:
                    continue
                destinations = custom_routes.get(key)
                if destinations:
                    out[key] = [
                        _Route(
                            "prefix" if to_prefix else "game",
                            _wire_path(destination), destination, path,
                            deploy_remap=False,
                        )
                        for to_prefix, destination in destinations
                    ]

        handler_spec = self._handler_mod_deploy.get(mod_name)
        if handler_spec is not None:
            handler_root, leading_folders = handler_spec[:2]
            flatten = bool(handler_spec[3]) if len(handler_spec) > 3 else False
            handler_path = Path(handler_root).expanduser().resolve(strict=False)
            handler_target = "custom:" + str(handler_path)
            handler_prefix = ""
            try:
                game_root = self.game.get_game_path()
                if game_root is not None:
                    relative_root = handler_path.relative_to(
                        Path(game_root).expanduser().resolve(strict=False))
                    handler_target = "game"
                    handler_prefix = (
                        "" if relative_root == Path(".")
                        else relative_root.as_posix())
            except (AttributeError, OSError, ValueError):
                pass
            prefixes = {
                str(item).replace("\\", "/").strip("/").casefold()
                for item in leading_folders
                if str(item).replace("\\", "/").strip("/")
            }
            for path in normal:
                key = path.lower()
                if key in out:
                    continue
                relative = path.replace("\\", "/").strip("/")
                head, separator, tail = relative.partition("/")
                if separator and head.casefold() in prefixes:
                    relative = tail
                if flatten:
                    relative = relative.rsplit("/", 1)[-1]
                destination = self._join(handler_prefix, relative)
                out[key] = [_Route(
                    handler_target, _wire_path(destination), destination,
                    relative, deploy_remap=False)]

        # BG3's primary .pak deployment is the Larian Mods folder and flattened.
        # That folder can physically sit below the configured game root when a
        # user keeps their Proton prefix there, so retain _default_domain's
        # relative prefix instead of treating every in-root target as <root>.
        # A custom rule still wins: Data/foo.pak is a game Data pak, while an
        # otherwise-unclaimed foo.pak belongs in the Larian Mods folder.
        game_id = str(getattr(self.game, "game_id", "")).lower()
        if game_id == "baldurs_gate_3":
            mods_target, mods_prefix = self._default_domain()
            for path in normal:
                key = path.lower()
                if key.endswith(".pak") and key not in out:
                    basename = path.rsplit("/", 1)[-1]
                    destination = self._join(mods_prefix, basename)
                    out[key] = [_Route(
                        mods_target, _wire_path(destination), destination, basename,
                        deploy_remap=False)]

        for path in normal:
            key = path.lower()
            if key not in out:
                full = self._join(data_prefix, path)
                out[key] = [_Route(target, _wire_path(full), full, path)]

        # BaseGame path/extension remaps are destination-only: the staged
        # spelling remains in ``legacy_rel`` so compatibility handlers can
        # resolve the source, while conflict identity, Data, deployed-state,
        # and restore all observe the path that is actually written to disk.
        # This is important for RE Engine's x64 -> STM and TEX-version maps,
        # and Cyberpunk's archive/pc/patch -> archive/pc/mod transition.
        # Raw separator deploys explicitly bypass game routing rules.
        if mod_name not in self._raw_route_mods:
            for key, routes in tuple(out.items()):
                rewritten = []
                for route in routes:
                    if route.target != "game" or not route.deploy_remap:
                        rewritten.append(route)
                        continue
                    destination = route.destination_display
                    lower = destination.lower()
                    for old, new in (
                            getattr(self.game, "mod_deploy_path_remap", {}) or {}
                    ).items():
                        old_text = str(old).replace("\\", "/")
                        if old_text and lower.startswith(old_text.lower()):
                            destination = (
                                str(new).replace("\\", "/")
                                + destination[len(old_text):]
                            )
                            lower = destination.lower()
                            break
                    for old, new in (
                            getattr(self.game, "pak_hash_extension_remap", {}) or {}
                    ).items():
                        old_text = str(old)
                        if old_text and lower.endswith(old_text.lower()):
                            destination = destination[:-len(old_text)] + str(new)
                            break
                    rewritten.append(replace(
                        route,
                        destination_key=_wire_path(destination),
                        destination_display=destination,
                        deploy_remap=False,
                    ))
                out[key] = rewritten
        custom_root = self._per_mod_deploy.get(mod_name)
        if custom_root is not None:
            custom_target = "custom:" + str(
                Path(custom_root).expanduser().resolve(strict=False))
            for key, routes in tuple(out.items()):
                rewritten = []
                for route in routes:
                    relative = route.destination_display
                    if mod_name in self._raw_route_mods:
                        relative = next(
                            (path for path in staged if path.lower() == key),
                            route.destination_display)
                    rewritten.append(_Route(
                        custom_target, _wire_path(relative), relative,
                        route.legacy_rel, root_rule=route.root_rule,
                        deploy_remap=False))
                out[key] = rewritten
        for path in staged:
            key = path.lower()
            if key in roots:
                full = path.replace("\\", "/").strip("/")
                out[key] = [_Route(
                    "game", _wire_path(full), full, path,
                    root=True, deploy_remap=False,
                )]
        if root_rule_mod:
            out = {
                key: [replace(route, root_rule=True) for route in routes]
                for key, routes in out.items()
            }
        return out

    def _matches_root_rule(self, mod_name: str, staged: list[str]) -> bool:
        try:
            from Utils.deployment.custom_rules import (
                mods_matching_root_rules,
                root_rule_flag_candidates,
            )
            flag_rules = root_rule_flag_candidates(self.game)
            return mod_name in mods_matching_root_rules(
                {mod_name: staged}, flag_rules
            )
        except Exception as exc:
            self.log(f"Root-rule capability warning for {mod_name}: {exc}")
            return False

    def _flags(self, routed: _Route, staged: str) -> int:
        result = FLAG_ROOT_RULE if routed.root_rule else 0
        lower = staged.lower()
        remaps = {
            str(prefix).replace("\\", "/").strip("/").lower()
            for prefix in (getattr(self.game, "mod_deploy_path_remap", {}) or {})
        }
        if any(lower == prefix or lower.startswith(prefix + "/")
               for prefix in remaps):
            result |= FLAG_PRE_RTX
        filename = lower.rsplit("/", 1)[-1]
        plugin_exts = tuple(
            str(ext).lower()
            for ext in (getattr(self.game, "plugin_extensions", None) or ())
        )
        if plugin_exts and filename.endswith(plugin_exts):
            result |= FLAG_PLUGIN
        archive_exts = tuple(
            str(ext).lower()
            for ext in (getattr(self.game, "archive_extensions", None) or ())
        )
        if archive_exts and filename.endswith(archive_exts):
            result |= FLAG_ARCHIVE
        try:
            framework_names = {
                str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
                for value in (getattr(self.game, "frameworks", {}) or {}).values()
                for path in framework_exe_candidates(value)
            }
        except Exception:
            framework_names = set()
        if filename in framework_names:
            result |= FLAG_FRAMEWORK
        if any(filename.endswith(extension) for extension in _TEXT_EXTENSIONS):
            result |= FLAG_TEXT
        return result

    def _plugin_key_for_route(self, route: _Route, staged: str) -> str | None:
        plugin_exts = tuple(
            str(ext).lower()
            for ext in (getattr(self.game, "plugin_extensions", None) or ())
        )
        filename = staged.lower().rsplit("/", 1)[-1]
        if not plugin_exts or not filename.endswith(plugin_exts):
            return None
        plugin_target, plugin_prefix = self._default_domain()
        expected = self._join(plugin_prefix, filename)
        if (route.target == plugin_target
                and route.destination_key == _wire_path(expected)):
            return filename
        return None

    def _identity(self, path: Path, staged: str, all_staged: set[str]) -> list[bytes]:
        if not staged.lower().endswith(".pak"):
            return []
        try:
            if not bg3_uuid_conflicts_enabled():
                return []
            fake_index = {item.lower(): item for item in all_staged}
            if is_multipart_pak(staged.lower(), fake_index):
                return []
            from Utils.bg3.pak import extract_meta_lsx
            from Utils.bg3.modsettings import parse_meta_lsx
            xml = extract_meta_lsx(path)
            info = parse_meta_lsx(xml) if xml else None
            uuid = (getattr(info, "uuid", "") or "").strip().lower()
            return [b"pak\0" + uuid.encode("ascii")] if uuid else []
        except Exception:
            return []

    def build_manifest(
        self, mod_name: str, *, cancel=None,
        catalog_manifest: dict | None = None,
    ) -> dict:
        _check_cancel(cancel)
        if mod_name == OVERWRITE_NAME:
            root = self.overwrite
        elif mod_name == ROOT_FOLDER_NAME:
            root = self.root_folder
        else:
            root = self.staging / mod_name
        if catalog_manifest is None:
            raw_files = self._scan_root(root, cancel=cancel) if root.is_dir() else []
        else:
            raw_files = [
                RawFile(
                    relative=bytes(record["source_rel"]),
                    display=str(record["source_display"]),
                    size=int(record.get("size", 0)),
                    mtime_ns=int(record.get("mtime_ns", 0)),
                    ordinal=int(record.get("ordinal", 0)),
                )
                for record in catalog_manifest.get("raw_files", ())
            ]
        whole_mod_root = mod_name in self._root_mods
        if (mod_name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                and not whole_mod_root):
            try:
                from Nexus.nexus_meta import read_meta
                whole_mod_root = bool(read_meta(root / "meta.ini").root_folder)
            except Exception:
                pass
        fingerprint = hashlib.blake2b(digest_size=24)
        for raw in raw_files:
            fingerprint.update(len(raw.relative).to_bytes(4, "little"))
            fingerprint.update(raw.relative)
            fingerprint.update(raw.size.to_bytes(8, "little", signed=False))
            fingerprint.update(raw.mtime_ns.to_bytes(8, "little", signed=True))
        manifest_fingerprint = (
            bytes(catalog_manifest.get("manifest_fingerprint", ()))
            if catalog_manifest is not None
            else fingerprint.digest()
        )
        cached_identities: dict[bytes, list[bytes]] = {}
        if catalog_manifest is not None:
            for candidate in catalog_manifest.get("candidates", ()):
                if candidate.get("kind") == "archive_member":
                    continue
                identities = [
                    bytes(identity) for identity in candidate.get("identities", ())
                ]
                if identities:
                    cached_identities[bytes(candidate["source_rel"])] = identities

        processed: list[tuple[RawFile, str, str]] = []
        # The retired mod index applied only the game's install-extension
        # filter before the UI capability scan.  Profile exclusions and
        # conflict-ignore rules affected deployment, not whether a mod was
        # described as shipping a plugin/root-router/framework/archive.
        # Preserve that distinction in raw inventory flags.
        indexed_by_key: dict[str, tuple[RawFile, str]] = {}
        indexed_groups: dict[str, list[tuple[RawFile, str]]] = {}
        allowed_extensions = {
            str(item).lower()
            for item in (getattr(self.game, "mod_install_extensions", None) or ())
        }
        per_file_roots = self._raw_root_files.get(mod_name, set())
        for raw in raw_files:
            raw_display = raw.display.replace("\\", "/")
            staged = self._strip(mod_name, raw_display)
            if not staged:
                continue
            filename = staged.lower().rsplit("/", 1)[-1]
            if (not allowed_extensions
                    or any(filename.endswith(ext) and len(filename) > len(ext)
                           for ext in allowed_extensions)):
                key = staged.lower()
                indexed_groups.setdefault(key, []).append((raw, staged))
                existing = indexed_by_key.get(key)
                if existing is None:
                    indexed_by_key[key] = (raw, staged)
                else:
                    # The retired index collapsed physical case collisions and
                    # preferred the spelling with the greatest number of
                    # uppercase folder characters.  Keep the raw rows, but
                    # derive exactly one candidate/inventory identity from the
                    # same winner so source selection and facet counts match.
                    old_staged = existing[1]
                    old_folder = old_staged.rpartition("/")[0]
                    new_folder = staged.rpartition("/")[0]
                    if (sum(char.isupper() for char in new_folder)
                            > sum(char.isupper() for char in old_folder)):
                        indexed_by_key[key] = (raw, staged)

        indexed_staged = list(indexed_by_key.values())
        roots: set[str] = set()
        indexed_roots: set[str] = set()
        for key, members in indexed_groups.items():
            raw, staged = indexed_by_key[key]
            if (mod_name == ROOT_FOLDER_NAME
                    or whole_mod_root
                    or any(
                        member.display.replace("\\", "/").lower()
                        in per_file_roots
                        for member, _member_staged in members
                    )):
                indexed_roots.add(key)

            # Exclusions are stored against raw paths. Several wrapper/casing
            # variants can collapse to one retired-index key; that key stayed
            # active unless every physical variant was excluded. Pick a real
            # surviving source, but retain the index winner's staged spelling
            # for conflict/deployment identity.
            surviving = [
                (member, member_staged)
                for member, member_staged in members
                if member.display.replace("\\", "/").lower()
                not in self._raw_excluded.get(mod_name, ())
            ]
            if not surviving:
                continue
            source_raw, _source_staged = next(
                (
                    pair for pair in surviving
                    if pair[0].display.replace("\\", "/").lower() == key
                ),
                next(
                    (pair for pair in surviving if pair[0] is raw),
                    surviving[0],
                ),
            )
            source_lower = source_raw.display.replace("\\", "/").lower()
            if not self._accept(mod_name, source_lower, staged):
                continue
            processed.append((source_raw, source_lower, staged))
            if key in indexed_roots:
                roots.add(key)
        spelling_hook = getattr(self.game, "filegraph_manifest_spelling", None)
        if callable(spelling_hook) and processed:
            try:
                replacements = spelling_hook(
                    root,
                    [(raw.display, staged) for raw, _lower, staged in processed],
                ) or {}
            except Exception as exc:
                self.log(f"Candidate spelling warning for {mod_name}: {exc}")
                replacements = {}
            roots = {
                replacements.get(staged_lower, staged_lower).lower()
                for staged_lower in roots
            }
            processed = [
                (raw, raw_lower, replacements.get(staged.lower(), staged))
                for raw, raw_lower, staged in processed
            ]
        root_rule_mod = self._matches_root_rule(
            mod_name, [staged for _raw, staged in indexed_staged])
        capability_routes = self._routes_for_manifest(
            mod_name, [staged for _raw, staged in indexed_staged],
            indexed_roots, root_rule_mod=root_rule_mod)
        routes = self._routes_for_manifest(
            mod_name, [staged for _raw, _lower, staged in processed], roots,
            root_rule_mod=root_rule_mod)
        raw_capabilities: dict[bytes, int] = {}
        for raw, staged in indexed_staged:
            capability_variants = capability_routes[staged.lower()]
            flags = FLAG_INDEXED
            for route in capability_variants:
                flags |= self._flags(route, staged)
            if any(
                    self._plugin_key_for_route(route, staged) is not None
                    for route in capability_variants):
                flags |= FLAG_STAGED_PLUGIN
            if staged.lower() in indexed_roots:
                flags |= FLAG_INDEX_ROOT
            raw_capabilities[raw.relative] = flags
        indexed_display = {
            raw.relative: staged for raw, staged in indexed_staged
        }
        # Archive inventory was maintained by its own index and did not depend
        # on the loose-file install-extension filter.  Keep that independent
        # capability so the BSA filter/toggle guard still sees every archive
        # the archive scanner can activate.
        archive_exts = tuple(
            str(ext).lower()
            for ext in (getattr(self.game, "archive_extensions", None) or ())
        )
        if archive_exts:
            for raw in raw_files:
                filename = raw.display.replace("\\", "/").lower().rsplit(
                    "/", 1)[-1]
                if filename.endswith(archive_exts):
                    raw_capabilities[raw.relative] = (
                        raw_capabilities.get(raw.relative, 0) | FLAG_ARCHIVE)
        plugin_exts = tuple(
            str(ext).lower()
            for ext in (getattr(self.game, "plugin_extensions", None) or ())
        )
        inventory_plugin_stems = {
            staged.lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
            for _raw, staged in indexed_staged
            if plugin_exts and staged.lower().endswith(plugin_exts)
        }
        all_staged = {staged for _raw, _lower, staged in processed}
        candidates: list[dict] = []
        for raw, _raw_lower, staged in processed:
            identities = (
                cached_identities.get(raw.relative, [])
                if catalog_manifest is not None
                else self._identity(root / raw.display, staged, all_staged)
            )
            for route in routes[staged.lower()]:
                kind = (
                    "overwrite" if mod_name == OVERWRITE_NAME
                    else "root" if route.root else "loose"
                )
                plugin_key = self._plugin_key_for_route(route, staged)
                flags = self._flags(route, staged)
                if plugin_key is None:
                    flags &= ~FLAG_PLUGIN
                candidates.append({
                    "source_rel": raw.relative,
                    "source_display": raw.display,
                    "target": route.target,
                    "destination_key": route.destination_key,
                    "destination_display": route.destination_display,
                    "kind": kind,
                    "size": raw.size,
                    "mtime_ns": raw.mtime_ns,
                    "ordinal": raw.ordinal,
                    "identities": identities,
                    "archive_key": None,
                    "plugin_key": plugin_key,
                    "deployable": True,
                    "legacy_root": route.root,
                    "legacy_rel": route.legacy_rel,
                    "flags": flags,
                })

        if catalog_manifest is None:
            self._append_archive_candidates(
                mod_name, root, raw_files, inventory_plugin_stems, candidates,
                cancel=cancel)
        else:
            self._append_cached_archive_candidates(
                mod_name, raw_files, inventory_plugin_stems,
                catalog_manifest, candidates,
                cancel=cancel)
        return {
            "mod_name": mod_name,
            "mod_key": _normalise_mod_key(mod_name),
            "variant_key": self.variant_key(mod_name),
            "manifest_fingerprint": manifest_fingerprint,
            "raw_files": [
                {
                    "source_rel": raw.relative,
                    "source_display": raw.display,
                    "index_display": indexed_display.get(raw.relative, ""),
                    "size": raw.size,
                    "mtime_ns": raw.mtime_ns,
                    "ordinal": raw.ordinal,
                    "flags": raw_capabilities.get(raw.relative, 0),
                }
                for raw in raw_files
            ],
            "candidates": candidates,
        }

    def _append_archive_candidates(
        self,
        mod_name: str,
        root: Path,
        raw_files: list[RawFile],
        plugin_stems: set[str],
        candidates: list[dict],
        *,
        cancel=None,
    ) -> None:
        extensions = frozenset(
            str(ext).lower()
            for ext in (getattr(self.game, "archive_extensions", None) or ())
        )
        if not extensions or not root.is_dir():
            return
        try:
            _name, archives, _parsed = scan_mod_archives(
                mod_name, str(root), extensions, None)
        except Exception as exc:
            self.log(f"Archive scan warning for {mod_name}: {exc}")
            return
        if not archives:
            return
        raw_by_lower = {
            raw.display.replace("\\", "/").lower(): raw for raw in raw_files
        }
        target, data_prefix = self._default_domain()
        mod_key = _normalise_mod_key(mod_name)
        try:
            from Utils.unreal.archives import UE_ARCHIVE_EXTENSIONS
            name_ordering = bool(extensions & UE_ARCHIVE_EXTENSIONS)
        except Exception:
            name_ordering = False
        for archive_name, mtime, paths in archives:
            _check_cancel(cancel)
            archive_lower = archive_name.replace("\\", "/").lower()
            raw = raw_by_lower.get(archive_lower)
            if raw is None:
                # Archive scanning happens before install-extension filtering;
                # retain a stable source ordinal beyond the loose manifest.
                try:
                    stat = (root / archive_name).stat()
                    size = int(stat.st_size)
                    mtime_ns = int(stat.st_mtime_ns)
                except OSError:
                    size, mtime_ns = 0, int(float(mtime) * 1_000_000_000)
                raw = RawFile(
                    archive_name.encode("utf-8", "surrogateescape"),
                    archive_name, size, mtime_ns,
                    len(raw_files) + len(self._archive_units),
                )
            stem = archive_lower.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            owner = owning_plugin(stem, plugin_stems)
            unique_archive = mod_key + "\0" + archive_lower
            mod_rank = self._mod_low_rank.get(mod_name, 0)
            if name_ordering:
                try:
                    rank = (1, *pak_name_rank(archive_lower), mod_rank, mod_key)
                except Exception:
                    rank = (1, archive_lower, mod_rank, mod_key)
            else:
                rank = (
                    0 if owner is None else 2,
                    mod_rank if owner is None else self._plugin_rank.get(owner, mod_rank),
                    mod_rank,
                    archive_lower,
                    mod_key,
                )
            self._archive_units.append((rank, unique_archive))
            for offset, member in enumerate(paths, 1):
                member = str(member).replace("\\", "/").lstrip("/")
                full = self._join(data_prefix, member)
                candidates.append({
                    "source_rel": raw.relative,
                    "source_display": raw.display,
                    "target": target,
                    "destination_key": _wire_path(full),
                    "destination_display": full,
                    "kind": "archive_member",
                    "size": 0,
                    "mtime_ns": raw.mtime_ns,
                    "ordinal": raw.ordinal,
                    "identities": [],
                    "archive_key": unique_archive,
                    "plugin_key": owner,
                    "deployable": False,
                    "legacy_root": False,
                    "legacy_rel": member,
                    "flags": FLAG_ARCHIVE,
                })

    def _append_cached_archive_candidates(
        self,
        mod_name: str,
        raw_files: list[RawFile],
        plugin_stems: set[str],
        catalog_manifest: dict,
        candidates: list[dict],
        *,
        cancel=None,
    ) -> None:
        """Re-route parsed archive members without reopening archive files."""
        archive_records = [
            record for record in catalog_manifest.get("candidates", ())
            if record.get("kind") == "archive_member"
        ]
        if not archive_records:
            return
        raw_by_path = {raw.relative: raw for raw in raw_files}
        target, data_prefix = self._default_domain()
        extensions = frozenset(
            str(ext).lower()
            for ext in (getattr(self.game, "archive_extensions", None) or ())
        )
        try:
            from Utils.unreal.archives import UE_ARCHIVE_EXTENSIONS
            name_ordering = bool(extensions & UE_ARCHIVE_EXTENSIONS)
        except Exception:
            name_ordering = False
        mod_key = _normalise_mod_key(mod_name)
        ranked: set[str] = set()
        for offset, record in enumerate(archive_records, 1):
            _check_cancel(cancel)
            source_rel = bytes(record["source_rel"])
            raw = raw_by_path.get(source_rel)
            if raw is None:
                continue
            archive_lower = raw.display.replace("\\", "/").lower()
            archive_key = str(
                record.get("archive_key") or (mod_key + "\0" + archive_lower))
            stem = archive_lower.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            owner = owning_plugin(stem, plugin_stems)
            if archive_key not in ranked:
                ranked.add(archive_key)
                mod_rank = self._mod_low_rank.get(mod_name, 0)
                if name_ordering:
                    try:
                        rank = (
                            1, *pak_name_rank(archive_lower), mod_rank, mod_key)
                    except Exception:
                        rank = (1, archive_lower, mod_rank, mod_key)
                else:
                    rank = (
                        0 if owner is None else 2,
                        mod_rank if owner is None else self._plugin_rank.get(
                            str(owner), mod_rank),
                        mod_rank,
                        archive_lower,
                        mod_key,
                    )
                self._archive_units.append((rank, archive_key))
            member = str(record.get("legacy_rel", "")).replace(
                "\\", "/").lstrip("/")
            if not member:
                continue
            full = self._join(data_prefix, member)
            candidates.append({
                "source_rel": source_rel,
                "source_display": raw.display,
                "target": target,
                "destination_key": _wire_path(full),
                "destination_display": full,
                "kind": "archive_member",
                "size": 0,
                "mtime_ns": raw.mtime_ns,
                "ordinal": int(record.get("ordinal", raw.ordinal + offset)),
                "identities": [],
                "archive_key": archive_key,
                "plugin_key": owner,
                "deployable": False,
                "legacy_root": False,
                "legacy_rel": member,
                "flags": FLAG_ARCHIVE,
            })

    def prepare_profile_rules(self, *, refresh_rules: bool = True) -> None:
        # Toggle operations cannot change routing, exclusions, root flags, or
        # per-mod variants.  Re-reading those profile files (and statting every
        # mod's meta.ini) dominated hot toggles on large libraries.  Moves do
        # refresh because crossing a separator can change a custom route.
        if refresh_rules:
            self._refresh_profile_rules()
        try:
            from Utils.mods.modlist import read_modlist
            ordered = [
                entry.name for entry in read_modlist(self.profile_dir / "modlist.txt")
                if not entry.is_separator
            ]
            self._mod_low_rank = {
                name: index for index, name in enumerate(reversed(ordered))
            }
        except Exception:
            self._mod_low_rank = {}
        try:
            from Utils.plugins import read_loadorder
            loadorder = read_loadorder(self.profile_dir / "loadorder.txt")
            self._plugin_rank = {
                name.lower().rsplit(".", 1)[0]: index
                for index, name in enumerate(loadorder)
            }
        except Exception:
            self._plugin_rank = {}

    def load_catalog_archive_order(self, records: Iterable[tuple]) -> None:
        """Rebuild archive ranking from catalog metadata, without disk I/O."""
        self._archive_units.clear()
        extensions = frozenset(
            str(ext).lower()
            for ext in (getattr(self.game, "archive_extensions", None) or ())
        )
        try:
            from Utils.unreal.archives import UE_ARCHIVE_EXTENSIONS
            name_ordering = bool(extensions & UE_ARCHIVE_EXTENSIONS)
        except Exception:
            name_ordering = False
        for mod_name, mod_key, archive_key, source_display, owner in records:
            archive_lower = str(source_display).replace("\\", "/").lower()
            mod_rank = self._mod_low_rank.get(str(mod_name), 0)
            if name_ordering:
                try:
                    rank = (
                        1, *pak_name_rank(archive_lower), mod_rank, str(mod_key))
                except Exception:
                    rank = (1, archive_lower, mod_rank, str(mod_key))
            else:
                rank = (
                    0 if owner is None else 2,
                    mod_rank if owner is None else self._plugin_rank.get(
                        str(owner), mod_rank),
                    mod_rank,
                    archive_lower,
                    str(mod_key),
                )
            self._archive_units.append((rank, str(archive_key)))
        self._archive_order = [
            archive for _rank, archive in sorted(self._archive_units)
        ]

    def refresh_batches(
        self,
        mod_names: Iterable[str] | None = None,
        *,
        progress: Callable[[RefreshProgress], None] | None = None,
        cancel=None,
    ) -> Iterable[dict]:
        self.prepare_profile_rules()
        self._archive_units.clear()
        if mod_names is None:
            try:
                names = sorted(
                    entry.name for entry in self.staging.iterdir()
                    if entry.is_dir() and not entry.name.endswith("_separator")
                )
            except OSError:
                names = []
            names.append(OVERWRITE_NAME)
            names.append(ROOT_FOLDER_NAME)
        else:
            names = list(dict.fromkeys(mod_names))
        total = len(names)
        files_scanned = archives_scanned = 0
        for index, name in enumerate(names, 1):
            _check_cancel(cancel)
            batch = self.build_manifest(name, cancel=cancel)
            files_scanned += sum(
                1 for candidate in batch["candidates"]
                if candidate["kind"] != "archive_member")
            archives_scanned += len({
                candidate["archive_key"] for candidate in batch["candidates"]
                if candidate["archive_key"]
            })
            if progress is not None:
                progress(RefreshProgress(
                    index, total, files_scanned, archives_scanned, name))
            yield batch
        self._archive_order = [
            archive for _rank, archive in sorted(self._archive_units)
        ]

    @property
    def archive_order(self) -> list[str]:
        return list(self._archive_order)

    def build_intent(self, operation_hint: dict | None = None) -> dict:
        from Utils.mods.modlist import read_modlist
        entries = read_modlist(self.profile_dir / "modlist.txt")
        mods = [
            {
                "name": entry.name,
                "key": _normalise_mod_key(entry.name),
                "enabled": bool(entry.enabled),
                "variant_key": self.variant_key(entry.name),
            }
            for entry in entries
            if not entry.is_separator and entry.name != ROOT_FOLDER_NAME
        ]
        try:
            from Utils.profiles.state import read_disabled_plugins
            disabled = {
                _normalise_mod_key(name): sorted({
                    str(path).lower().encode("utf-8", "surrogateescape")
                    for path in paths
                })
                for name, paths in read_disabled_plugins(
                    self.profile_dir, None).items()
                if paths
            }
        except Exception:
            disabled = {}
        try:
            from Utils.plugins import read_loadorder
            plugin_order = read_loadorder(self.profile_dir / "loadorder.txt")
        except Exception:
            plugin_order = []
        intent_sources = [
            self.profile_dir / "modlist.txt",
            self.profile_dir / "profile_state.json",
            self.profile_dir / "plugins.txt",
            self.profile_dir / "loadorder.txt",
        ]
        digest = hashlib.blake2b(digest_size=24)
        for path in intent_sources:
            digest.update(path.name.encode())
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"\0")
        return {
            "profile_id": str(self.profile_dir.resolve(strict=False)),
            "intent_hash": digest.digest(),
            "rules_hash": self.rules_hash(),
            "mods": mods,
            # The volatile providers are active outside the visible modlist,
            # but still have rule-derived candidate variants.  Carry their
            # selected keys explicitly so Rust can restore only this profile's
            # variants instead of loading every cached profile variant.
            "special_variants": {
                OVERWRITE_NAME.lower(): self.variant_key(OVERWRITE_NAME),
                ROOT_FOLDER_NAME.lower(): self.variant_key(ROOT_FOLDER_NAME),
            },
            "archive_order": self.archive_order,
            "plugin_order": [
                str(name).lower().rsplit(".", 1)[0]
                for name in plugin_order
            ],
            "plugin_extensions": [
                str(ext).lower()
                for ext in (getattr(self.game, "plugin_extensions", None) or ())
            ],
            "disabled_plugin_paths": disabled,
            "loose_beats_archive": not bool(
                set(getattr(self.game, "archive_extensions", ()) or ())
                & {".pak", ".utoc", ".ucas"}
            ),
            "normalize_folder_case": self._normalize_folder_case,
            "casing_strategy": str(
                getattr(self.game, "filemap_casing", "upper") or "upper"),
            "casing_pins": {
                str(segment).lower(): str(spelling)
                for segment, spelling in (
                    getattr(self.game, "filemap_casing_pins", None) or {}
                ).items()
            },
            "hint": operation_hint or {},
        }
