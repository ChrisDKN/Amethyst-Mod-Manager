"""
ue5_game.py
Abstract base class for Unreal Engine 5 games.

UE5 games ship mods as files destined for multiple locations inside the game
root (pak files → Content/Paks/, esp/esm plugins → Content/Dev/ObvData/Data/,
UE4SS lua mods → Binaries/Win64/ue4ss/Mods/, etc.).

This base class handles the multi-target deploy/restore pattern.  Subclasses
declare their routing rules via ``ue5_routing_rules`` and fill in the usual
identity/path properties.

Routing rules
-------------
Each rule is a dict with at least:
  ``dest``  — path relative to game root where matching files are deployed
              (e.g. ``"Content/Paks"``)

Match criteria (one or more):
  ``extensions``  — list of lowercase dotted extensions, e.g. ``[".pak", ".utoc"]``
  ``folder``      — top-level folder name inside the mod (case-insensitive),
                    e.g. ``"ue4ss"`` — matches when the first path segment of
                    the staged file equals this string
  ``strip``       — optional list of leading path segments to strip from the
                    staged relative path before writing to ``dest``
                    (e.g. strip ``["Content/Paks", "Paks"]`` so a staged file at
                    ``Content/Paks/MyMod.pak`` deploys as ``Content/Paks/MyMod.pak``
                    rather than ``Content/Paks/Content/Paks/MyMod.pak``)

Rules are evaluated in order; the first match wins.  Files that match no rule
are deployed to ``ue5_default_dest`` (defaults to the game root itself).

Deploy workflow
---------------
Unlike traditional games, UE5 mod destinations are not folders full of vanilla
files — they are either empty or contain unrelated game content that must not
be touched.  Deploy therefore works without a Core backup:

  1. Place each mod file directly into its resolved game destination.
  2. Track every placed file path in a deployed.txt manifest.

Restore:
  1. Read deployed.txt and delete every listed file.
  2. Remove any directories that became empty.
  3. Delete deployed.txt.
"""

from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from Games.base_game import BaseGame
from Utils.deploy import LinkMode, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, expand_separator_raw_deploy, expand_separator_link_modes, _resolve_nocase, _write_deploy_snapshot, _move_runtime_files, _FILEMAP_SNAPSHOT_NAME
from Utils.deploy_custom_rules import deploy_custom_rules, restore_custom_rules, compute_prefix_handled
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Manifest written next to filemap.txt so restore knows exactly what to remove
_DEPLOYED_MANIFEST = "ue5_deployed.txt"

# Vanilla files displaced by mod files are backed up here (inside the game root)
_VANILLA_BACKUP_DIR = "Amethyst_vanilla_files"

# Custom-dir vanilla files displaced by mod files are backed up here (inside profile root).
# Files are stored with their full absolute path mirrored so restore can reconstruct them.
_CUSTOM_VANILLA_BACKUP_DIR = "ue5_custom_vanilla_backup"


# ---------------------------------------------------------------------------
# Routing rule dataclass
# ---------------------------------------------------------------------------

@dataclass
class UE5Rule:
    """A single file-routing rule for a UE5 game.

    Attributes:
        dest:       Game-root-relative destination directory.
        extensions: Match files with these lowercase extensions (e.g. ".pak").
        folder:     Match files whose first staged path segment equals this
                    value (case-insensitive), e.g. "ue4ss".
        folder_anywhere:
                    Match files where this folder name appears as any path
                    segment (case-insensitive), not just the first one. The
                    prefix above the matched segment is stripped automatically.
                    Used by user-defined custom rules so a folder rule like
                    "folder2" matches "Binaries/Win64/folder1/folder2/file"
                    and lands at "<dest>/folder2/file".
        prefix:     Match files whose staged path starts with this multi-segment
                    prefix (case-insensitive), e.g. "Binaries/Win64/ue4ss".
                    More specific than ``folder`` — checked first.
        filenames:  Match files whose basename (case-insensitive) is in this
                    list, e.g. ["enabled.txt"].  Checked after prefix/folder.
        strip:      Path prefixes to strip from the staged relative path
                    before placing the file inside ``dest``.
                    Checked longest-first so more-specific prefixes win.
    flatten:    When True, reduce the final path to just the filename,
                    discarding all directory components.  Useful for files
                    that must land flat in ``dest`` regardless of how they
                    are packaged inside the mod folder (e.g. .bk2 movies).
    loose_only: When True, the rule only matches files that are not inside
                    any folder (i.e. files with no directory components in
                    their relative path).  Default False.
    include_siblings:
                    When True, a single match drags the matched file's
                    *containing folder* along: every file under that folder
                    (in the same mod) is routed to ``dest`` too, preserving
                    its path relative to the containing folder. The matched
                    file lands at ``dest/<container_name>/<filename>``;
                    flatten is ignored for these matches. Resolution
                    happens in ``_resolve_filemap_entries`` since per-entry
                    resolution can't see siblings.
    """
    dest: str
    extensions: list[str] = field(default_factory=list)
    folder: str = ""
    folder_anywhere: str = ""
    prefix: str = ""
    filenames: list[str] = field(default_factory=list)
    strip: list[str] = field(default_factory=list)
    flatten: bool = False
    loose_only: bool = False
    include_siblings: bool = False


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class UE5Game(BaseGame):
    """Abstract base for Unreal Engine 5 games with multi-target mod routing."""

    _PREFIX_SKIP_DEST = "__prefix_skip__"

    def __init__(self) -> None:
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self.load_paths()

    # -----------------------------------------------------------------------
    # Routing rules
    # -----------------------------------------------------------------------
    # The default ``ue5_routing_rules`` composition is:
    #
    #     [
    #         *_ue5_shared_pre_rules,            # LogicMods, pak, UE4SS normalisation
    #         *self._ue5_pre_passthrough_rules,  # game-specific specific-folder rules
    #         UE5Rule(dest="", folder="binaries"),
    #         UE5Rule(dest="", folder="content"),
    #         *self._ue5_post_passthrough_rules, # game-specific generic-extension rules
    #     ]
    #
    # Subclasses normally only need to override the two hook properties below.
    # Override ``ue5_routing_rules`` directly for fully custom orderings.

    @property
    def _ue5_shared_pre_rules(self) -> list[UE5Rule]:
        """Routing rules common to every UE5 game: LogicMods folder placement,
        .pak/.utoc/.ucas → ``Content/Paks/~mods``, and UE4SS folder
        normalisation.  Evaluated before the generic ``binaries``/``content``
        pass-through pair."""
        return [
            # LogicMods folder → Content/Paks/LogicMods/ (preserved as a folder
            # under Paks). Must come before the .pak extension rule so files
            # inside LogicMods don't get routed to ~mods/.
            UE5Rule(dest="Content/Paks", prefix="Content/Paks/LogicMods",
                    strip=["Content/Paks"], flatten=True),
            UE5Rule(dest="Content/Paks", prefix="Paks/LogicMods",
                    strip=["Paks"], flatten=True),
            UE5Rule(dest="Content/Paks", folder="LogicMods", flatten=True),
            # Pak / streaming files → Content/Paks/~mods/
            UE5Rule(
                dest="Content/Paks/~mods",
                extensions=[".pak", ".utoc", ".ucas"],
                strip=["Content/Paks/~mods", "Content/Paks/~Mods", "Content/Paks", "Paks", "Content", "~mods", "~Mods"],
                flatten=True,
            ),
            # Files already inside Content/Paks/~Mods (any casing) → normalise
            # to lowercase ~mods dest so only one folder is created on disk.
            UE5Rule(
                dest="Content/Paks/~mods",
                prefix="Content/Paks/~Mods",
                strip=["Content/Paks/~Mods", "Content/Paks/~mods"],
                flatten=True,
            ),
            # Mods shipping Binaries/Win64/UE4SS/… → normalise to lowercase
            # ue4ss dest so only one folder is ever created on disk.
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                prefix="Binaries/Win64/UE4SS",
                strip=["Binaries/Win64/UE4SS", "Binaries/Win64/ue4ss"],
                flatten=True,
            ),
            # ue4ss/ or UE4SS/ top-level folder → Binaries/Win64/ue4ss/
            UE5Rule(
                dest="Binaries/Win64/ue4ss",
                folder="ue4ss",
                strip=["ue4ss", "UE4SS"],
                flatten=True,
            ),
        ]

    @property
    def _ue5_pre_passthrough_rules(self) -> list[UE5Rule]:
        """Game-specific rules inserted between the shared pre-rules and the
        ``binaries``/``content`` pass-through.  Use this for rules that target
        a *specific* sub-folder (e.g. ``Content/Movies/Modern``) and must beat
        the generic ``folder="content"`` catch-all."""
        return []

    @property
    def _ue5_post_passthrough_rules(self) -> list[UE5Rule]:
        """Game-specific rules appended after the ``binaries``/``content``
        pass-through.  Use this for the per-game UE4SS Mods rule, Bink/.asi
        plugins, and the trailing loose-runtime ``[".dll", ".pdb"]`` rule."""
        return []

    @property
    def ue5_routing_rules(self) -> list[UE5Rule]:
        """Ordered list of routing rules.  First match wins.  Defaults to
        ``shared_pre + pre_passthrough + binaries/content + post_passthrough``;
        override directly only if you need a fundamentally different layout
        (e.g. ``Ue5CustomGame``, which prepends user-defined custom rules)."""
        return [
            *self._ue5_shared_pre_rules,
            *self._ue5_pre_passthrough_rules,
            UE5Rule(dest="", folder="binaries"),
            UE5Rule(dest="", folder="content"),
            *self._ue5_post_passthrough_rules,
        ]

    @property
    def ue5_default_dest(self) -> str:
        """Destination for files that match no rule.  Defaults to game root."""
        return ""

    # -----------------------------------------------------------------------
    # Archive (pak) conflict detection
    # -----------------------------------------------------------------------

    @property
    def archive_extensions(self) -> frozenset[str]:
        """Scan UE .pak and IoStore .utoc TOCs so mods that ship the same
        asset paths inside different archives get archive-conflict flags
        (Utils.ue_pak_reader).  Companion .ucas files hold only bulk data
        (no names) and are skipped."""
        return frozenset({".pak", ".utoc"})

    @property
    def archive_plugin_ordering(self) -> bool:
        """UE pak mounting is not plugin-driven — archive conflict winners
        follow mod priority, not any plugin load order."""
        return False

    def reshade_install_subdir(self, game_path: "Path") -> "Path | None":
        """ReShade must sit next to the real rendering binary, which in Unreal
        Engine games lives under ``<Project>/Binaries/Win64/`` rather than the
        bootstrap launcher at the game root (the official ReShade installer
        follows the launcher's embedded ``IDI_EXEC_FILE`` resource to find it).

        Locate the ``Binaries/Win64`` folder that holds the shipping exe and
        return it relative to *game_path*.  Falls back to the base behaviour
        (parent of :attr:`exe_name`) if the layout can't be found on disk.
        """
        if game_path is not None and Path(game_path).is_dir():
            root = Path(game_path)
            candidates: list[Path] = []
            # get_game_path() already resolves to the UE project root, so
            # Binaries/Win64 usually sits directly under it; allow a couple of
            # nesting levels in case a handler points at the install root.
            for pattern in ("Binaries/Win64", "*/Binaries/Win64", "*/*/Binaries/Win64"):
                for win64 in root.glob(pattern):
                    if win64.is_dir():
                        candidates.append(win64)
                if candidates:
                    break

            def _has_shipping_exe(d: Path) -> bool:
                try:
                    return any(p.name.lower().endswith("-shipping.exe") for p in d.iterdir())
                except OSError:
                    return False

            # Prefer a Win64 folder that actually contains a *-Shipping.exe.
            best = next((c for c in candidates if _has_shipping_exe(c)), None)
            if best is None and candidates:
                best = min(candidates, key=lambda p: len(p.parts))
            if best is not None:
                try:
                    return best.relative_to(root)
                except ValueError:
                    return best

        # Disk probe failed (game not installed yet, etc.). UE projects always
        # render from Binaries/Win64 relative to the project root, so default
        # there rather than the bootstrap launcher's folder.
        return Path("Binaries/Win64")

    # -----------------------------------------------------------------------
    # Paths (concrete default — subclasses may override)
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        return self._game_path

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def set_staging_path(self, path: Path | str | None) -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Routing helpers
    # -----------------------------------------------------------------------

    def _match_rule(
        self, rel_str: str,
    ) -> tuple[UE5Rule, list[str], bool] | None:
        """Return (rule, dynamic_strip, is_folder_match) for the first match,
        or None.

        ``dynamic_strip`` defaults to ``rule.strip`` but is overridden for
        ``folder_anywhere`` matches to strip the prefix above the matched
        segment so the matched folder is preserved under ``dest``.

        ``is_folder_match`` is True when the match came from folder/prefix/
        folder_anywhere (an "anchored" match where the matched folder should
        be preserved under dest when flatten=True), False for ext/filename-
        only matches (where flatten=True means bare filename).

        Extension matching uses filename-suffix logic so multi-dot extensions
        like ".dekcns.json" can be configured (Path.suffix returns only the
        last suffix). Within a rule, the longest extension is matched first.
        """
        norm = rel_str.replace("\\", "/")
        parts = norm.split("/")
        first_seg = parts[0].lower() if parts else ""
        basename = parts[-1].lower() if parts else ""
        is_loose = len(parts) == 1
        lower_segs = [p.lower() for p in parts]

        def _ext_hit(exts: list[str]) -> bool:
            # Longest-first so ".dekcns.json" wins over ".json" within the
            # rule's own list. Comparison is case-insensitive (basename is
            # already lowercased; rule.extensions is normalised on entry).
            for e in sorted(exts, key=len, reverse=True):
                el = e.lower()
                if basename.endswith(el) and len(basename) > len(el):
                    return True
            return False

        def _name_hit(names: list[str]) -> bool:
            # Filenames support glob patterns (``*``, ``?``, ``[seq]``) so
            # rules can target e.g. ``*.dekcns.json``. Plain names still match
            # by exact case-insensitive equality.
            for n in names:
                nl = n.lower()
                if any(c in nl for c in "*?["):
                    if fnmatch.fnmatchcase(basename, nl):
                        return True
                elif basename == nl:
                    return True
            return False

        for rule in self.ue5_routing_rules:
            # loose_only on prefix/folder/folder_anywhere: matched folder
            # must be at the top level (handled inline below).
            # loose_only on ext/filename-only: file itself must be loose
            # (handled by the late check before ext/filename branches).
            if rule.prefix and norm.lower().startswith(rule.prefix.lower() + "/"):
                # If the rule also has an extension filter, only match when
                # the file's extension is in the list.
                if rule.extensions and not _ext_hit(rule.extensions):
                    continue
                # loose_only: prefix must start at index 0 of the path
                # (always true since startswith already requires that), but
                # also require no segments above the prefix's last segment —
                # i.e. the prefix is anchored at the root.
                if rule.loose_only:
                    # startswith already anchors at root, so this is True.
                    pass
                return rule, rule.strip, True
            if rule.folder and first_seg == rule.folder.lower():
                if rule.extensions and not _ext_hit(rule.extensions):
                    continue
                # rule.folder always matches at the first segment, so
                # loose_only is automatically satisfied here.
                return rule, rule.strip, True
            if rule.folder_anywhere:
                target = rule.folder_anywhere.lower()
                # Search any directory segment (not the basename).
                hit_idx = -1
                for i, seg in enumerate(lower_segs[:-1]):
                    if seg == target:
                        hit_idx = i
                        break
                if hit_idx >= 0:
                    if rule.extensions and not _ext_hit(rule.extensions):
                        continue
                    # loose_only on folder_anywhere: matched folder must
                    # itself be at the top level.
                    if rule.loose_only and hit_idx != 0:
                        continue
                    if hit_idx == 0:
                        # Folder is already at root — no dynamic strip.
                        return rule, rule.strip, True
                    # Strip the prefix above the matched folder so the
                    # folder + contents land under dest.
                    dyn_prefix = "/".join(parts[:hit_idx])
                    return rule, [dyn_prefix, *rule.strip], True
            # For ext/filename-only rules, loose_only means the file itself
            # has no directory components.
            if rule.loose_only and not is_loose:
                continue
            if rule.filenames and _name_hit(rule.filenames):
                return rule, rule.strip, False
            if rule.extensions and _ext_hit(rule.extensions):
                return rule, rule.strip, False
        return None

    def _match_single_ue5_rule(
        self, rel_str: str, rule: "UE5Rule",
    ) -> tuple[list[str], bool] | None:
        """Run the same matching logic as ``_match_rule`` for a single rule.

        Returns ``(dyn_strip, is_folder_match)`` on a hit, else None. Used by
        ``_resolve_filemap_entries`` so include_siblings drags from earlier
        rules can claim files before later rules' primary matches run.
        """
        norm = rel_str.replace("\\", "/")
        parts = norm.split("/")
        first_seg = parts[0].lower() if parts else ""
        basename = parts[-1].lower() if parts else ""
        is_loose = len(parts) == 1
        lower_segs = [p.lower() for p in parts]

        def _ext_hit(exts):
            for e in sorted(exts, key=len, reverse=True):
                el = e.lower()
                if basename.endswith(el) and len(basename) > len(el):
                    return True
            return False

        def _name_hit(names):
            for n in names:
                nl = n.lower()
                if any(c in nl for c in "*?["):
                    if fnmatch.fnmatchcase(basename, nl):
                        return True
                elif basename == nl:
                    return True
            return False

        if rule.prefix and norm.lower().startswith(rule.prefix.lower() + "/"):
            if rule.extensions and not _ext_hit(rule.extensions):
                return None
            return rule.strip, True
        if rule.folder and first_seg == rule.folder.lower():
            if rule.extensions and not _ext_hit(rule.extensions):
                return None
            return rule.strip, True
        if rule.folder_anywhere:
            target = rule.folder_anywhere.lower()
            hit_idx = -1
            for i, seg in enumerate(lower_segs[:-1]):
                if seg == target:
                    hit_idx = i
                    break
            if hit_idx >= 0:
                if rule.extensions and not _ext_hit(rule.extensions):
                    return None
                if rule.loose_only and hit_idx != 0:
                    return None
                if hit_idx == 0:
                    return rule.strip, True
                dyn_prefix = "/".join(parts[:hit_idx])
                return [dyn_prefix, *rule.strip], True
        if rule.loose_only and not is_loose:
            return None
        if rule.filenames and _name_hit(rule.filenames):
            return rule.strip, False
        if rule.extensions and _ext_hit(rule.extensions):
            return rule.strip, False
        return None

    def _sibling_container(
        self, norm_rel: str, dyn_strip: list[str], is_folder_match: bool,
        mod_name: str,
    ) -> tuple[str, str] | None:
        """Return ``(container_path, container_name)`` for a matched entry.

        Container = topmost folder containing the matched file. See
        ``deploy_custom_rules._sibling_container`` for the rationale.
        """
        del dyn_strip, is_folder_match, mod_name  # unused
        if "/" not in norm_rel:
            return None
        container = norm_rel.split("/", 1)[0]
        return (container, container)

    def _apply_strip(self, rel_str: str, strips: list[str]) -> str:
        """Strip the longest matching prefix from rel_str (case-insensitive)."""
        norm = rel_str.replace("\\", "/")
        for prefix in sorted(strips, key=len, reverse=True):
            p = prefix.strip("/").lower()
            if norm.lower().startswith(p + "/"):
                return norm[len(p) + 1:]
        return norm

    def _resolve_entry(self, rel_str: str) -> tuple[str, str]:
        """Return (dest_rel, final_rel) for a filemap entry.

        dest_rel  — game-root-relative destination directory (may be "")
        final_rel — file path relative to dest_rel

        Placement under ``dest`` depends on rule.flatten:
        - flatten=False (default) — preserve the full mod-relative path under
          dest (no strip applied)
        - flatten=True + folder/prefix/folder_anywhere match — apply the
          rule's strip so the matched folder + contents land under dest.
          If the matched folder is already at the root (no parent to strip),
          the path is preserved as-is so the folder name itself is kept.
        - flatten=True + ext/filename-only match — bare filename under dest

        ``include_siblings`` only affects the matched file itself here (it
        lands at ``dest/<container_name>/<filename>``); resolving the
        sibling drag requires the full filemap and is handled by
        ``_resolve_filemap_entries``.
        """
        match = self._match_rule(rel_str)
        if match is not None:
            rule, dyn_strip, is_folder_match = match
            dest = rule.dest
            norm = rel_str.replace("\\", "/")
            # Per-entry resolution can't drag siblings (no view of other
            # entries), but it can still place the matched file inside its
            # container so single-file queries show the right destination.
            # Whole-mod drag (container_path == "") needs mod_name to form a
            # useful destination; without it we fall back to flatten/preserve.
            info = self._sibling_container(norm, dyn_strip, is_folder_match, "") \
                if rule.include_siblings else None
            container_path = info[0] if info is not None else None
            if info is not None and container_path:
                container_name = info[1]
                final_rel = container_name + "/" + norm[len(container_path) + 1:]
            elif rule.flatten:
                if is_folder_match:
                    # Folder/prefix/folder_anywhere match: strip parents above
                    # the matched folder, keep matched folder + contents.
                    # Empty strip means "no parent to strip" (folder is
                    # already at root) — preserve as-is.
                    final_rel = self._apply_strip(rel_str, dyn_strip) if dyn_strip else norm
                else:
                    # Ext/filename-only: bare filename.
                    final_rel = Path(norm).name
            else:
                # Preserve the full mod-relative path under dest.
                final_rel = norm
        else:
            dest = self.ue5_default_dest
            final_rel = rel_str.replace("\\", "/")
        return dest, final_rel

    def _prefix_routing_rules(self) -> list:
        """Subset of ``custom_routing_rules`` that target the Wine/Proton prefix.

        These are handled by ``deploy_custom_rules`` (with ``prefix_root`` set)
        before the UE5 manifest deploy runs, and are skipped by the manifest
        pipeline via the ``_PREFIX_SKIP_DEST`` sentinel.
        """
        return [r for r in self.custom_routing_rules
                if getattr(r, "to_prefix", False)]

    def _resolve_filemap_entries(
        self, entries: list[tuple[str, str]],
    ) -> list[tuple[str, str, str, str]]:
        """Resolve a whole filemap at once, applying include_siblings drag.

        Returns ``[(staged_rel, mod_name, dest_rel, final_rel), ...]``.

        Per-entry resolution can't see other files; ``include_siblings``
        needs to know that file X under the matched file's containing folder
        in the same mod should ride along. This pass first runs
        ``_resolve_entry`` on every entry to find primaries, then for each
        ``include_siblings`` primary it overrides the resolution of every
        same-mod file under the same containing folder so they all land at
        ``dest/<container_name>/<rel-from-container>``.

        Entries claimed by ``custom_routing_rules`` with ``to_prefix=True`` are
        held out of UE5 rule resolution and re-appended with ``_PREFIX_SKIP_DEST``
        so the deploy loop and data tab know to leave them alone (they're placed
        separately by ``deploy_custom_rules`` with ``prefix_root`` set).
        """
        prefix_rules = self._prefix_routing_rules()
        prefix_handled: set[str] = set()
        if prefix_rules:
            prefix_handled, _ = compute_prefix_handled(
                entries, self.custom_routing_rules,
            )
            core_entries = [
                (sr, mn) for sr, mn in entries
                if sr.replace("\\", "/").lower() not in prefix_handled
            ]
        else:
            core_entries = entries

        # Default placement (no rule matches): default_dest + full path.
        default_dest = self.ue5_default_dest
        per_entry: list[tuple[str, str, str, str]] = [
            (sr, mn, default_dest, sr.replace("\\", "/"))
            for sr, mn in core_entries
        ]
        claimed: set[int] = set()

        # Process rules in declaration order. For each rule:
        #   1. Claim every still-unclaimed entry that this rule matches as
        #      a primary, placing it under rule.dest.
        #   2. If include_siblings, drag the container of every just-claimed
        #      primary so subsequent rules can't claim files inside it.
        # This ordering enforces "rule order wins" — an earlier rule's drag
        # pre-empts a later rule's primary match on the same files.
        for rule in self.ue5_routing_rules:
            new_primaries: list[tuple[int, list[str], bool]] = []
            for i, (staged_rel, mod_name) in enumerate(core_entries):
                if i in claimed:
                    continue
                hit = self._match_single_ue5_rule(staged_rel, rule)
                if hit is None:
                    continue
                dyn_strip, is_folder_match = hit
                norm = staged_rel.replace("\\", "/")
                # Compute placement for this primary.
                if rule.include_siblings:
                    info = self._sibling_container(norm, dyn_strip, is_folder_match, mod_name)
                    if info is not None:
                        cont, cname = info
                        if cont:
                            tail = norm[len(cont) + 1:]
                        else:
                            tail = norm
                        final_rel = (cname + "/" + tail) if cname else tail
                        per_entry[i] = (staged_rel, mod_name, rule.dest, final_rel)
                        claimed.add(i)
                        new_primaries.append((i, dyn_strip, is_folder_match))
                        continue
                # Non-include_siblings: standard flatten / preserve placement.
                if rule.flatten:
                    if is_folder_match:
                        final_rel = self._apply_strip(staged_rel, dyn_strip) if dyn_strip else norm
                    else:
                        final_rel = Path(norm).name
                else:
                    final_rel = norm
                per_entry[i] = (staged_rel, mod_name, rule.dest, final_rel)
                claimed.add(i)
            # Drag siblings for include_siblings primaries.
            if not rule.include_siblings or not new_primaries:
                continue
            drags: list[tuple[str, str, str, bool]] = []
            for pidx, dyn_strip, is_folder_match in new_primaries:
                staged_rel, mod_name = core_entries[pidx]
                norm = staged_rel.replace("\\", "/")
                info = self._sibling_container(norm, dyn_strip, is_folder_match, mod_name)
                if info is None:
                    continue
                cont, cname = info
                drags.append((cont.lower(), cname, mod_name, cont == ""))
            drags.sort(key=lambda t: (0 if t[3] else 1, -len(t[0])))
            seen_drags: set[tuple[str, str]] = set()
            for cont_lower, cname, primary_mod, is_whole in drags:
                key = (cont_lower, primary_mod)
                if key in seen_drags:
                    continue
                seen_drags.add(key)
                prefix_lower = cont_lower + "/" if cont_lower else ""
                for i, (staged_rel, mod_name) in enumerate(core_entries):
                    if i in claimed:
                        continue
                    if mod_name != primary_mod:
                        continue
                    norm = staged_rel.replace("\\", "/")
                    norm_lower = norm.lower()
                    if is_whole:
                        rel_in_container = norm
                    else:
                        if norm_lower == cont_lower:
                            continue
                        if not norm_lower.startswith(prefix_lower):
                            continue
                        rel_in_container = norm[len(cont_lower) + 1:]
                    final_rel = (cname + "/" + rel_in_container) if cname else rel_in_container
                    per_entry[i] = (staged_rel, mod_name, rule.dest, final_rel)
                    claimed.add(i)

        if prefix_handled:
            for sr, mn in entries:
                if sr.replace("\\", "/").lower() in prefix_handled:
                    per_entry.append(
                        (sr, mn, self._PREFIX_SKIP_DEST, sr.replace("\\", "/"))
                    )

        return per_entry

    # -----------------------------------------------------------------------
    # UE4SS mods.txt management
    # -----------------------------------------------------------------------

    def _resolve_ue4ss_mods_dest(self) -> str | None:
        """Return the game-root-relative dir where UE4SS lua mods land.

        Detected by walking ``ue5_routing_rules`` for a rule whose ``dest``
        ends in ``Mods`` (case-insensitive) and whose ``extensions`` include
        ``.lua``.  Returns the dest string (e.g. ``"Binaries/Win64/Mods"``
        or ``"Binaries/Win64/ue4ss/Mods"``) or ``None`` if no UE4SS lua
        rule is configured.
        """
        for rule in self.ue5_routing_rules:
            if not rule.dest:
                continue
            dest_norm = rule.dest.replace("\\", "/").rstrip("/")
            if not dest_norm.lower().endswith("/mods") and dest_norm.lower() != "mods":
                continue
            exts_lower = {e.lower() for e in rule.extensions}
            if ".lua" in exts_lower:
                return dest_norm
        return None

    @staticmethod
    def _parse_mods_txt_line(line: str) -> tuple[str | None, bool | None]:
        """Parse a ``<folder> : <0|1>`` line.  Returns (folder, enabled) or
        (None, None) for blank/comment/unrecognised lines."""
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            return None, None
        if ":" not in stripped:
            return None, None
        name_part, _, val_part = stripped.partition(":")
        name = name_part.strip()
        val = val_part.strip()
        if not name or val not in ("0", "1"):
            return None, None
        return name, val == "1"

    def _collect_ue4ss_disabled_consensus(
        self, staging: Path, mod_names: list[str],
        index: dict[str, tuple[dict[str, str], dict[str, str]]] | None = None,
    ) -> set[str]:
        """Scan staged mod folders for ``mods.txt`` files and return the set
        of folder names that should default to ``: 0``.

        A folder defaults to disabled iff it appears in at least one source
        ``mods.txt`` AND every source that mentions it sets it to ``0``.
        Any ``: 1`` mention flips it to enabled. Folders not mentioned in
        any source default to enabled.

        ``index`` is the modindex.bin contents (mod_name → (normal_files,
        root_files), each a {rel_key: rel_str} dict). When provided, we use
        it to locate ``Mods/mods.txt`` files without walking disk; only the
        handful of matching files are actually opened. Falls back to
        ``rglob`` if the index isn't available.

        Returns a set of lowercased folder names.
        """
        # Per-folder counts: lowered_folder_name -> [mentions, zero_mentions]
        counts: dict[str, list[int]] = {}

        def _ingest(text: str) -> None:
            for line in text.splitlines():
                folder, enabled = self._parse_mods_txt_line(line)
                if folder is None:
                    continue
                slot = counts.setdefault(folder.lower(), [0, 0])
                slot[0] += 1
                if not enabled:
                    slot[1] += 1

        used_index = False
        if index is not None:
            used_index = True
            for mod_name in mod_names:
                entry = index.get(mod_name)
                if entry is None:
                    continue
                normal_files, root_files = entry
                # rel_key is lowercased — match endings like "/mods/mods.txt"
                # or exactly "mods/mods.txt" (no leading slash).
                for rel_key, rel_str in normal_files.items():
                    if not (rel_key.endswith("/mods/mods.txt") or rel_key == "mods/mods.txt"):
                        continue
                    src = staging / mod_name / rel_str
                    try:
                        _ingest(src.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue

        if not used_index:
            # Fallback: rglob each mod root. Slower; only used when the
            # modindex.bin isn't available.
            for mod_name in mod_names:
                mod_root = staging / mod_name
                if not mod_root.is_dir():
                    continue
                try:
                    for p in mod_root.rglob("mods.txt"):
                        if not p.is_file():
                            continue
                        if p.parent.name.lower() != "mods":
                            continue
                        try:
                            _ingest(p.read_text(encoding="utf-8", errors="replace"))
                        except OSError:
                            continue
                except OSError:
                    continue

        return {k for k, (mentions, zeros) in counts.items()
                if mentions > 0 and mentions == zeros}

    def _update_ue4ss_mods_txt(
        self,
        deployed_folder_names: set[str],
        disabled_folders: set[str] | None = None,
        log_fn=None,
    ) -> None:
        """Sync ``mods.txt`` to reflect the currently deployed UE4SS lua mods.

        ``disabled_folders`` (case-insensitive lowercased names) — fresh
        entries written for folders in this set get ``: 0`` instead of
        ``: 1``. Existing lines in the file are preserved as-is, so the
        user's manual edits survive.
        """
        _log = log_fn or (lambda _: None)

        dest_rel = self._resolve_ue4ss_mods_dest()
        if dest_rel is None:
            return
        if self._game_path is None:
            return
        game_path = self.get_game_path()
        if game_path is None:
            return

        mods_file = game_path / dest_rel / "mods.txt"

        existing_lines: list[str] = []
        if mods_file.is_file():
            try:
                raw = mods_file.read_text(encoding="utf-8", errors="replace")
                existing_lines = raw.splitlines()
            except OSError as exc:
                _log(f"  WARN: could not read {mods_file}: {exc}")
                return

        deployed_lower = {n.lower() for n in deployed_folder_names}
        disabled_lower = {n.lower() for n in (disabled_folders or set())}

        def _entry_for(name: str) -> str:
            return f"{name} : {'0' if name.lower() in disabled_lower else '1'}"

        out_lines: list[str] = []
        seen_lower: set[str] = set()

        # Track Keybinds separately — UE4SS requires it loaded last (the
        # shipped file has a "; Built-in keybinds, do not move up!" comment
        # above it). We always force it to the bottom regardless of where
        # it appeared in the source file.
        keybinds_line: str | None = None
        keybinds_deployed = "keybinds" in deployed_lower

        for line in existing_lines:
            folder, _enabled = self._parse_mods_txt_line(line)
            if folder is None:
                # Comment / blank / unrecognised — keep as-is
                out_lines.append(line)
                continue
            f_lower = folder.lower()
            if f_lower in seen_lower:
                # Duplicate of an entry we've already emitted — drop
                continue
            if f_lower == "keybinds":
                # Defer Keybinds to end-of-file
                if keybinds_deployed and keybinds_line is None:
                    keybinds_line = line
                seen_lower.add(f_lower)
                continue
            if f_lower in deployed_lower:
                # Managed entry, still deployed — keep, mark as seen
                out_lines.append(line)
                seen_lower.add(f_lower)
            # Else: managed entry whose mod is no longer deployed — drop

        # Strip any trailing blank lines / comments left after Keybinds was
        # pulled out (so we don't double the trailing blank when re-appending).
        while out_lines and (
            not out_lines[-1].strip()
            or out_lines[-1].lstrip().startswith(";")
            or out_lines[-1].lstrip().startswith("#")
        ):
            # Only strip trailing blanks/comments if Keybinds is going to be
            # appended; otherwise leave them alone.
            if not keybinds_deployed:
                break
            out_lines.pop()

        # Append regular new entries (everything except Keybinds). Default
        # state honours disabled_folders (consensus from source mods.txt).
        new_names = sorted(
            n for n in deployed_folder_names
            if n.lower() not in seen_lower and n.lower() != "keybinds"
        )
        out_lines.extend(_entry_for(n) for n in new_names)

        # Append Keybinds last with the standard header comment.
        if keybinds_deployed:
            out_lines.append("")
            out_lines.append("; Built-in keybinds, do not move up!")
            # Preserve the original Keybinds line if it had a custom state
            # (e.g. user disabled it), else use the consensus default.
            keybinds_name = next(
                (n for n in deployed_folder_names if n.lower() == "keybinds"),
                "Keybinds",
            )
            out_lines.append(keybinds_line if keybinds_line else _entry_for(keybinds_name))

        # If nothing remains besides blanks/comments we ourselves emitted,
        # delete the file so the empty-dir sweep can clean up the parent.
        # Without this, the "; Built-in keybinds, do not move up!" header we
        # wrote at deploy time would survive restore and keep the file alive.
        has_real_content = any(
            self._parse_mods_txt_line(l)[0] is not None for l in out_lines
        )
        if not has_real_content:
            if mods_file.is_file():
                try:
                    mods_file.unlink()
                    _log("  Removed empty UE4SS mods.txt")
                except OSError as exc:
                    _log(f"  WARN: could not remove {mods_file}: {exc}")
            return

        new_content = "\r\n".join(out_lines) + "\r\n"

        # Skip the write if nothing changed (avoids touching mtime needlessly).
        if mods_file.is_file():
            try:
                if mods_file.read_bytes() == new_content.encode("utf-8"):
                    return
            except OSError:
                pass

        try:
            mods_file.parent.mkdir(parents=True, exist_ok=True)
            mods_file.write_text(new_content, encoding="utf-8", newline="")
            _log(f"  Updated UE4SS mods.txt ({len(deployed_folder_names)} entries)")
        except OSError as exc:
            _log(f"  WARN: could not write {mods_file}: {exc}")

    def _collect_deployed_ue4ss_folders(
        self, manifest: list[str], dest_rel: str,
    ) -> set[str]:
        """From a deploy manifest, find folder names directly under ``dest_rel``
        that should get a ``mods.txt`` entry.

        Filters applied (post-deploy, against the live game tree):
          - Skip if the folder contains ``enabled.txt`` (UE4SS auto-loads via
            the per-folder marker — duplicate entry is unnecessary).
          - Skip if the folder doesn't contain ``Scripts/main.lua`` (UE4SS
            only treats folders with a main.lua as actual mods; everything
            else is library/shared code).

        Manifest entries are game-root-relative (custom-dir absolute entries
        are skipped — UE4SS lua mods always land in the game tree).
        """
        prefix = dest_rel.replace("\\", "/").strip("/").lower() + "/"
        candidate_folders: set[str] = set()
        for entry in manifest:
            if not entry:
                continue
            if Path(entry).is_absolute():
                continue
            norm = entry.replace("\\", "/").lstrip("/")
            if not norm.lower().startswith(prefix):
                continue
            tail = norm[len(prefix):]
            first_seg, _, rest = tail.partition("/")
            if not first_seg or not rest:
                # Loose file directly in the Mods dir (not a mod folder) — skip
                continue
            candidate_folders.add(first_seg)

        # Filter against the live game tree: folder must have Scripts/main.lua
        # and must NOT have enabled.txt.
        if self._game_path is None:
            return candidate_folders
        game_path = self.get_game_path()
        if game_path is None:
            return candidate_folders
        mods_root = game_path / dest_rel

        kept: set[str] = set()
        for folder in candidate_folders:
            folder_dir = mods_root / folder
            if (folder_dir / "enabled.txt").is_file():
                continue
            if not (folder_dir / "Scripts" / "main.lua").is_file():
                continue
            kept.add(folder)
        return kept

    # -----------------------------------------------------------------------
    # Deploy
    # -----------------------------------------------------------------------

    def deploy(
        self,
        log_fn=None,
        mode: LinkMode = LinkMode.HARDLINK,
        profile: str = "default",
        progress_fn=None,
    ) -> None:
        """Place each mod file directly into its resolved game destination.

        UE5 destination folders (Content/Paks, Binaries/Win64, etc.) contain
        game content that must not be moved.  We therefore skip the Core backup
        pattern and simply place files, tracking them in ue5_deployed.txt so
        restore() knows what to remove.
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path = self.get_game_path()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")

        filemap = self.get_effective_filemap_path()
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        staging = self.get_effective_mod_staging_path()
        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries)
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries)
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None

        prefix_rules = self._prefix_routing_rules()
        if prefix_rules:
            _log("Routing prefix-bound files via custom rules ...")
            deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=prefix_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                raw_mods=per_mod_raw or None,
                log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )
        overwrite_dir = staging.parent / "overwrite"

        manifest: list[str] = []
        vanilla_backup_dir = (self._game_path or game_path) / _VANILLA_BACKUP_DIR
        custom_vanilla_backup_dir = self.get_profile_root() / _CUSTOM_VANILLA_BACKUP_DIR
        linked = 0
        skipped = 0
        backed_up = 0

        lines = [
            line.rstrip("\n")
            for line in filemap.read_text(encoding="utf-8").splitlines()
            if "\t" in line
        ]

        # Build priority map so flatten/strip collisions resolve to the highest
        # priority mod's file rather than whichever line happens to deploy last.
        # Index 0 in modlist == top priority, so lower rank wins.
        modlist_path = profile_dir / "modlist.txt"
        priority_map: dict[str, int] = {}
        if modlist_path.is_file():
            for rank, e in enumerate(read_modlist(modlist_path)):
                priority_map[e.name] = rank
        # Pre-resolve every entry and dedupe by final destination path.
        # When multiple staged paths resolve to the same on-disk target
        # (typical with flatten=True), the higher-priority mod wins.
        # Use _resolve_filemap_entries (not per-line _resolve_entry) so
        # rules with include_siblings can drag in same-mod files under the
        # matched file's containing folder.
        parsed = [tuple(line.split("\t", 1)) for line in lines]
        rule_resolved = {
            (sr, mn): (dr, fr)
            for sr, mn, dr, fr in self._resolve_filemap_entries(
                [(sr, mn) for sr, mn in parsed]
            )
        }
        resolved_by_dest: dict[str, tuple[int, str, str, str, Path, Path, bool, str]] = {}
        prefix_skip_dest = getattr(self, "_PREFIX_SKIP_DEST", None)
        for staged_rel, mod_name in parsed:
            base_dir = per_mod_deploy.get(mod_name, game_path)
            in_custom_dir = base_dir != game_path
            if mod_name in per_mod_raw:
                final_rel = staged_rel.replace("\\", "/")
                dest_rel = ""
                dest_dir = base_dir
                dest_file = dest_dir / final_rel
            else:
                dest_rel, final_rel = rule_resolved[(staged_rel, mod_name)]
                # Files routed into the Proton/Wine prefix are placed by
                # deploy_custom_rules before this loop runs; skip them here.
                if prefix_skip_dest is not None and dest_rel == prefix_skip_dest:
                    continue
                dest_dir = (base_dir / dest_rel) if dest_rel else base_dir
                dest_file = dest_dir / final_rel
            key = str(dest_file)
            rank = priority_map.get(mod_name, 1 << 30)
            existing = resolved_by_dest.get(key)
            if existing is None or rank < existing[0]:
                resolved_by_dest[key] = (
                    rank, staged_rel, mod_name, final_rel,
                    dest_dir, dest_file, in_custom_dir, dest_rel,
                )
        deploy_order = list(resolved_by_dest.values())
        total = len(deploy_order)

        # The managed UE4SS mods.txt path — we own this file entirely; skip
        # any mod-shipped copy from being placed at this exact location.
        ue4ss_dest_rel = self._resolve_ue4ss_mods_dest()
        managed_mods_txt: Path | None = (
            game_path / ue4ss_dest_rel / "mods.txt"
            if ue4ss_dest_rel is not None else None
        )

        for i, (_rank, staged_rel, mod_name, final_rel,
                dest_dir, dest_file, in_custom_dir, dest_rel) in enumerate(deploy_order):

            # Skip mod-shipped mods.txt at the managed location — we generate
            # the canonical file ourselves after the deploy loop, so placing
            # a mod's copy here would just be overwritten and creates churn
            # in the vanilla-backup logic.
            if (managed_mods_txt is not None and not in_custom_dir
                    and dest_file == managed_mods_txt):
                if progress_fn:
                    progress_fn(i + 1, total)
                continue

            src = self._find_staged_file(
                staging, mod_name, staged_rel,
                per_mod_strip.get(mod_name, []),
                overwrite_dir,
                global_strips=self.mod_folder_strip_prefixes,
            )
            if src is None:
                _log(f"  WARN: source not found for {staged_rel} ({mod_name})")
                skipped += 1
                if progress_fn:
                    progress_fn(i + 1, total)
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                # Back up any real vanilla file before overwriting it.
                # Symlinks are our own previous deploys — don't back those up.
                if dest_file.is_file() and not dest_file.is_symlink():
                    if in_custom_dir:
                        # Mirror full absolute path so restore can reconstruct it.
                        rel_abs = dest_file.relative_to(dest_file.anchor)
                        backup_target = custom_vanilla_backup_dir / rel_abs
                    else:
                        game_rel = dest_file.relative_to(game_path)
                        backup_target = vanilla_backup_dir / game_rel
                    if not backup_target.exists():
                        backup_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dest_file, backup_target)
                        backed_up += 1

                if dest_file.exists() or dest_file.is_symlink():
                    dest_file.unlink()
                if mode == LinkMode.SYMLINK:
                    dest_file.symlink_to(src)
                elif mode == LinkMode.COPY:
                    shutil.copy2(src, dest_file)
                else:
                    try:
                        dest_file.hardlink_to(src)
                    except (OSError, NotImplementedError):
                        shutil.copy2(src, dest_file)
                # Record in manifest: absolute path for custom dirs, game-root-relative otherwise
                if in_custom_dir:
                    manifest.append(str(dest_file))
                else:
                    manifest.append(
                        ((dest_rel + "/" + final_rel) if dest_rel else final_rel)
                    )
                linked += 1
            except OSError as exc:
                _log(f"  ERROR placing {final_rel}: {exc}")
                skipped += 1

            if progress_fn:
                progress_fn(i + 1, total)

        # Write manifest so restore() knows exactly what to remove
        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST
        manifest_path.write_text("\n".join(manifest), encoding="utf-8")

        # Sync UE4SS mods.txt for games that need it (Palworld-style loaders
        # which require an explicit enable list rather than per-folder enabled.txt).
        ue4ss_dest = self._resolve_ue4ss_mods_dest()
        if ue4ss_dest is not None:
            try:
                folders = self._collect_deployed_ue4ss_folders(manifest, ue4ss_dest)
                # Build the disabled-by-consensus set from every source
                # mods.txt across staging — a folder defaults to ``: 0`` only
                # if every mod that mentions it sets it to 0. Reuses the
                # cached modindex.bin to avoid walking disk per mod.
                from Utils.filemap import read_mod_index as _read_mod_index
                _index = _read_mod_index(filemap.parent / "modindex.bin")
                enabled_mods = [
                    e.name for e in read_modlist(modlist_path)
                    if e.enabled and not e.is_separator
                ]
                disabled = self._collect_ue4ss_disabled_consensus(
                    staging, enabled_mods, index=_index,
                )
                self._update_ue4ss_mods_txt(folders, disabled_folders=disabled, log_fn=_log)
            except Exception as exc:
                _log(f"  WARN: could not update UE4SS mods.txt: {exc}")

        # Snapshot the game root so restore() can identify runtime-generated files
        # (saves, shader cache, config files written by the game after launch).
        snapshot_path = self.get_profile_root() / _FILEMAP_SNAPSHOT_NAME
        try:
            _write_deploy_snapshot(game_path, snapshot_path, log_fn=_log)
        except Exception as exc:
            _log(f"  WARN: could not write deploy snapshot: {exc}")

        backed_msg = f", {backed_up} vanilla file(s) backed up" if backed_up else ""
        _log(f"Deploy complete. {linked} file(s) placed{backed_msg}, {skipped} skipped.")

    def _find_staged_file(
        self,
        staging: Path,
        mod_name: str,
        staged_rel: str,
        mod_strips: list[str],
        overwrite_dir: Path,
        global_strips: set[str] | None = None,
    ) -> Path | None:
        """Locate the physical source file for a filemap entry.

        Tries in order:
          1. Overwrite dir
          2. staging/<mod>/<staged_rel>  (direct)
          3. staging/<mod>/<global_strip>/<staged_rel>  (re-add stripped prefix)
          4. staging/<mod>/<per_mod_strip>/<staged_rel>
        """
        ow = overwrite_dir / staged_rel
        if ow.is_file():
            return ow

        mod_root = staging / mod_name
        norm = staged_rel.replace("\\", "/")

        src = _resolve_nocase(mod_root, norm)
        if src is not None:
            return src

        # Re-add global strip prefixes (e.g. "oblivionremastered") — the
        # filemap stripped them during build but the file on disk still has them.
        # Use case-insensitive lookup since the prefix is stored lowercase.
        if global_strips:
            for prefix in sorted(global_strips, key=len, reverse=True):
                src = _resolve_nocase(mod_root, prefix + "/" + norm)
                if src is not None:
                    return src

        # Per-mod strip prefixes (user-configured ignore folders)
        for prefix in sorted(mod_strips, key=len, reverse=True):
            src = _resolve_nocase(mod_root, prefix + "/" + norm)
            if src is not None:
                return src

        return None

    # -----------------------------------------------------------------------
    # Restore
    # -----------------------------------------------------------------------

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Remove every file listed in ue5_deployed.txt, then delete empty dirs."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        game_path = self.get_game_path()
        if game_path is None:
            raise RuntimeError("Game path is not configured.")

        prefix_rules = self._prefix_routing_rules()
        if prefix_rules:
            filemap = self.get_effective_filemap_path()
            if filemap.is_file():
                _log("Restore: removing prefix-routed files ...")
                restore_custom_rules(
                    filemap, self._game_path,
                    rules=prefix_rules, log_fn=_log,
                    prefix_root=self.get_prefix_path(),
                )

        manifest_path = self.get_profile_root() / _DEPLOYED_MANIFEST
        if not manifest_path.is_file():
            _log("Restore: no deployed manifest found — nothing to remove.")
            return

        # Move runtime-generated files (saves, shader cache, etc.) to overwrite/
        # before removing deployed files, using the snapshot taken at deploy time.
        snapshot_path = self.get_profile_root() / _FILEMAP_SNAPSHOT_NAME
        overwrite_dir = self.get_effective_overwrite_path()
        if snapshot_path.is_file():
            _log("  Scanning game root for runtime-generated files ...")
            overwrite_dir.mkdir(parents=True, exist_ok=True)
            moved_rt = _move_runtime_files(game_path, snapshot_path, overwrite_dir, _log,
                                           restore_whitelist=self.restore_whitelist_matcher())
            _log(f"  Moved {moved_rt} runtime-generated file(s) to overwrite/.")
            try:
                snapshot_path.unlink()
            except OSError:
                pass

        lines = [
            l.strip() for l in manifest_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        removed = 0
        dirs_to_check: set[Path] = set()

        for rel in lines:
            # Absolute paths are custom-dir files; relative paths are game-root-relative
            is_abs = Path(rel).is_absolute()
            target = Path(rel) if is_abs else game_path / rel
            if target.is_file() or target.is_symlink():
                try:
                    target.unlink()
                    removed += 1
                    if is_abs:
                        dirs_to_check.add(target.parent)
                    else:
                        p = target.parent
                        while p != game_path:
                            dirs_to_check.add(p)
                            p = p.parent
                except OSError as exc:
                    _log(f"  WARN: could not remove {rel}: {exc}")

        # Strip our managed UE4SS mods.txt entries — leaves user sentinels intact,
        # or removes the file entirely if nothing else is left.
        ue4ss_dest = self._resolve_ue4ss_mods_dest()
        if ue4ss_dest is not None:
            try:
                self._update_ue4ss_mods_txt(set(), log_fn=_log)
                # Add the mods dir to the empty-dir sweep set so it can be
                # cleaned up if mods.txt was removed and nothing else remains.
                dirs_to_check.add(game_path / ue4ss_dest)
            except Exception as exc:
                _log(f"  WARN: could not clean UE4SS mods.txt: {exc}")

        # Restore any vanilla files that were displaced during deploy
        vanilla_backup_dir = (self._game_path or game_path) / _VANILLA_BACKUP_DIR
        restored_vanilla = 0
        if vanilla_backup_dir.is_dir():
            for backup_file in vanilla_backup_dir.rglob("*"):
                if not backup_file.is_file():
                    continue
                rel = backup_file.relative_to(vanilla_backup_dir)
                dest = game_path / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_file), dest)
                    restored_vanilla += 1
                except OSError as exc:
                    _log(f"  WARN: could not restore vanilla {rel}: {exc}")
            # Remove the backup dir (and any empty subdirs left behind)
            try:
                shutil.rmtree(vanilla_backup_dir)
            except OSError as exc:
                _log(f"  WARN: could not remove vanilla backup dir: {exc}")

        # Restore custom-dir vanilla files (e.g. engine.ini deployed to a
        # custom separator location outside the game root).
        custom_vanilla_backup_dir = self.get_profile_root() / _CUSTOM_VANILLA_BACKUP_DIR
        if custom_vanilla_backup_dir.is_dir():
            for backup_file in custom_vanilla_backup_dir.rglob("*"):
                if not backup_file.is_file():
                    continue
                # Reconstruct original absolute path from mirrored relative path.
                rel = backup_file.relative_to(custom_vanilla_backup_dir)
                dest = Path("/") / rel
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_file), dest)
                    restored_vanilla += 1
                    _log(f"  Restored {dest.name} to custom location")
                except OSError as exc:
                    _log(f"  WARN: could not restore custom vanilla {dest}: {exc}")
            try:
                shutil.rmtree(custom_vanilla_backup_dir)
            except OSError as exc:
                _log(f"  WARN: could not remove custom vanilla backup dir: {exc}")

        # Remove directories that became empty, deepest first
        for d in sorted(dirs_to_check, key=lambda p: len(p.parts), reverse=True):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

        manifest_path.unlink(missing_ok=True)
        vanilla_msg = f", {restored_vanilla} vanilla file(s) restored" if restored_vanilla else ""
        _log(f"Restore complete. {removed} file(s) removed{vanilla_msg}.")

    def validate_install(self) -> list[str]:
        errors: list[str] = []
        if not self.is_configured():
            errors.append(
                f"Game path not set or does not exist for '{self.name}'."
            )
        return errors
