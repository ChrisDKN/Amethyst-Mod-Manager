"""
RE Engine / Fluffy Mod Manager bundle detection and parsing.

Games that use Fluffy Mod Manager (RE Village, RE Requiem, Monster Hunter Wilds)
ship "bundle" archives whose top-level folders each carry a ``modinfo.ini``
describing how the folders relate.  There are two on-disk shapes - both flat,
i.e. every variant folder is an immediate child of one wrapper directory:

1. ``nameasbundle`` (flat "select one")
   Every immediate subdir has ``modinfo.ini`` with a shared ``nameasbundle=<name>``
   and no ``AddonFor``.  All variants are mutually exclusive - the user picks one.
   (e.g. "Main Menu Selector" → Day / Evening / Morning / Night.)

2. ``AddonFor`` tree (nested groups)
   Folders are physically flat but form a logical tree via ``AddonFor`` pointing
   at another entry's ``name=`` value:
     - **Root**:        ``AddonFor`` empty + ``Dummymod=True``  → the bundle itself.
     - **Group header**: ``Dummymod=True``, ``AddonFor=<root>``  → a "select one" group.
     - **Option**:      ``AddonFor=<group name>``               → exclusive within its group.
     - **Standalone**:  ``AddonFor=<root>``, not a Dummymod     → independent optional toggle.
   ``Dummymod=True`` content-less header folders are skipped as install targets but
   still define groups.  (e.g. "Drivetech Manon".)

This module is GUI-free.

**Install model (single mod):** a bundle installs as ONE normal mod - a single
mod folder, ``meta.ini`` and modlist row.  :func:`detect_re_bundle` discovers
the structure at install time, frozen into a :class:`BundleSpec` (groups +
ordered options + current selection) stored in the mod's ``meta.ini``
``[Bundle]`` section.  The original option folders are kept untouched in a hidden
``<mod>/.mm_bundle/`` library (skipped by the file scanner).
:func:`materialize_selection` overlays the *selected* options onto the mod root
(hardlink, copy fallback), prefix-stripped to deploy paths (``natives/...``),
applied in bundle order (independent optionals last so they override base/group
options).  The mod then scans/deploys like any normal mod.  The selection menu
(GUI) edits the spec and re-materialises.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


__all__ = [
    "BundleVariant",
    "BundleGroup",
    "BundleLayout",
    "BundleOption",
    "BundleSpecGroup",
    "BundleSpec",
    "parse_modinfo",
    "detect_re_bundle",
    "detect_bundle",
    "detect_multi_mod",
    "layout_to_spec",
    "merge_bundle_spec",
    "read_bundle_spec",
    "write_bundle_spec",
    "materialize_selection",
    "option_image",
    "option_description",
    "BUNDLE_LIB_DIR",
]


def parse_modinfo(ini_path: Path) -> dict[str, str] | None:
    """Parse a Fluffy ``modinfo.ini`` into a lower-cased-key dict.

    ``modinfo.ini`` files have no ``[section]`` header, so a synthetic ``[mod]``
    section is injected before parsing.  Values frequently carry trailing tabs /
    spaces (authoring artifacts) which are stripped.  Returns ``None`` if the
    file can't be read or parsed.
    """
    try:
        text = ini_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    cfg = configparser.RawConfigParser()
    try:
        cfg.read_string("[mod]\n" + text)
    except configparser.Error:
        return None
    out: dict[str, str] = {}
    for key, value in cfg.items("mod"):
        out[key.strip().lower()] = value.strip()
    return out


def _is_dummymod(info: dict[str, str]) -> bool:
    """True if ``Dummymod`` is set to a truthy value (a content-less header)."""
    return info.get("dummymod", "").strip().lower() in ("true", "1", "yes")


def _has_deployable_files(folder: Path) -> bool:
    """True if *folder* contains at least one deployable file - i.e. a file that
    is not Fluffy bookkeeping (modinfo.ini or a screenshot image).  Flat
    ``NameAsBundle`` bundles often include content-less folders purely as visual
    section dividers / info text (``---cover---``, ``-2 ---Body--- 2-``); those
    return False so they can be shown as non-selectable labels."""
    try:
        for p in folder.rglob("*"):
            if p.is_file() and not _is_option_metadata(p.name):
                return True
    except OSError:
        pass
    return False


@dataclass
class BundleVariant:
    """One installable folder inside a bundle (an option or standalone addon).

    ``is_label`` marks a content-less divider / info entry (no deployable files):
    shown in the menu as a non-selectable label, never materialised.
    """
    name: str          # display name from modinfo ``name=`` (falls back to folder)
    path: str          # absolute path to the variant folder
    folder: str        # on-disk folder name
    is_label: bool = False


@dataclass
class BundleGroup:
    """A logical group of variants.

    ``select_one`` groups are mutually exclusive (radio behaviour); the first
    variant is enabled by default.  Non-``select_one`` groups hold independent
    checkbox toggles.  ``flat`` marks the single group produced by a flat
    ``NameAsBundle`` bundle (no machine-readable grouping): its default selection
    enables the first real option per ``-N-`` name prefix rather than all options
    (which would deploy conflicting alternatives).
    """
    name: str                                   # group display name (header / bundle name)
    select_one: bool                            # True → mutually exclusive
    variants: list[BundleVariant] = field(default_factory=list)
    flat: bool = False


@dataclass
class BundleLayout:
    """Structured result of detecting a Fluffy bundle."""
    bundle_name: str
    groups: list[BundleGroup] = field(default_factory=list)

    @property
    def variant_count(self) -> int:
        return sum(len(g.variants) for g in self.groups)


# "<Word> [Option ]<N>" - the author's intended sequence lives in the *label*
# (e.g. ``Texture Option 3``), not the folder name, which often sorts the
# variants into a scrambled order.  Captures the section word + number.
_LABEL_ORDER_RE = re.compile(r"^\s*([A-Za-z]+)\s+(?:option\s+)?(\d+)\b",
                             re.IGNORECASE)


def _variant_sort_key(index: int, name: str) -> tuple:
    """Stable sort key ordering variants within a group by the number in their
    label.  ``Mesh Option 1/2`` then ``Texture Option 1/2/3/4`` - grouped by the
    leading word, each ascending by number.  Labels without a ``<Word> N`` shape
    (e.g. a ``Main Files`` entry) sort *before* numbered ones, keeping their
    original disk order via *index* so nothing else gets reshuffled."""
    m = _LABEL_ORDER_RE.match(name)
    if not m:
        return (0, "", 0, index)        # un-numbered: keep first, in disk order
    return (1, m.group(1).lower(), int(m.group(2)), index)


def _sorted_variants(variants: "list[BundleVariant]") -> "list[BundleVariant]":
    """Return *variants* ordered by :func:`_variant_sort_key` (label number),
    stable on the original sequence for anything without a number."""
    return [v for _, v in sorted(
        ((_variant_sort_key(i, v.name), v) for i, v in enumerate(variants)),
        key=lambda t: t[0],
    )]


def _scan_variant_dirs(root: Path) -> list[tuple[Path, dict[str, str]]] | None:
    """Return ``[(subdir, modinfo_dict), ...]`` for every immediate subdir that
    has a parseable ``modinfo.ini``.  Returns ``None`` unless there are at least
    two such subdirs and *every* immediate subdir qualifies (so a stray
    non-bundle folder rules the whole archive out)."""
    subdirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(subdirs) < 2:
        return None
    found: list[tuple[Path, dict[str, str]]] = []
    for subdir in subdirs:
        ini = subdir / "modinfo.ini"
        if not ini.is_file():
            return None
        info = parse_modinfo(ini)
        if info is None:
            return None
        found.append((subdir, info))
    return found if len(found) >= 2 else None


def detect_re_bundle(extract_dir: str) -> BundleLayout | None:
    """Detect a Fluffy bundle and return its grouped layout, or ``None``.

    Handles three on-disk shapes (all "every immediate subdir has a
    ``modinfo.ini``"):

    1. **AddonFor tree** - any ``AddonFor`` present → :func:`_build_addonfor_layout`.
    2. **Flat ``NameAsBundle``** - one independent group per distinct bundle name
       (an archive may mix several), plus a trailing group for un-bundled
       standalones.
    3. **Plain multi-mod** - no ``NameAsBundle`` and no ``AddonFor`` anywhere, yet
       every subdir is a self-describing Fluffy mod.  These are facets of one
       Nexus download (e.g. per-character variants), so we install them as ONE
       bundle (a single independent group, each folder a toggleable option) - a
       single mod folder + meta.ini, so Nexus update/endorse actions work.
    """
    root = Path(extract_dir)
    scanned = _scan_variant_dirs(root)
    if scanned is None:
        return None

    # --- Format 2: AddonFor tree -------------------------------------------
    # An AddonFor anywhere means the author intended the tree format.
    has_addonfor = any(info.get("addonfor", "") for _, info in scanned)
    if has_addonfor:
        return _build_addonfor_layout(scanned)

    # --- Format 1: flat nameasbundle ---------------------------------------
    # The flat format carries NO machine-readable grouping - Fluffy just shows a
    # flat checklist and the user picks freely (no enforced "select one").  A
    # single archive may mix several ``NameAsBundle`` values (a base bundle plus
    # a sub-bundle, e.g. "Gemma..." + "...Summer Coveralls"), and may include
    # folders with NO ``NameAsBundle`` at all (plain ``name=`` standalones).  We
    # build one independent (checkbox) group per distinct bundle name, in first-
    # seen order, plus a trailing group for the un-bundled standalones - nothing
    # is dropped.  Content-less folders (no deployable files) are visual dividers
    # / info text → kept as labels.
    #
    # --- Format 3: plain multi-mod (no NameAsBundle, no AddonFor) -----------
    # When NO folder carries a bundle name, the folders are independent Fluffy
    # mods shipped in one archive.  We still bundle them (one group) so they
    # install as a single mod with one meta.ini.  The group is named after the
    # folders' common name prefix.
    has_nameasbundle = any(info.get("nameasbundle", "") for _, info in scanned)

    # bundle-name (preserve first-seen casing) → list of variants, in disk order.
    grouped: "dict[str, list[BundleVariant]]" = {}
    display_name: dict[str, str] = {}   # lower → first-seen display casing
    standalones: list[BundleVariant] = []
    for subdir, info in scanned:
        vname = info.get("name", "") or subdir.name
        is_label = not _has_deployable_files(subdir)
        variant = BundleVariant(name=vname, path=str(subdir),
                                folder=subdir.name, is_label=is_label)
        bname = info.get("nameasbundle", "").strip()
        if not bname:
            standalones.append(variant)
            continue
        key = bname.lower()
        display_name.setdefault(key, bname)
        grouped.setdefault(key, []).append(variant)

    groups: list[BundleGroup] = []
    for key, variants in grouped.items():
        groups.append(BundleGroup(name=display_name[key], select_one=False,
                                  variants=_sorted_variants(variants), flat=True))
    if standalones:
        # Un-bundled folders.  When the archive has NamedAsBundle groups too,
        # these are leftovers → a trailing "- Other" group, kept ``flat`` so its
        # section-default (first per ``-N-`` section) applies.  When there are NO
        # named groups at all (plain multi-mod), they ARE the bundle: independent
        # per-mod folders → name the group after the common prefix and mark it
        # NOT flat so every option defaults on (the override-prune pass still
        # drops any that another fully covers, e.g. "Type A" ⊆ "Type B").
        if has_nameasbundle:
            other_name = next(iter(display_name.values()), "")
            grp_name = f"{other_name} - Other" if other_name else "Other"
            grp_flat = True
        else:
            grp_name = _common_name_prefix(
                [v.name for v in standalones if not v.is_label]) or "Bundle"
            grp_flat = False
        groups.append(BundleGroup(
            name=grp_name, select_one=False,
            variants=_sorted_variants(standalones), flat=grp_flat))

    if sum(1 for g in groups for v in g.variants if not v.is_label) < 2:
        return None
    # Overall display name = the bundle with the most options (most prominent),
    # not whichever happened to sort first.  For a plain multi-mod (no named
    # groups), use the common-prefix group name.
    if display_name:
        bundle_name = max(display_name, key=lambda k: len(grouped[k]))
        bundle_name = display_name[bundle_name]
    else:
        bundle_name = groups[0].name if groups else "Bundle"
    return BundleLayout(bundle_name=bundle_name, groups=groups)


def _common_name_prefix(names: "list[str]") -> str:
    """Return the longest common word-boundary prefix of *names*, trimmed of
    trailing separators (used to name a plain multi-mod bundle, e.g.
    ``["Dormant Jiggle Physics: Chun-Li", "Dormant Jiggle Physics: Lily"]`` →
    ``"Dormant Jiggle Physics"``).  "" if there's no shared multi-word prefix."""
    if not names:
        return ""
    split = [n.split() for n in names]
    common: list[str] = []
    for words in zip(*split):
        first = words[0]
        if all(w == first for w in words):
            common.append(first)
        else:
            break
    prefix = " ".join(common).rstrip(" :-_–-|/").strip()
    # Only useful as a label if it actually shortens / shares something.
    return prefix if len(common) >= 1 and prefix else ""


def _build_addonfor_layout(
    scanned: list[tuple[Path, dict[str, str]]],
) -> BundleLayout | None:
    """Resolve the AddonFor tree into ordered groups.

    The tree is keyed by each entry's ``name`` value (what ``AddonFor`` points
    at), matched case-insensitively.  Roles:

    - **Root** = a Dummymod with empty ``AddonFor`` *or* a Dummymod whose own
      ``name`` is the ``AddonFor`` target of the options (a bundle that nests
      itself under another dummymod, e.g. ``999.HAIR`` → ``AddonFor=Just
      Cleavage`` while every option says ``AddonFor=999.HAIR``).
    - **Header** (other Dummymod that options point at) → a group; its children
      (entries whose ``AddonFor`` equals the header's name) are the options.
    - **Standalone** (not Dummymod, AddonFor=root) → independent optional toggle.

    ``AddonFor`` in Fluffy means "independent add-on", so header groups are
    **independent** (checkbox) toggles - the options are not mutually exclusive
    (different hair slots, etc.).  Children of any non-root header become that
    group's options.  Anything whose ``AddonFor`` doesn't resolve to a known
    header is treated as a root-level standalone so nothing is silently dropped.
    """
    # name (lower) → (subdir, info)
    by_name: dict[str, tuple[Path, dict[str, str]]] = {}
    for subdir, info in scanned:
        nm = (info.get("name", "") or subdir.name).strip().lower()
        by_name.setdefault(nm, (subdir, info))

    # Names that non-dummymod options point at via AddonFor (the parents that
    # actually hold installable options).
    pointed_at = {
        info.get("addonfor", "").strip().lower()
        for subdir, info in scanned
        if not _is_dummymod(info) and info.get("addonfor", "").strip()
    }

    # Identify the root(s): a dummymod with empty AddonFor, OR a dummymod whose
    # own name is what the options point at (a bundle nested under another
    # dummymod - e.g. ``999.HAIR`` carries ``AddonFor=Just Cleavage`` yet every
    # option says ``AddonFor=999.HAIR``).  The latter would otherwise be mistaken
    # for a child header and collapse all options into one wrong group.
    root_names = {
        (info.get("name", "") or subdir.name).strip().lower()
        for subdir, info in scanned
        if _is_dummymod(info) and not info.get("addonfor", "")
    }

    # Any other dummymod that options point at is a group header; its children
    # (entries whose AddonFor equals the header's name) are independent options.
    header_names = {
        nm
        for subdir, info in scanned
        if _is_dummymod(info)
        and (nm := (info.get("name", "") or subdir.name).strip().lower()) not in root_names
        and nm in pointed_at
    }

    # children[header_name_lower] = [(subdir, info), ...] in disk order
    children: dict[str, list[tuple[Path, dict[str, str]]]] = {}
    headers: list[tuple[Path, dict[str, str]]] = []   # dummymod group headers (non-root)
    standalones: list[tuple[Path, dict[str, str]]] = []

    for subdir, info in scanned:
        nm = (info.get("name", "") or subdir.name).strip().lower()
        parent = info.get("addonfor", "").strip().lower()
        if nm in root_names:
            continue  # the root itself installs nothing
        if _is_dummymod(info):
            headers.append((subdir, info))   # a group header - installs nothing itself
            continue
        if parent and parent in header_names:
            children.setdefault(parent, []).append((subdir, info))
        else:
            # AddonFor=root (or an unresolved/empty parent) → independent toggle.
            standalones.append((subdir, info))

    bundle_name = ""
    for subdir, info in scanned:
        if _is_dummymod(info) and not info.get("addonfor", ""):
            bundle_name = info.get("name", "") or subdir.name
            break
    if not bundle_name:
        # No explicit root - fall back to a common AddonFor target name.
        bundle_name = next(
            (info.get("addonfor", "") for _, info in scanned if info.get("addonfor", "")),
            "Bundle",
        )

    groups: list[BundleGroup] = []

    # Standalone addons first (Main files, optionals) - independent toggles.
    if standalones:
        groups.append(BundleGroup(
            name=bundle_name,
            select_one=False,
            variants=_sorted_variants([
                BundleVariant(
                    name=info.get("name", "") or subdir.name,
                    path=str(subdir), folder=subdir.name,
                    is_label=not _has_deployable_files(subdir),
                )
                for subdir, info in standalones
            ]),
        ))

    # One independent (checkbox) group per dummymod header.  AddonFor options are
    # independent add-ons, not mutually exclusive; ordered by label number.
    for h_subdir, h_info in headers:
        h_name = h_info.get("name", "") or h_subdir.name
        opts = children.get(h_name.strip().lower(), [])
        if not opts:
            continue  # empty group - nothing to install
        groups.append(BundleGroup(
            name=h_name,
            select_one=False,
            variants=_sorted_variants([
                BundleVariant(
                    name=info.get("name", "") or subdir.name,
                    path=str(subdir), folder=subdir.name,
                    is_label=not _has_deployable_files(subdir),
                )
                for subdir, info in opts
            ]),
        ))

    if not any(g.variants for g in groups):
        return None
    return BundleLayout(bundle_name=bundle_name, groups=groups)


# ---------------------------------------------------------------------------
# Legacy detectors - kept for callers that only need the flat shapes.
# ---------------------------------------------------------------------------

def detect_bundle(extract_dir: str) -> tuple[str, list[tuple[str, str]]] | None:
    """Flat ``nameasbundle`` detector (legacy shape).

    Returns ``(bundle_name, [(variant_name, variant_path), ...])`` or ``None``.
    Prefer :func:`detect_re_bundle`; this is retained for the simple case.
    """
    layout = detect_re_bundle(extract_dir)
    if layout is None or len(layout.groups) != 1 or not layout.groups[0].select_one:
        return None
    grp = layout.groups[0]
    return layout.bundle_name, [(v.name, v.path) for v in grp.variants]


def detect_multi_mod(extract_dir: str) -> list[tuple[str, str]] | None:
    """Detect a multi-mod archive: every immediate subdir has a ``modinfo.ini``
    but there's no shared ``nameasbundle`` and no ``AddonFor`` (so each subdir is
    an independent mod).  Returns ``[(mod_name, subdir_path), ...]`` or ``None``.
    """
    root = Path(extract_dir)
    scanned = _scan_variant_dirs(root)
    if scanned is None:
        return None
    # If it's actually a bundle (either format), it's not a plain multi-mod.
    if any(info.get("nameasbundle", "") or info.get("addonfor", "")
           for _, info in scanned):
        return None
    return [
        (info.get("name", "") or subdir.name, str(subdir))
        for subdir, info in scanned
    ]


# ===========================================================================
# Single-mod bundle spec - persisted selection + virtual file resolution
# ===========================================================================

@dataclass
class BundleOption:
    """One option within a bundle group.

    ``folder`` is the on-disk subfolder name (kept inside the single mod folder);
    it is the path prefix stripped from the option's files when resolved.
    ``is_label`` marks a content-less divider / info entry - shown in the menu as
    a non-selectable label, never selected and never materialised.
    """
    folder: str
    label: str
    selected: bool = False
    is_label: bool = False

    def to_dict(self) -> dict:
        d = {"folder": self.folder, "label": self.label, "selected": self.selected}
        if self.is_label:
            d["is_label"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BundleOption":
        return cls(
            folder=str(d.get("folder", "")),
            label=str(d.get("label", "")) or str(d.get("folder", "")),
            selected=bool(d.get("selected", False)),
            is_label=bool(d.get("is_label", False)),
        )


@dataclass
class BundleSpecGroup:
    """A group of options (``select_one`` → radios, else independent checkboxes).
    ``flat`` marks the flat-NameAsBundle group (see :class:`BundleGroup`)."""
    name: str
    select_one: bool
    options: list[BundleOption] = field(default_factory=list)
    flat: bool = False

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "select_one": self.select_one,
            "options": [o.to_dict() for o in self.options],
        }
        if self.flat:
            d["flat"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BundleSpecGroup":
        return cls(
            name=str(d.get("name", "")),
            select_one=bool(d.get("select_one", False)),
            options=[BundleOption.from_dict(o) for o in d.get("options", [])
                     if isinstance(o, dict)],
            flat=bool(d.get("flat", False)),
        )


@dataclass
class BundleSpec:
    """Persisted bundle structure + current selection (stored in meta.ini)."""
    groups: list[BundleSpecGroup] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"groups": [g.to_dict() for g in self.groups]},
                          ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "BundleSpec | None":
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        groups = [BundleSpecGroup.from_dict(g) for g in data.get("groups", [])
                  if isinstance(g, dict)]
        if not groups:
            return None
        return cls(groups=groups)

    def selected_folders(self) -> set[str]:
        return {o.folder for g in self.groups for o in g.options if o.selected}


def _name_prefix_group(name: str) -> str:
    """Derive a flat-bundle option's "section" key, used only to pick a sensible
    default ("first real option per section", so mutually-exclusive alternatives
    don't all deploy at once).  Recognises two authoring conventions:

    - a ``-N-`` section marker (e.g. ``-2- body-01`` → ``2``);
    - a ``<Word> Option N`` / ``<Word> N`` label (e.g. ``Mesh Option 1`` →
      ``mesh``, ``Texture Option 3`` → ``texture``) - the *word* is the section
      so every "Mesh Option N" collapses to one default-on entry.

    Returns "" when neither pattern matches (each such option is its own
    section, hence default-on)."""
    m = re.search(r"(?:^|[^0-9])-(\d+)-(?:[^0-9]|$)", name)
    if m:
        return m.group(1)
    m = re.match(r"\s*([A-Za-z]+)\s+(?:option\s+)?\d+\b", name, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return ""


def layout_to_spec(layout: BundleLayout) -> BundleSpec:
    """Build a :class:`BundleSpec` from a freshly detected :class:`BundleLayout`,
    applying the default selection:
      - select-one group → the first non-label option;
      - flat NameAsBundle group → the first real option per ``-N-`` section
        (so a multi-part bundle deploys one of each part, not conflicting
        alternatives all at once);
      - other independent group (AddonFor optionals) → every option on;
      - label entries are never selected.

    Independent groups default every option on, but many bundles (e.g. skin/hair
    variants) have several options that write the *same* files.  As a final pass
    (:func:`_prune_overridden_defaults`) we keep, for each set of fully-
    overlapping alternatives, the **topmost** option and turn the rest off - so a
    fresh install starts with one contributor per file and the first listed wins.
    The user can re-enable any of them in the Bundle Options dialog.
    """
    groups: list[BundleSpecGroup] = []
    for g in layout.groups:
        opts: list[BundleOption] = []

        # Precompute the default-selected folder set for this group.
        default_on: set[str] = set()
        if g.select_one:
            first = next((v for v in g.variants if not v.is_label), None)
            if first is not None:
                default_on.add(first.folder)
        elif g.flat:
            seen_sections: set[str] = set()
            for v in g.variants:
                if v.is_label:
                    continue
                sec = _name_prefix_group(v.name)
                if sec not in seen_sections:
                    seen_sections.add(sec)
                    default_on.add(v.folder)
        else:
            default_on = {v.folder for v in g.variants if not v.is_label}

        for v in g.variants:
            opts.append(BundleOption(
                folder=v.folder, label=v.name,
                selected=(v.folder in default_on),
                is_label=v.is_label,
            ))
        groups.append(BundleSpecGroup(name=g.name, select_one=g.select_one,
                                      options=opts, flat=g.flat))

    spec = BundleSpec(groups=groups)
    _prune_overridden_defaults(spec, layout)
    return spec


def _prune_overridden_defaults(spec: BundleSpec, layout: BundleLayout) -> None:
    """Turn off default-selected options that conflict with an earlier one, so a
    fresh install starts with one contributor per file and the **topmost** option
    of any fully-overlapping set is the one left on.  Mutates *spec* in place;
    select-one options are left alone (their group needs one winner).

    Deploy apply order is "lower in the list wins" - so naively the *bottom*
    alternative would survive.  To make the *top* the default, we walk options in
    list order and claim each file for the *first* selected option to provide it;
    an option none of whose files are still unclaimed (i.e. an earlier option
    already covers them all) is turned off.

    File sets come from each variant's extracted folder (``BundleVariant.path``).
    Pruning one option can free files for a later one, so the pass repeats until
    stable.  If file sets can't be read, the selection is unchanged.
    """
    # folder → deployable rel set, read from the extracted option folders.
    files: dict[str, set[str]] = {}
    for g in layout.groups:
        for v in g.variants:
            if v.is_label:
                continue
            try:
                files[v.folder] = _deployable_rels_at(Path(v.path))
            except OSError:
                files[v.folder] = set()

    opt_by_folder = {o.folder: o for g in spec.groups for o in g.options}
    # Only independent (non select-one) options are eligible to be turned off.
    independent = {o.folder for g in spec.groups if not g.select_one
                   for o in g.options}
    # Options in display (top→bottom) order, grouped as shown.
    display_order = [o.folder for g in spec.groups for o in g.options
                     if not o.is_label]

    for _ in range(len(opt_by_folder) + 1):
        claimed: set[str] = set()
        changed = False
        # First-come-first-served in DISPLAY order: the top option claims its
        # files; a lower option whose files are all already claimed is dropped.
        for folder in display_order:
            opt = opt_by_folder.get(folder)
            if opt is None or not opt.selected:
                continue
            ofiles = files.get(folder, set())
            if folder in independent and ofiles and ofiles <= claimed:
                opt.selected = False
                changed = True
                continue
            claimed |= ofiles
        if not changed:
            break


def merge_bundle_spec(new_spec: BundleSpec, old_spec: BundleSpec) -> BundleSpec:
    """Carry the user's saved selection (and order) from *old_spec* onto a freshly
    detected *new_spec*, so reinstalling / updating a bundle keeps the user's
    choices.  Identity is the option ``folder`` (the stable on-disk name).

    - Options present in both → keep the old ``selected`` state.
    - Options new in this version → keep *new_spec*'s default selection.
    - Options removed in this version → dropped (absent from *new_spec*).
    - Order: surviving options keep the user's old relative order; options new in
      this version are appended (in new-layout order) after them.
    - Labels are never selected.  A select-one group whose old choice was removed
      falls back to selecting its first real option so it isn't left empty.
    """
    old_by_group: dict[str, BundleSpecGroup] = {g.name: g for g in old_spec.groups}

    merged_groups: list[BundleSpecGroup] = []
    for ng in new_spec.groups:
        og = old_by_group.get(ng.name)
        if og is None:
            merged_groups.append(ng)  # wholly new group - keep defaults
            continue

        old_sel = {o.folder: o.selected for o in og.options}
        old_order = {o.folder: i for i, o in enumerate(og.options)}

        for o in ng.options:
            if o.is_label:
                o.selected = False
            elif o.folder in old_sel:
                o.selected = old_sel[o.folder]
            # else: new option - keep its default `selected`

        # Reorder: surviving options by old position (stable), new options after.
        ng.options.sort(key=lambda o: (
            0 if o.folder in old_order else 1,
            old_order.get(o.folder, 0),
        ))

        if ng.select_one and not any(o.selected and not o.is_label for o in ng.options):
            first = next((o for o in ng.options if not o.is_label), None)
            if first is not None:
                first.selected = True

        merged_groups.append(ng)

    return BundleSpec(groups=merged_groups)


# meta.ini [Bundle] section ------------------------------------------------
_BUNDLE_SECTION = "Bundle"
_BUNDLE_KEY = "spec"


def read_bundle_spec(meta_ini_path: Path) -> "BundleSpec | None":
    """Read the ``[Bundle] spec`` JSON from a mod's meta.ini, or None if absent."""
    if not meta_ini_path.is_file():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(str(meta_ini_path), encoding="utf-8")
    except configparser.Error:
        return None
    raw = cp.get(_BUNDLE_SECTION, _BUNDLE_KEY, fallback="")
    if not raw:
        return None
    # configparser doubles literal % on write - undo for our JSON blob.
    return BundleSpec.from_json(raw.replace("%%", "%"))


def write_bundle_spec(meta_ini_path: Path, spec: BundleSpec) -> None:
    """Write/update the ``[Bundle] spec`` JSON in a mod's meta.ini, preserving
    every other section (Nexus metadata, FOMOD flags, etc.)."""
    cp = configparser.ConfigParser()
    if meta_ini_path.is_file():
        try:
            cp.read(str(meta_ini_path), encoding="utf-8")
        except configparser.Error:
            pass
    if not cp.has_section(_BUNDLE_SECTION):
        cp.add_section(_BUNDLE_SECTION)
    cp.set(_BUNDLE_SECTION, _BUNDLE_KEY, spec.to_json().replace("%", "%%"))
    meta_ini_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_ini_path, "w", encoding="utf-8") as f:
        cp.write(f)


# Library folder (inside the mod) that holds the original extracted option
# folders.  Hidden from the file scanner (see filemap._scan_dir skip) so only
# the materialised selection at the mod root is indexed/deployed.
BUNDLE_LIB_DIR = ".mm_bundle"
_MATERIALIZED_MANIFEST = ".materialized"


def option_image(lib_dir: Path, folder: str) -> "Path | None":
    """Return the preview image for option *folder* under the bundle library, or
    None.  Prefers the modinfo ``screenshot=`` file, else the first image found
    at the option root."""
    opt_root = lib_dir / folder
    if not opt_root.is_dir():
        return None
    info = parse_modinfo(opt_root / "modinfo.ini")
    if info:
        shot = info.get("screenshot", "").strip()
        if shot:
            cand = opt_root / shot
            if cand.is_file():
                return cand
    try:
        for p in sorted(opt_root.iterdir()):
            if p.is_file() and p.suffix.lower() in _OPTION_META_EXT:
                return p
    except OSError:
        pass
    return None
_OPTION_META_EXT = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")


def option_description(lib_dir: Path, folder: str) -> str:
    """Return option *folder*'s ``description=`` from its modinfo.ini (stripped),
    or "" if absent/blank.  Used for the option-row hover tooltip."""
    info = parse_modinfo(lib_dir / folder / "modinfo.ini")
    if not info:
        return ""
    return info.get("description", "").strip()


def _is_option_metadata(rel_tail: str) -> bool:
    """True for Fluffy per-option bookkeeping at the option root: ``modinfo.ini``
    and the screenshot image - not deployable mod content."""
    if rel_tail.lower() == "modinfo.ini":
        return True
    return rel_tail.lower().endswith(_OPTION_META_EXT)


def _deployable_rels_at(opt_root: Path) -> set[str]:
    """Lowercased mod-root-relative paths the option folder at *opt_root* would
    materialise (prefix stripped, Fluffy bookkeeping skipped).  Works on any
    absolute option-folder path (extract dir or ``.mm_bundle`` library)."""
    if not opt_root.is_dir():
        return set()
    rels: set[str] = set()
    for src in opt_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(opt_root).as_posix()
        if "/" not in rel and _is_option_metadata(rel):
            continue
        if _is_option_metadata(src.name) and src.parent == opt_root:
            continue
        rels.add(rel.lower())
    return rels


def option_deployable_rels(lib_dir: Path, folder: str) -> set[str]:
    """Return the lowercased mod-root-relative paths option *folder* would
    materialise (the option-folder prefix stripped, Fluffy bookkeeping skipped).

    This mirrors :func:`materialize_selection`'s per-option file resolution so
    callers (e.g. the Bundle Options conflict view) can tell, before any deploy,
    which selected options write the same files.
    """
    return _deployable_rels_at(lib_dir / folder)


def _ordered_selected_folders(spec: BundleSpec) -> list[str]:
    """Selected option folders in apply order: select-one groups first (declared
    order), independent groups last so optional add-ons override base/group
    files when they touch the same path."""
    select_one: list[str] = []
    independent: list[str] = []
    for g in spec.groups:
        bucket = select_one if g.select_one else independent
        for o in g.options:
            if o.selected and not o.is_label:
                bucket.append(o.folder)
    return select_one + independent


def _read_materialized_manifest(lib_dir: Path) -> list[str]:
    p = lib_dir / _MATERIALIZED_MANIFEST
    if not p.is_file():
        return []
    try:
        return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln]
    except OSError:
        return []


def _write_materialized_manifest(lib_dir: Path, rels: list[str]) -> None:
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / _MATERIALIZED_MANIFEST).write_text(
        "\n".join(rels), encoding="utf-8")


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink *src*→*dst*, falling back to copy (cross-device / FS without
    hardlinks).  Overwrites an existing *dst*."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
        except OSError:
            pass
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def materialize_selection(mod_dir: Path, spec: BundleSpec) -> list[str]:
    """Overlay the bundle's *selected* options onto the mod root.

    The original option folders live untouched under ``<mod>/.mm_bundle/``.  This
    removes whatever was materialised last time (per the manifest), then links
    every file of each selected option (in :func:`_ordered_selected_folders`
    order - optionals last so they override) to the mod root with the option
    folder prefix stripped (``natives/...``).  Fluffy per-option bookkeeping
    (modinfo.ini, screenshots) is skipped.

    Returns the list of materialised mod-root-relative paths (also recorded in
    the manifest so the next call can clean up).
    """
    lib_dir = mod_dir / BUNDLE_LIB_DIR

    # 1. Remove previously materialised files (and now-empty parent dirs).
    old = _read_materialized_manifest(lib_dir)
    for rel in old:
        f = mod_dir / rel
        try:
            if f.is_file() or f.is_symlink():
                f.unlink()
        except OSError:
            pass
    _prune_empty_dirs(mod_dir, {Path(r).parent for r in old}, stop=lib_dir.name)

    # 2. Materialise the current selection, in apply order.
    materialised: list[str] = []
    seen: set[str] = set()  # lowercased rel - later option overrides earlier
    for folder in _ordered_selected_folders(spec):
        opt_root = lib_dir / folder
        if not opt_root.is_dir():
            continue
        for src in sorted(opt_root.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(opt_root).as_posix()
            if "/" not in rel and _is_option_metadata(rel):
                continue
            if _is_option_metadata(src.name) and src.parent == opt_root:
                continue
            _link_or_copy(src, mod_dir / rel)
            low = rel.lower()
            if low not in seen:
                seen.add(low)
                materialised.append(rel)

    _write_materialized_manifest(lib_dir, materialised)
    return materialised


def _prune_empty_dirs(root: Path, dirs: "set[Path]", stop: str) -> None:
    """Remove now-empty directories (deepest first), never touching the library
    dir named *stop* or *root* itself."""
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        cur = root / d
        while cur != root and cur.name and cur.name != stop:
            try:
                cur.rmdir()
            except OSError:
                break  # not empty (or gone) - stop climbing
            cur = cur.parent
