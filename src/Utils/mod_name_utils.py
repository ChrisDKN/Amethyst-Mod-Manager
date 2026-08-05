"""
Mod name parsing: strip title metadata and suggest display names from filename stems.
Used by install_mod, dialogs (NameModDialog), and modlist_panel. No dependency on other gui modules.
"""

import re
import unicodedata


# Characters Windows/Wine forbid in a path component.  Mods are deployed and
# read through Wine tools (xEdit, PGPatcher, BodySlide, …) and into Wine
# prefixes, so a folder name Wine can't address breaks those tools — and a
# trailing dot or space is silently stripped by Windows path normalisation,
# which makes the folder vanish from the tool's point of view.
_WINDOWS_RESERVED_CHARS = r'<>:"/\\|?*'

# Zero-width / invisible format characters that are never a legitimate part of a
# mod name but silently break exact-match lookups (the folder name carries an
# invisible byte the modlist entry does not). File managers and copy-paste can
# introduce these. Stripped so a name that LOOKS clean also IS clean. NB: the
# regular no-break space (U+00A0) is handled by str.strip()/rstrip below (Python
# treats it as whitespace), so it is not listed here.
_INVISIBLE_CHARS = (
    "\u200b"  # ZERO WIDTH SPACE
    "\u200c"  # ZERO WIDTH NON-JOINER
    "\u200d"  # ZERO WIDTH JOINER
    "\u2060"  # WORD JOINER
    "\ufeff"  # ZERO WIDTH NO-BREAK SPACE / BOM
)
_INVISIBLE_RE = re.compile(f"[{_INVISIBLE_CHARS}]")


def sanitize_mod_folder_name(name: str) -> str:
    """Return *name* made safe for use as a Wine/Windows-addressable folder.

    - Normalises to Unicode NFC so a decomposed name (e.g. "e" + combining
      acute) and its composed form ("é") produce the SAME bytes. File managers
      differ on which form they write; without this the folder name and the
      modlist entry can be byte-different for the same visible name, and the
      mod silently drops out of filemap.txt (no conflicts / plugins / Data-tab).
    - Removes zero-width / invisible format characters (see _INVISIBLE_CHARS).
    - Strips characters Windows/Wine forbid in a path component.
    - Removes control characters.
    - Trims trailing dots and spaces (incl. the no-break space, which
      str.strip() treats as whitespace) — Windows path normalisation drops
      these, so "Foo." / "Foo " / "Foo " become unreachable to Wine tools.
    - Falls back to "Mod" if nothing usable remains.

    Visible non-ASCII characters (accented letters, Cyrillic, CJK, …) are
    preserved — only ambiguous/invisible bytes are normalised away. This only
    affects the on-disk folder name; the user's chosen display name is
    unaffected elsewhere.
    """
    # NFC first so combining sequences fold before any per-char handling.
    s = unicodedata.normalize("NFC", name)
    s = _INVISIBLE_RE.sub("", s)
    s = s.strip()
    # Drop reserved characters and ASCII control chars.
    s = re.sub(rf"[{re.escape(_WINDOWS_RESERVED_CHARS)}]", "", s)
    s = "".join(ch for ch in s if ord(ch) >= 32)
    # Windows strips trailing dots and spaces from each path component.
    s = s.rstrip(". ")
    # Reserved DOS device names (CON, PRN, NUL, COM1…) — extremely rare for a
    # mod name, but a folder so named is unusable under Wine.
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", s):
        s = s + "_"
    return s if s else "Mod"


# New Nexus download format (rolled out 2026-06-11):
#   "<mod name>_<version>_<slug>"      e.g. "FDE_Senna_1.0.1_xDLPwGTYs"
#                                           "Canidae_-_A_Wolf_Replacer_1.3_587RqT2ex"
#                                           "Skyrim_Sewers_PL_1_FpnkHZi8m"  (version "1")
# Spaces in the title are encoded as underscores; <version> is a number that may
# be a plain integer ("1", "79"), dotted ("2.0.2"), or carry a trailing tag
# ("2.0.2BETA"); <slug> is a random base62 token Nexus appends per upload.
# Nexus has signalled they intend to drop the slug in a later change, leaving:
#   "<mod name>_<version>"             e.g. "FDE_Senna_1.0.1"
#
# Two anchors, because the version alone is too weak a signal:
#   * _WITH_SLUG: the trailing token IS a random slug (see ``_slug_like``), so we
#     trust the segment before it as the version and accept even a bare integer.
#     This is the live format and matches every real download.
#   * _NO_SLUG (future slug-less format): no random token to anchor on, so the
#     trailing "_<version>" is taken as the version even when it is a bare
#     integer ("Domain_Expansion_Release_V1_1" -> "Domain Expansion Release V1").
#     This can occasionally eat a real title number, so the un-stripped
#     ``filename_stem`` is still offered as a fallback candidate in the rename
#     dialog (see ``_suggest_mod_names``).
_NEXUS_SLUG = r"[A-Za-z0-9]{6,16}"
_NEW_NEXUS_WITH_SLUG_RE = re.compile(
    rf"^(?P<name>.+?)_(?P<ver>\d+(?:\.\d+)*[A-Za-z]*)_(?P<slug>{_NEXUS_SLUG})$"
)
_NEW_NEXUS_NO_SLUG_RE = re.compile(
    r"^(?P<name>.+?)_(?P<ver>\d+(?:\.\d+)*[A-Za-z]*)$"
)

# Later Nexus download format (rolled out 2026-07):
#   "<mod name> <mod id> <version> <slug>"
#     e.g. "Shattered Royal Armor 183637 1.4 L5WQbqNIa"
#          "Skyrim Sewers 12345 1 FpnkHZi8m"          (version "1")
_NEW_NEXUS_SPACED_RE = re.compile(
    rf"^(?P<name>.+?) (?P<id>\d+) (?P<ver>\d+(?:\.\d+)*[A-Za-z]*) (?P<slug>{_NEXUS_SLUG})$"
)

# Still-later Nexus download format (rolled out 2026-07, 4th format change):
#   "<mod name> <mod id> <version> <timestamp> <slug>"
#     e.g. "Fuse00 Legionary Armor 184575 1 2026-07-06T18-32Z Ae46W7hI2"
# Same as the spaced form but with an ISO-ish upload timestamp
# (YYYY-MM-DDThh-mmZ) inserted between the version and the slug.
_NEXUS_TIMESTAMP = r"\d{4}-\d{2}-\d{2}T[\dNRZ:-]+Z"
_NEW_NEXUS_SPACED_TS_RE = re.compile(
    rf"^(?P<name>.+?) (?P<id>\d+) (?P<ver>\d+(?:\.\d+)*[A-Za-z]*) "
    rf"(?P<ts>{_NEXUS_TIMESTAMP}) (?P<slug>{_NEXUS_SLUG})$"
)

# mod.io download names append a truncated-UUID tail: an underscore, the first
# two hex groups of the mod's UUID (``<8hex>-<4hex>``), then one or more short
# trailing groups (1-4 chars each) ending in a random token — e.g.
# ``bettercontainers_cb42bc3a-f1d2-afwl``, ``wingsunlocked_3d7eabb4-81bf-d9-bwww``,
# ``weightlessgold_81117bd5-de2f-a-du9y`` (note the 1-char ``a`` group).  The
# ``_<8hex>-<4hex>`` anchor is distinct from any Nexus tail, so matching is
# unambiguous and never touches Nexus names.
_MODIO_TAIL_RE = re.compile(r"_[0-9a-f]{8}-[0-9a-f]{4}(?:-[0-9a-z]{1,4})+$")


# ---------------------------------------------------------------------------
# Default (built-in) install-name rules, exposed as editable regex rows.
#
# These mirror the known download-name formats above as plain search/replace
# regex so a user can SEE and tweak what runs — the editor seeds them into the
# config on first use and offers a per-row / global "restore default". They run
# through the same _apply_custom_patterns path as user rules (as Step 0 of
# _suggest_mod_names). The heuristic Python parsers below (_strip_nexus_new_format
# etc.) still run afterwards as a fallback safety net, so a rule a user disables
# or mangles won't wholly break name cleaning.
#
# Each default is {id, label, search, replace, enabled}. `id` is stable so the
# editor can match a saved row back to its default for the reset action.
DEFAULT_INSTALL_NAME_RULES: list[dict] = [
    {
        "id": "nexus_spaced_timestamp",
        "label": "Nexus: name id version timestamp slug",
        "search": r"^(.+?) \d+ \d+(?:\.\d+)*[A-Za-z]* "
                  r"\d{4}-\d{2}-\d{2}T[\dNRZ:-]+Z [A-Za-z0-9]{6,16}$",
        "replace": r"\1",
        "enabled": True,
    },
    {
        "id": "nexus_spaced",
        "label": "Nexus: name id version slug",
        "search": r"^(.+?) \d+ \d+(?:\.\d+)*[A-Za-z]* [A-Za-z0-9]{6,16}$",
        "replace": r"\1",
        "enabled": True,
    },
    {
        "id": "nexus_legacy_dash_tail",
        "label": "Nexus (legacy): strip -id-version-timestamp tail",
        "search": r"(?:-\d+[A-Za-z]*){2,}$",
        "replace": r"",
        "enabled": True,
    },
    {
        "id": "modio_uuid_tail",
        "label": "mod.io: strip UUID tail",
        "search": r"_[0-9a-f]{8}-[0-9a-f]{4}(?:-[0-9a-z]{1,4})+$",
        "replace": r"",
        "enabled": True,
    },
]


def default_install_name_rules() -> list[dict]:
    """Return a fresh copy of the built-in default rules (id/label/search/
    replace/enabled). Used to seed a new config and to power the editor's
    reset-to-default action."""
    return [dict(r) for r in DEFAULT_INSTALL_NAME_RULES]


# ids of the built-in defaults whose FORMAT is ALSO handled by a heuristic
# Python parser below (_strip_nexus_new_format / the mod.io tail strip). The
# editor's rules are authoritative: when the user disables (or deletes) the rule
# for one of these, the matching Python parser step is skipped too — otherwise
# the internal parser would silently shadow the disabled rule and disabling
# would appear to do nothing. Formats WITHOUT a duplicate parser (e.g. the
# underscore ``<name>_<ver>_<slug>`` form, legacy dash tails) always run.
_GATED_DEFAULT_IDS = {
    "nexus_spaced_timestamp", "nexus_spaced",
    "nexus_legacy_dash_tail", "modio_uuid_tail",
}


def _disabled_gated_ids() -> set[str]:
    """Return the set of gated default-rule ids that the user has disabled or
    removed. A gated Python parser step is skipped for any id in this set so the
    editor's rules stay authoritative. On any error, returns an empty set (parse
    everything — the safe default)."""
    try:
        from Utils.ui_config import load_install_name_patterns
        rules = load_install_name_patterns()
    except Exception:
        return set()
    present_enabled = {
        str(r.get("id"))
        for r in rules
        if r.get("id") and r.get("enabled", True)
    }
    # Disabled = a gated default that is NOT present-and-enabled (covers both an
    # explicitly-disabled row and a removed one).
    return {rid for rid in _GATED_DEFAULT_IDS if rid not in present_enabled}


def _apply_custom_patterns(stem: str) -> str | None:
    """Apply the user's custom install-name search/replace rules to *stem*.

    Rules come from Settings ▸ (install name patterns) via
    ``ui_config.load_install_name_patterns`` and are applied in order — each
    enabled rule runs ``re.sub(search, replace, stem)``. Nexus keeps changing
    its download-name format, so this lets a user adapt without a code change.

    Returns the transformed name when at least one enabled rule actually changed
    the stem, else ``None`` so the caller falls through to the built-in parsing.
    A rule with an invalid regex is skipped (never raises into the installer).
    """
    try:
        from Utils.ui_config import load_install_name_patterns
        rules = load_install_name_patterns()
    except Exception:
        return None
    if not rules:
        return None
    result = stem
    changed = False
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        search = rule.get("search", "")
        if not search:
            continue
        try:
            new = re.sub(search, rule.get("replace", ""), result)
        except re.error:
            continue
        if new != result:
            result = new
            changed = True
    result = result.strip()
    if changed and result:
        return result
    return None


def _slug_like(token: str) -> bool:
    """True if *token* resembles a random Nexus upload slug (base62 noise)
    rather than a meaningful word an uploader might append after a version."""
    if not (6 <= len(token) <= 16):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    return (has_digit and has_alpha) or (has_upper and has_lower)


def _strip_nexus_new_format(stem: str, disabled_ids: set[str] | None = None) -> str | None:
    """If *stem* matches the post-2026-06-11 Nexus download format
    ``<mod name>_<version>[_<slug>]``, return the decoded mod name (underscores
    → spaces).  Returns ``None`` when *stem* does not match, so callers can fall
    back to the legacy parsing untouched.

    *disabled_ids* names built-in default rules the user has turned off in the
    editor; the matching (duplicated) parser step is skipped so the editor's
    rules stay authoritative. The underscore forms below are NOT duplicated by an
    editor rule, so they always run.
    """
    disabled_ids = disabled_ids or set()

    # Newest spaced form with an upload timestamp
    # "<name> <id> <version> <timestamp> <slug>": try this before the timestamp-
    # less spaced form so the timestamp isn't mistaken for the version/slug.
    if "nexus_spaced_timestamp" not in disabled_ids:
        m = _NEW_NEXUS_SPACED_TS_RE.match(stem)
        if m and _slug_like(m.group("slug")):
            name = m.group("name").strip()
            return name or None

    # Spaced form "<name> <id> <version> <slug>": anchored on the trailing
    # slug plus the numeric mod-id, so it is unambiguous and never touches the
    # underscore or legacy dash formats (different separators).
    if "nexus_spaced" not in disabled_ids:
        m = _NEW_NEXUS_SPACED_RE.match(stem)
        if m and _slug_like(m.group("slug")):
            name = m.group("name").strip()
            return name or None

    # Prefer the slug-anchored form: when the trailing token is a random Nexus
    # slug we can trust the segment before it as the version even if it is a
    # bare integer ("..._1_<slug>").
    m = _NEW_NEXUS_WITH_SLUG_RE.match(stem)
    if m and _slug_like(m.group("slug")):
        name = m.group("name").replace("_", " ").strip()
        return name or None

    # Future slug-less form ("<name>_<version>"): no random token to anchor on,
    # so the trailing "_<version>" is stripped even when it is a bare integer.
    # This may occasionally remove a real title number; the caller still offers
    # the un-stripped stem as a fallback candidate.
    m = _NEW_NEXUS_NO_SLUG_RE.match(stem)
    if m:
        name = m.group("name").replace("_", " ").strip()
        return name or None

    return None


def _strip_title_metadata(name: str) -> str:
    """
    Remove common metadata from a mod name: parenthesized/bracketed tags,
    version strings, underscores-as-spaces, Nexus remnant suffixes, and
    trailing noise.

    Examples:
        "SkyUI_5_2_SE"                    → "SkyUI"
        "All in one (all game versions)"  → "All in one"
        "Cool Mod (SE) v1.2.3"           → "Cool Mod"
        "My_Awesome_Mod_v2_0"            → "My Awesome Mod"
    """
    s = name

    # Strip residual Nexus-style suffix still containing alphanumeric version
    # parts (e.g. -12604-5-2SE that the strict numeric strip missed).
    s = re.sub(r"-\d{2,}(?:-[\w]+)*$", "", s)

    # Replace underscores with spaces (common in Nexus filenames)
    s = s.replace("_", " ")

    # Remove content in parentheses and square brackets (e.g. "(SE)", "[1.0]")
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"\s*\[[^\]]*\]", "", s)

    # Remove trailing version-like patterns:  v1.2.3, V2.0, etc.
    s = re.sub(r"\s+[vV]\d+(?:[.\-]\w+)*\s*$", "", s)
    # Remove trailing dotted version:  1.0.0, 2.3.1
    s = re.sub(r"\s+\d+(?:\.\d+)+\s*$", "", s)

    # Remove trailing segments that are numeric or known edition/platform tags
    _EDITION_TAGS = r"(?:SE|AE|LE|VR|SSE|GOTY|HD|UHD)"
    s = re.sub(rf"(\s+(?:\d[\w]*|{_EDITION_TAGS})){{2,}}\s*$", "", s)
    s = re.sub(rf"\s+{_EDITION_TAGS}\s*$", "", s)
    s = re.sub(r"(?<=\d)\s+\d+\s*$", "", s)

    # Second pass for version patterns uncovered after stripping above
    s = re.sub(r"\s+[vV]\d+(?:[.\-]\w+)*\s*$", "", s)

    # Clean up any leftover dashes or whitespace at the edges
    s = re.sub(r"[\s\-]+$", "", s)
    s = re.sub(r"^[\s\-]+", "", s)

    return s if s else name


def _suggest_mod_names(filename_stem: str) -> list[str]:
    """
    Given a raw filename stem (no extension), return a list of name candidates
    for the install/rename dialog, **best (default) first**.

    Nexus Mods download names follow ``ModName-nexusid-version-timestamp``.
    The only suffix we strip for the *default* name is that Nexus tail — the
    title itself (including any parentheses, version, or descriptive tags the
    uploader chose) is preserved.  This mirrors Mod Organizer 2, whose
    name-guess regex treats ``( ) . -`` and spaces as legitimate mod-name
    characters and removes only the trailing id/version.

    The aggressively-cleaned name (parens/version/edition tags removed) is still
    offered as a *lower-priority* candidate so the rename dialog can suggest it,
    but it is no longer the default — too many real titles carry meaningful
    parentheses (Stardew framework tags "(CP)"/"(AT)", disambiguators like
    "(Black)" vs "(Silver)", etc.) that the old default silently destroyed.
    """
    # Step 0: user-defined custom rules (Settings) win over every built-in
    # parser — this is the escape hatch for a new Nexus download-name format we
    # haven't shipped a built-in for yet. When a rule matches, the transformed
    # name is the default candidate; the built-in suggestions are still appended
    # below as fallbacks in the rename dialog.
    custom_names: list[str] = []
    custom = _apply_custom_patterns(filename_stem)
    if custom:
        custom_names.append(custom)

    # Which built-in default rules has the user turned off? The matching
    # (duplicated) parser steps below are skipped so the editor's rules stay
    # authoritative — disabling a default rule visibly changes the result.
    disabled = _disabled_gated_ids()

    # Step 1: strip duplicate-download suffix added by browsers/OS (e.g. " (1)", " (2)")
    stem = re.sub(r"\s*\(\d+\)\s*$", "", filename_stem).strip()

    # Step 1b: strip the mod.io UUID-fragment tail (distinct from any Nexus
    # tail).  The cleaned name is the default; the raw stem stays as a fallback.
    # Gated on the mod.io default rule being enabled.
    modio_clean = stem if "modio_uuid_tail" in disabled else _MODIO_TAIL_RE.sub("", stem)
    if modio_clean != stem:
        seen = set()
        result = []
        for candidate in (*custom_names, modio_clean, filename_stem):
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    # Step 2: handle the post-2026-06-11 Nexus underscore format
    #   "<mod name>_<version>[_<slug>]".  When it matches, the decoded name is
    # the least-destructive (default) candidate.  Old-format downloads don't
    # match and fall through to the legacy dash-tail handling below unchanged.
    new_format = _strip_nexus_new_format(stem, disabled)
    if new_format:
        # Skip the dash-tail strip: the underscore format has no Nexus dash tail,
        # and the decoded name may legitimately contain dashes ("A - B").
        nexus_clean = new_format
        title_clean = _strip_title_metadata(nexus_clean)
        # The version was stripped to form the default.  Offer the
        # number-preserved name (underscores decoded) as a fallback so a real
        # title number eaten by the slug-less integer strip can be recovered.
        kept_number = stem.replace("_", " ").strip()
        seen = set()
        result = []
        for candidate in (*custom_names, nexus_clean, title_clean, kept_number, filename_stem):
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    # Step 3: strip the legacy Nexus tail (-nexusid-version-timestamp).  Each segment is
    # a dash followed by digits and optional trailing letters (e.g. "-4a", "-2SE"
    # that Nexus appends for versioned uploads).  We require at least two such
    # segments so a single "-2" inside a real title (e.g. "Mod-2") is left alone.
    # Gated on the legacy-tail default rule being enabled (editor is
    # authoritative): when the user disables that rule, BOTH the primary strip
    # and the single-segment fallback below are skipped, since they are two
    # facets of the same "strip legacy Nexus dash tail" behaviour the rule
    # represents. Otherwise disabling the rule would appear to do nothing.
    if "nexus_legacy_dash_tail" in disabled:
        nexus_clean = stem
    else:
        nexus_clean = re.sub(r"(?:-\d+[A-Za-z]*){2,}$", "", stem).strip()
        if nexus_clean == stem:
            # Fall back to the looser numeric-only strip for names with just one
            # trailing -digits segment (rare, but keep prior behaviour for those).
            nexus_clean = re.sub(r"(-\d+)+$", "", stem).strip()

    # Aggressively-cleaned variant: strip parens/brackets/version/edition tags.
    # Offered as a fallback candidate only — NOT the default (see docstring).
    title_clean = _strip_title_metadata(nexus_clean)

    # Build de-duplicated list, default (least-destructive) first.
    seen = set()
    result = []
    for candidate in (*custom_names, nexus_clean, title_clean, filename_stem):
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result
