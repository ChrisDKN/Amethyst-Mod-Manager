"""Modlist filter engine (Qt) - a faithful port of the Tk
ModListPanel._compute_visible_indices filtering logic.

Pure backend: takes the model's ModEntry list + a FilterData bundle (conflict
maps, the various "mods that have X" sets, categories, filetype membership) + the
side-panel state dict, and returns the SET OF ROW INDICES TO HIDE. The view
unions this with its collapse hide-set and calls setRowHidden.

The tri-state semantics, separator-block awareness ("keep a separator if any
mod in its block matches"), and the independent include/exclude handling all
mirror the Tk engine so the same edge cases are covered. Data builders
(categories, filetypes, BSA, PBR) read the persisted backend indexes directly -
the same sources the Tk panel uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from os.path import splitext
from pathlib import Path

from Utils.filemap import (
    OVERWRITE_NAME, ROOT_FOLDER_NAME,
    CONFLICT_NONE, CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL,
)


# Status checkbox keys (match the Tk _FILTER_CHECKBOXES var_keys 1:1 so the
# spec, the state dict, and the engine all agree). Value = which engine step
# consumes it. Order here is the display order in the panel.
STATUS_FILTERS: tuple[tuple[str, str], ...] = (
    ("filter_show_disabled",        "Disabled mods"),
    ("filter_show_enabled",         "Enabled mods"),
    ("filter_hide_separators",      "Hide separators"),
    ("filter_winning",              "Winning conflicts"),
    ("filter_losing",               "Losing conflicts"),
    ("filter_partial",              "Winning & losing conflicts"),
    ("filter_full",                 "Fully conflicted mods"),
    ("filter_missing_reqs",         "Missing requirements"),
    ("filter_has_disabled_plugins", "Mods with disabled plugins"),
    ("filter_has_plugins",          "Mods with plugins"),
    ("filter_has_disabled_files",   "Mods modified in Mod Files tab"),
    ("filter_has_updates",          "Mods with updates"),
    ("filter_has_notes",            "Mods with notes"),
    ("filter_fomod_only",           "FOMOD mods"),
    ("filter_bain_only",            "BAIN mods"),
    ("filter_has_bsa",              "Mods with BSA archives"),
    ("filter_has_pbr",              "PGPatcher mods"),
)

# Conflict checkbox key -> the CONFLICT_* code it includes/excludes.
_CONFLICT_KEYS = {
    "filter_winning": CONFLICT_WINS,
    "filter_losing": CONFLICT_LOSES,
    "filter_partial": CONFLICT_PARTIAL,
    "filter_full": CONFLICT_FULL,
}

_BOUNDARY = (OVERWRITE_NAME, ROOT_FOLDER_NAME)

# Texture suffixes PGPatcher acts on (parallax/complex-material/PBR) - mirrors
# the Tk _PGPATCHER_TEX_SUFFIXES list.
_PGPATCHER_TEX_SUFFIXES = (
    "_p", "_m", "_em", "_envmask",
    "_rmaos", "_cnr", "_s", "_i", "_f",
)


@dataclass
class FilterData:
    """All the per-mod data the filters need. Conflict codes are the raw
    backend CONFLICT_* values. The *_mods sets are mod names. Built off-thread."""
    conflict_codes: dict[str, int] = field(default_factory=dict)        # loose
    bsa_conflict_codes: dict[str, int] = field(default_factory=dict)
    mods_with_plugins: set[str] = field(default_factory=set)
    mods_with_bsa: set[str] = field(default_factory=set)
    mods_with_pbr: set[str] = field(default_factory=set)
    mods_with_updates: set[str] = field(default_factory=set)
    fomod_mods: set[str] = field(default_factory=set)
    bain_mods: set[str] = field(default_factory=set)
    missing_reqs: set[str] = field(default_factory=set)
    ignored_missing_reqs: set[str] = field(default_factory=set)
    disabled_plugins_mods: set[str] = field(default_factory=set)
    notes_mods: set[str] = field(default_factory=set)
    modified_mf_mods: set[str] = field(default_factory=set)
    category_names: dict[str, str] = field(default_factory=dict)        # mod -> cat
    author_names: dict[str, str] = field(default_factory=dict)          # mod -> uploader
    # filetype membership is computed lazily from the index per query.
    filetype_counts: dict[str, int] = field(default_factory=dict)
    mod_filetypes: dict[str, set[str]] = field(default_factory=dict)    # mod -> {ext}


# --------------------------------------------------------------------------
# Separator-block helpers (operate on the entry list)
# --------------------------------------------------------------------------
def _sep_block_range(entries, sep_idx: int) -> range:
    """[sep_idx, end) - the separator plus its non-separator mods until the next
    separator (or end). (No bundle handling - Qt has no bundle separators yet.)"""
    end = sep_idx + 1
    n = len(entries)
    while end < n and not entries[end].is_separator:
        end += 1
    return range(sep_idx, end)


def _sep_block_has(entries, sep_idx: int, mod_pred) -> bool:
    for i in _sep_block_range(entries, sep_idx):
        e = entries[i]
        if not e.is_separator and mod_pred(e):
            return True
    return False


# Search-bar `!token` keywords → how to test a single mod ModEntry. Each value
# is a builder(data) -> (ModEntry -> bool); the builder captures the relevant
# FilterData set/dict once so the per-row predicate is cheap. `data` is never
# None here (callers pass an empty FilterData when it isn't built yet, so the
# sets are simply empty → match-nothing, which is the desired behaviour).
def _tok_conflict(codes: set[int]):
    def build(data):
        cmap, bmap = data.conflict_codes, data.bsa_conflict_codes
        def pred(e):
            return (cmap.get(e.name, CONFLICT_NONE) in codes
                    or bmap.get(e.name, CONFLICT_NONE) in codes)
        return pred
    return build


def _tok_set(attr: str):
    def build(data):
        mods = getattr(data, attr)
        return lambda e: e.name in mods
    return build


def _tok_missing_reqs(data):
    mods = data.missing_reqs - data.ignored_missing_reqs
    return lambda e: e.name in mods


SEARCH_TOKENS = {
    "update":   _tok_set("mods_with_updates"),
    "updates":  _tok_set("mods_with_updates"),
    "winning":  _tok_conflict({CONFLICT_WINS}),
    "losing":   _tok_conflict({CONFLICT_LOSES}),
    "partial":  _tok_conflict({CONFLICT_PARTIAL}),
    "full":     _tok_conflict({CONFLICT_FULL}),
    "fomod":    _tok_set("fomod_mods"),
    "bain":     _tok_set("bain_mods"),
    "missing":  _tok_missing_reqs,
    "notes":    _tok_set("notes_mods"),
    "plugins":  _tok_set("mods_with_plugins"),
    "bsa":      _tok_set("mods_with_bsa"),
    "pbr":      _tok_set("mods_with_pbr"),
    "enabled":  lambda _data: (lambda e: e.enabled),
    "disabled": lambda _data: (lambda e: not e.enabled),
}


def _token_predicate(token: str, data: "FilterData"):
    """Resolve a single `!token` (leading `!` already stripped) to a mod
    predicate. Order: known keyword → filetype (leading dot) → category name →
    match-nothing. Case-insensitive for keywords/categories."""
    t = token.strip()
    if not t:
        return lambda e: True
    low = t.lower()
    builder = SEARCH_TOKENS.get(low)
    if builder is not None:
        return builder(data)
    # Filetype: `.dds` (or a bare ext we normalise to a leading dot).
    if low.startswith("."):
        ext = low
        return lambda e: ext in data.mod_filetypes.get(e.name, ())
    # Category name (case-insensitive equality).
    cats = data.category_names
    return lambda e: (cats.get(e.name, "") or "").lower() == low


def search_hidden_rows(entries, query: str, data: "FilterData | None" = None) -> set[int]:
    """Rows to HIDE for the modlist search box.

    Plain text matches the mod name (case-insensitive substring, Tk parity).
    A whitespace-separated term starting with `!` is a filter token resolved
    against *data* (a live FilterData); `!.dds` = filetype, `!patches` = a
    category, `!update`/`!winning`/… = status keywords. All tokens AND with each
    other and with the text needle. Unknown or unresolvable tokens (or any
    data-backed token when *data* is unbuilt) match nothing. A separator is
    shown when any mod in its block satisfies every predicate. Empty query
    hides nothing."""
    raw = query or ""
    tokens: list[str] = []
    words: list[str] = []
    for term in raw.split():
        if term.startswith("!") and len(term) > 1:
            tokens.append(term[1:])
        else:
            words.append(term)
    needle = " ".join(words).strip().lower()
    if not tokens and not needle:
        return set()

    if data is None:
        data = FilterData()

    preds = [_token_predicate(tok, data) for tok in tokens]
    if needle:
        preds.append(lambda e, _n=needle: _n in e.display_name.lower())

    def _match(e) -> bool:
        return all(p(e) for p in preds)

    hide: set[int] = set()
    for i, e in enumerate(entries):
        if e.is_separator:
            if not _sep_block_has(entries, i, _match):
                hide.add(i)
        elif not _match(e):
            hide.add(i)
    return hide


def conflict_filter_hidden_rows(entries, names) -> set[int]:
    """Rows to HIDE for the right-click "Filter Conflicts" action: keep only the
    mods in *names* (the anchor mod plus everything that conflicts with it).

    Boundary rows (Overwrite / Root Folder) are treated as mods, not separators:
    Overwrite takes part in conflicts and shows up when it's in the set, while
    Root Folder (no conflict data of its own) always hides. A real separator
    survives only while one of its mods does, so the list keeps its structure
    without leaving empty headers behind. Empty *names* hides nothing."""
    keep = set(names or ())
    if not keep:
        return set()
    hide: set[int] = set()
    for i, e in enumerate(entries):
        if e.is_separator and e.name not in _BOUNDARY:
            if not _sep_block_has(entries, i, lambda m: m.name in keep):
                hide.add(i)
        elif e.name not in keep:
            hide.add(i)
    return hide


def plugin_search_hidden_rows(rows, query: str, owner: dict | None = None) -> set[int]:
    """Rows to HIDE for the plugins search box (Tk parity): a plugin is shown
    when its name OR its owning mod name contains *query* (case-insensitive).
    `owner` maps plugin filename (lower) → mod name. Empty query hides nothing."""
    q = (query or "").strip().casefold()
    if not q:
        return set()
    owner = owner or {}
    hide: set[int] = set()
    for i, r in enumerate(rows):
        name_lower = r.name.lower()
        if q in name_lower:
            continue
        mod = owner.get(name_lower, "")
        if mod and q in mod.casefold():
            continue
        hide.add(i)
    return hide


# Plugin filter keys → display labels (the plugins-tab Filters side panel).
PLUGIN_STATUS_FILTERS = [
    ("filter_enabled",        "Enabled plugins"),
    ("filter_disabled",       "Disabled plugins"),
    ("filter_esl_flagged",    "ESL-flagged (light)"),
    ("filter_esl_not_flagged","Not ESL-flagged"),
    ("filter_ext_esl",        "Extension .esl"),
    ("filter_ext_esm",        "Extension .esm"),
    ("filter_ext_esp",        "Extension .esp"),
    ("filter_master",         "Master (loads first)"),
    ("filter_missing",        "Missing masters"),
    ("filter_dirty",          "Dirty (needs cleaning)"),
    ("filter_userlist",       "Managed by userlist"),
    ("filter_bash_tags",      "Bash-tagged"),
    ("filter_esl_safe",       "ESL-safe (eligible for ESL flag)"),
    ("filter_esl_unsafe",     "ESL-unsafe (too many records)"),
    ("filter_bos_sp",         "BOS/SP-patched"),
    ("filter_bos_only",       "BOS-patched"),
    ("filter_sp_only",        "SkyPatcher-patched"),
]


def plugin_filter_hidden_rows(rows, state: dict, disabled_mf=None) -> set[int]:
    """Rows to HIDE for the plugins-tab filter side panel.

    `rows` is the PluginModel row list (each has .name/.enabled/.flags/.vanilla);
    `state` maps each filter_* key → tri-state int (0 off / 1 include / 2 exclude).
    `disabled_mf` (optional) is the set of plugin filenames (lowercase basename)
    disabled via the Mod Files tab - a plugin in this set counts as "disabled"
    even if plugins.txt still enables it (the two sources are unioned).
    Include narrows (keep only rows where the predicate holds); exclude drops
    (hide rows where it holds). AND across includes, OR across excludes - same
    convention as compute_hidden_rows. No active keys → hide nothing.
    """
    from gui_qt.plugin_state import (
        PF_ESL, PF_MISSING, PF_DIRTY, PF_TAGS, PF_USERLIST,
        PF_ESL_SAFE, PF_ESL_UNSAFE)

    disabled_mf = disabled_mf or set()

    def _disabled_in_mf(r):
        return r.name.lower() in disabled_mf

    def _has(key):
        def pred(r):
            if key == "filter_enabled":
                return (r.enabled or r.vanilla) and not _disabled_in_mf(r)
            if key == "filter_disabled":
                return (not r.enabled and not r.vanilla) or _disabled_in_mf(r)
            if key == "filter_esl_flagged":
                return bool(r.flags & PF_ESL)
            if key == "filter_esl_not_flagged":
                return not (r.flags & PF_ESL)
            if key == "filter_ext_esl":
                return r.name.lower().endswith(".esl")
            if key == "filter_ext_esm":
                return r.name.lower().endswith(".esm")
            if key == "filter_ext_esp":
                return r.name.lower().endswith(".esp")
            if key == "filter_master":
                # Master block - unlike the literal "Extension ." keys above,
                # this also catches a master-flagged .esp.
                from gui_qt.plugin_state import is_master_group
                return is_master_group(r)
            if key == "filter_missing":
                return bool(r.flags & PF_MISSING)
            if key == "filter_dirty":
                return bool(r.flags & PF_DIRTY)
            if key == "filter_userlist":
                return bool(r.flags & PF_USERLIST)
            if key == "filter_bash_tags":
                return bool(r.flags & PF_TAGS)
            if key == "filter_esl_safe":
                return bool(r.flags & PF_ESL_SAFE)
            if key == "filter_esl_unsafe":
                return bool(r.flags & PF_ESL_UNSAFE)
            if key == "filter_bos_sp":
                return bool(getattr(r, "bos_sp", ""))
            if key == "filter_bos_only":
                return getattr(r, "bos_sp", "") in ("bos", "both")
            if key == "filter_sp_only":
                return getattr(r, "bos_sp", "") in ("sp", "both")
            return True
        return pred

    # filter_bos_only + filter_sp_only get bespoke handling (a union when BOTH
    # are INCLUDE-checked) mirroring Tk; every other key is a plain tri-state.
    _BOS_SP_KEYS = {"filter_bos_only", "filter_sp_only"}
    includes = [k for k, _l in PLUGIN_STATUS_FILTERS
                if state.get(k) == 1 and k not in _BOS_SP_KEYS]
    excludes = [k for k, _l in PLUGIN_STATUS_FILTERS
                if state.get(k) == 2 and k not in _BOS_SP_KEYS]

    keep = list(range(len(rows)))
    for k in includes:
        pred = _has(k)
        keep = [i for i in keep if pred(rows[i])]
    for k in excludes:
        pred = _has(k)
        keep = [i for i in keep if not pred(rows[i])]

    # BOS/SP union case: both INCLUDE-checked keeps any patched plugin;
    # otherwise each acts as an independent tri-state (include/exclude).
    bos_state = state.get("filter_bos_only") or 0
    sp_state = state.get("filter_sp_only") or 0
    if bos_state or sp_state:
        bos_pred = _has("filter_bos_only")
        sp_pred = _has("filter_sp_only")
        if bos_state == 1 and sp_state == 1:
            keep = [i for i in keep
                    if bool(getattr(rows[i], "bos_sp", ""))]
        else:
            if bos_state == 1:
                keep = [i for i in keep if bos_pred(rows[i])]
            elif bos_state == 2:
                keep = [i for i in keep if not bos_pred(rows[i])]
            if sp_state == 1:
                keep = [i for i in keep if sp_pred(rows[i])]
            elif sp_state == 2:
                keep = [i for i in keep if not sp_pred(rows[i])]

    if not includes and not excludes and not bos_state and not sp_state:
        return set()

    kept = set(keep)
    return {i for i in range(len(rows)) if i not in kept}


def _apply_include(entries, keep: list[int], mod_pred, sep_pred) -> list[int]:
    """Keep mods passing mod_pred + separators whose block satisfies sep_pred."""
    out = []
    for i in keep:
        e = entries[i]
        if e.is_separator:
            if sep_pred(i):
                out.append(i)
        elif mod_pred(e):
            out.append(i)
    return out


def _apply_exclude(entries, keep: list[int], mod_pred) -> list[int]:
    """Drop mods matching mod_pred; separators always retained."""
    out = []
    for i in keep:
        e = entries[i]
        if e.is_separator or not mod_pred(e):
            out.append(i)
    return out


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def compute_hidden_rows(entries, state: dict, data: FilterData) -> set[int]:
    """Return the set of row indices to HIDE for the given filter state.

    `entries` is the model's full ModEntry list (incl. boundary separators).
    `state` is the side-panel state dict. `data` is the per-mod FilterData.
    No active filters → empty set (hide nothing)."""
    if not _any_active(state):
        return set()

    keep = list(range(len(entries)))

    # Step: hide separators (keep Overwrite / Root Folder boundaries).
    if state.get("filter_hide_separators") == 1:
        keep = [i for i in keep
                if not entries[i].is_separator
                or entries[i].name in _BOUNDARY]

    # Step: enabled / disabled (tri-state per side, independent).
    sd = state.get("filter_show_disabled", 0)
    if sd == 1:
        keep = _apply_include(
            entries, keep, lambda e: not e.enabled,
            lambda i: _sep_block_has(entries, i, lambda e: not e.enabled))
    elif sd == 2:
        keep = _apply_exclude(entries, keep, lambda e: not e.enabled)
    se = state.get("filter_show_enabled", 0)
    if se == 1:
        keep = _apply_include(
            entries, keep, lambda e: e.enabled,
            lambda i: _sep_block_has(entries, i, lambda e: e.enabled))
    elif se == 2:
        keep = _apply_exclude(entries, keep, lambda e: e.enabled)

    # Step: conflict-type (loose + BSA), include + exclude sets separate.
    include_c: set = set()
    exclude_c: set = set()
    for key, code in _CONFLICT_KEYS.items():
        v = state.get(key, 0)
        if v == 1:
            include_c.add(code)
        elif v == 2:
            exclude_c.add(code)
    cmap, bmap = data.conflict_codes, data.bsa_conflict_codes
    if include_c:
        def _c_in(e):
            return (cmap.get(e.name, CONFLICT_NONE) in include_c
                    or bmap.get(e.name, CONFLICT_NONE) in include_c)
        keep = _apply_include(
            entries, keep, _c_in,
            lambda i: _sep_block_has(entries, i, _c_in))
    if exclude_c:
        def _c_ex(e):
            return (cmap.get(e.name, CONFLICT_NONE) in exclude_c
                    or bmap.get(e.name, CONFLICT_NONE) in exclude_c)
        keep = _apply_exclude(entries, keep, _c_ex)

    # Step: simple flag-driven membership filters.
    keep = _apply_simple(entries, keep, state, data)

    # Step: category include/exclude.
    cats = data.category_names
    inc_cat = state.get("categories") or frozenset()
    exc_cat = state.get("categories_exclude") or frozenset()
    if inc_cat:
        def _cat_in(e):
            return (cats.get(e.name, "") or "") in inc_cat
        keep = _apply_include(
            entries, keep, _cat_in,
            lambda i: _sep_block_has(entries, i, _cat_in))
    if exc_cat:
        def _cat_ex(e):
            return (cats.get(e.name, "") or "") in exc_cat
        keep = _apply_exclude(entries, keep, _cat_ex)

    # Step: author (Nexus uploader) include/exclude.
    auths = data.author_names
    inc_auth = state.get("authors") or frozenset()
    exc_auth = state.get("authors_exclude") or frozenset()
    if inc_auth:
        def _auth_in(e):
            return (auths.get(e.name, "") or "") in inc_auth
        keep = _apply_include(
            entries, keep, _auth_in,
            lambda i: _sep_block_has(entries, i, _auth_in))
    if exc_auth:
        def _auth_ex(e):
            return (auths.get(e.name, "") or "") in exc_auth
        keep = _apply_exclude(entries, keep, _auth_ex)

    # Step: filetype include/exclude.
    inc_ft = state.get("filetypes") or frozenset()
    exc_ft = state.get("filetypes_exclude") or frozenset()
    if inc_ft:
        ft_mods = _mods_with_filetypes(data, inc_ft)
        keep = _apply_include(
            entries, keep, lambda e: e.name in ft_mods,
            lambda i: _sep_block_has(entries, i, lambda e: e.name in ft_mods))
    if exc_ft:
        ft_ex = _mods_with_filetypes(data, exc_ft)
        keep = _apply_exclude(entries, keep, lambda e: e.name in ft_ex)

    visible = set(keep)
    return {i for i in range(len(entries)) if i not in visible}


# (attr_key, mods_set_attr_on_data) - the tri-state membership filters.
_SIMPLE_SPECS = (
    ("filter_missing_reqs", None),          # special: subtract ignored
    ("filter_has_disabled_plugins", "disabled_plugins_mods"),
    ("filter_has_notes", "notes_mods"),
    ("filter_has_plugins", "mods_with_plugins"),
    ("filter_has_disabled_files", "modified_mf_mods"),
    ("filter_has_updates", "mods_with_updates"),
    ("filter_fomod_only", "fomod_mods"),
    ("filter_bain_only", "bain_mods"),
    ("filter_has_bsa", "mods_with_bsa"),
    ("filter_has_pbr", "mods_with_pbr"),
)


def _apply_simple(entries, keep, state, data) -> list[int]:
    for key, attr in _SIMPLE_SPECS:
        v = state.get(key, 0)
        if not v:
            continue
        if key == "filter_missing_reqs":
            mods = data.missing_reqs - data.ignored_missing_reqs
        else:
            mods = getattr(data, attr)
        if v == 1 and not mods:
            return []      # include with no matches → show nothing
        mod_pred = (lambda e, _m=mods: e.name in _m)
        if v == 1:
            keep = _apply_include(
                entries, keep, mod_pred,
                lambda i, _m=mods: _sep_block_has(
                    entries, i, lambda e: e.name in _m))
        else:
            keep = _apply_exclude(entries, keep, mod_pred)
    return keep


def _mods_with_filetypes(data: FilterData, exts: frozenset) -> set[str]:
    return {mod for mod, mexts in data.mod_filetypes.items()
            if mexts & exts}


def _any_active(state: dict) -> bool:
    for key, _label in STATUS_FILTERS:
        if state.get(key):
            return True
    if state.get("categories") or state.get("categories_exclude"):
        return True
    if state.get("authors") or state.get("authors_exclude"):
        return True
    if state.get("filetypes") or state.get("filetypes_exclude"):
        return True
    return False


# --------------------------------------------------------------------------
# Data builders (read the persisted backend indexes - same sources as Tk)
# --------------------------------------------------------------------------
def _read_mod_index(staging_parent: Path) -> dict:
    """{mod: (normal, root)} from modindex.bin, or {}."""
    idx = staging_parent / "modindex.bin"
    if not idx.is_file():
        return {}
    try:
        from Utils.filemap import read_mod_index
        return read_mod_index(idx) or {}
    except Exception:
        return {}


def build_index_data(staging_parent: Path) -> tuple[dict, dict, set]:
    """From modindex.bin build (filetype_counts, mod_filetypes, mods_with_pbr).
    One pass over the index so the panel is cheap to populate."""
    counts: dict[str, int] = {}
    mod_ft: dict[str, set[str]] = {}
    pbr: set[str] = set()
    for mod, (normal, root) in _read_mod_index(staging_parent).items():
        exts: set[str] = set()
        has_pbr = False
        for rel_key in (*normal, *root):
            ext = splitext(rel_key)[1]
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
                exts.add(ext)
            if not has_pbr and rel_key.endswith(".dds"):
                stem = rel_key[:-4]
                if any(stem.endswith(suf) for suf in _PGPATCHER_TEX_SUFFIXES):
                    has_pbr = True
        if exts:
            mod_ft[mod] = exts
        if has_pbr:
            pbr.add(mod)
    return counts, mod_ft, pbr


def build_mods_with_bsa(staging_parent: Path) -> set[str]:
    """Mods that contain at least one BSA/BA2 with files (from bsa_index.bin)."""
    idx = staging_parent / "bsa_index.bin"
    if not idx.is_file():
        return set()
    try:
        from Utils.bsa_filemap import read_bsa_index
        index = read_bsa_index(idx) or {}
    except Exception:
        return set()
    return {name for name, archives in index.items()
            if any(paths for _bsa, _mt, paths in archives)}


def build_mods_with_plugins(staging_parent: Path, plugin_exts) -> set[str]:
    """Mods that win at least one plugin, from filemap.txt."""
    fm = staging_parent / "filemap.txt"
    if not fm.is_file():
        return set()
    exts = tuple(e.lower() for e in (plugin_exts or ()))
    if not exts:
        exts = (".esp", ".esm", ".esl")
    out: set[str] = set()
    try:
        # surrogateescape: filemap.txt carries filesystem-derived relative
        # paths whose non-UTF-8 bytes decode to surrogate code points - a plain
        # utf-8 read raises UnicodeDecodeError/EncodeError on them.
        for line in fm.read_text(encoding="utf-8",
                                 errors="surrogateescape").splitlines():
            if "\t" not in line:
                continue
            rel_key, mod = line.split("\t", 1)
            if rel_key.rsplit("/", 1)[-1].lower().endswith(exts):
                out.add(mod)
    except Exception:
        return set()
    return out
