#!/bin/bash
# One-command translation refresh: bring every language .ts up to the CURRENT UI
# strings and machine-translate only what's new. This is the command to run
# after adding/changing UI strings.
#
#   ./tools/i18n/refresh_translations.sh <lang-dir> [lang...]
#
# e.g.
#   ./tools/i18n/refresh_translations.sh src/translations              # all langs found
#   ./tools/i18n/refresh_translations.sh src/translations fr de        # just these
#   ./tools/i18n/refresh_translations.sh ~/…/Resources/Localisation    # Resources copy
#
# For each language, in <lang-dir>, it:
#   1. MERGES new UI strings into amethyst_<code>.ts (existing translations
#      kept; new ones arrive "unfinished"). English base is refreshed first.
#   2. Reports how many strings are new/unfinished.
#   3. Machine-translates ONLY the unfinished ones with the best available
#      backend (see below) — never re-translates finished strings.
#   4. Recompiles amethyst_<code>.qm next to the .ts.
#
# TRANSLATION BACKEND (auto-selected; override with AMM_MT_BACKEND=deepl|libre|none):
#   * DeepL   — used if DEEPL_API_KEY is set AND has quota left (probes /usage).
#   * LibreTranslate — used if DeepL is unavailable/exhausted AND a local server
#     is up. Start one with:  ./tools/i18n/libretranslate_server.sh start
#   * none    — neither available: merge + report only; translate the unfinished
#     entries by hand (or set up a backend and re-run).
#
# NB: only src/translations/amethyst_en.ts is touched in src/ (English base);
# language .ts are written wherever <lang-dir> points.
set -e
cd "$(dirname "$0")/../.."

SRC="src"

LOC_DIR="$1"; shift || true
if [ -z "$LOC_DIR" ] || [ ! -d "$LOC_DIR" ]; then
    echo "usage: $0 <resources-localisation-dir> [lang...]" >&2
    echo "  (the dir with amethyst_<code>.ts / .qm)" >&2
    exit 1
fi
LOC_DIR="${LOC_DIR%/}"

# Languages: args, else every amethyst_<code>.ts in Resources except the English
# reference base (en is built into the app, never a shipped language).
if [ "$#" -gt 0 ]; then
    LANGS=("$@")
else
    LANGS=()
    for ts in "$LOC_DIR"/amethyst_*.ts; do
        [ -e "$ts" ] || continue
        code="$(basename "$ts" .ts)"; code="${code#amethyst_}"
        [ "$code" = "en" ] && continue
        LANGS+=("$code")
    done
fi
if [ "${#LANGS[@]}" -eq 0 ]; then
    echo "no languages found/requested." >&2
    exit 1
fi
echo "languages: ${LANGS[*]}"
FAILED=()

# First refresh the built-in English base (the source of truth for the string
# list) WITHOUT touching any language .ts in src/translations/ (en-only there).
# Only the chatter is dropped - stderr is kept, because i18n_update.sh FAILS on
# a broken English base (dead strings / unsafe placeholders) and swallowing that
# would abort the refresh with no explanation.
./tools/i18n/i18n_update.sh >/dev/null
LRELEASE=".venv/bin/pyside6-lrelease"
LUPDATE=".venv/bin/pyside6-lupdate"
# Same roots as i18n_update.sh — see the comment there (Games/ holds per-game
# Qt wizards). A mismatch here would merge a different string set per language.
mapfile -t PY_FILES < <(find "$SRC/gui_qt" "$SRC/wizards_qt" "$SRC/Games" \
    -name '*.py' -not -path '*/__pycache__/*' | sort)

# --- Pick a machine-translation backend --------------------------------------
# Priority: DeepL (if a working key) > LibreTranslate (if its local server is
# up) > none (report only). AMM_MT_BACKEND=deepl|libre|none forces one.
_LT_URL="${AMM_LT_URL:-http://127.0.0.1:5000}"
_libre_up() { curl -s -o /dev/null -m 3 "$_LT_URL/languages" 2>/dev/null; }
_deepl_ok() {
    [ -n "${DEEPL_API_KEY:-}" ] || return 1
    # A 456 (quota) or missing key means DeepL is unusable — probe /usage.
    local host="https://api.deepl.com/v2/usage"
    case "$DEEPL_API_KEY" in *:fx) host="https://api-free.deepl.com/v2/usage";; esac
    local body
    body=$(curl -s -m 10 "$host" \
        -H "Authorization: DeepL-Auth-Key $DEEPL_API_KEY" 2>/dev/null)
    # If usage came back and count < limit, DeepL has room.
    echo "$body" | grep -q '"character_count"' || return 1
    echo "$body" | python3 -c 'import sys,json
d=json.load(sys.stdin)
sys.exit(0 if d["character_count"] < d["character_limit"] else 1)' 2>/dev/null
}

BACKEND="${AMM_MT_BACKEND:-}"
if [ -z "$BACKEND" ]; then
    if _deepl_ok; then BACKEND=deepl
    elif _libre_up; then BACKEND=libre
    else BACKEND=none; fi
fi
case "$BACKEND" in
    deepl) echo "translation backend: DeepL" ;;
    libre) echo "translation backend: LibreTranslate ($_LT_URL)" ;;
    none)
        echo "translation backend: NONE — will merge + report only."
        echo "  (set DEEPL_API_KEY, or start LibreTranslate:"
        echo "     ./tools/i18n/libretranslate_server.sh start)" ;;
esac

for code in "${LANGS[@]}"; do
    echo ""
    echo "=== $code ==="
    ts="$LOC_DIR/amethyst_${code}.ts"
    if [ ! -f "$ts" ]; then
        echo "  ! $ts not found — skipping (use tools/i18n/i18n_deepl.py to start a"
        echo "    new language, or Localisation/tools/new_language.py)."
        continue
    fi
    # 1. Merge new strings straight INTO the Localisation .ts (existing
    #    translations preserved; new ones arrive marked unfinished).
    # -locations none: don't record source line refs (they churn on any code
    # edit; no runtime effect). See i18n_update.sh for the full rationale.
    "$LUPDATE" -no-obsolete -locations none "${PY_FILES[@]}" -ts "$ts" \
        -source-language en -target-language "$code" \
        2>&1 | grep -E "Found [0-9]+ source" | head -1

    # 2. Count what still needs translating.
    unfinished=$(grep -c 'type="unfinished"' "$ts" || true)
    echo "  $unfinished string(s) still unfinished"

    # 3. Machine-translate the unfinished ones with the chosen backend. Both
    #    tools read the en base + write back to the .ts in $LOC_DIR
    #    (AMM_I18N_OUT_DIR targets this dir, whether Localisation/ or
    #    src/translations).
    if [ "${unfinished:-0}" -gt 0 ]; then
        case "$BACKEND" in
            deepl)
                echo "  translating $unfinished new string(s) via DeepL…"
                AMM_I18N_OUT_DIR="$LOC_DIR" \
                    .venv/bin/python3 tools/i18n/i18n_deepl.py "$code" --only-unfinished ;;
            libre)
                echo "  translating $unfinished new string(s) via LibreTranslate…"
                AMM_I18N_OUT_DIR="$LOC_DIR" AMM_LT_URL="$_LT_URL" \
                    .venv/bin/python3 tools/i18n/i18n_libre.py "$code" --only-unfinished ;;
            none)
                echo "  (no backend — edit $ts by hand, then re-run to compile.)" ;;
        esac
    fi

    # 4. Normalise quotes/apostrophes to literal (matches Linguist/contributor
    #    files, so edits don't churn in git — see normalize_ts.py).
    .venv/bin/python3 "$(dirname "$0")/normalize_ts.py" "$ts" >/dev/null

    # 5. Validate before compiling. A translation whose {0}/{1} placeholders
    #    .format() can't handle raises inside a Qt callback at runtime — the
    #    translator never sees it, the user gets a broken screen. --quarantine
    #    marks those entries unfinished so lrelease leaves them out and Qt falls
    #    back to English; the text stays in the .ts to be fixed. Reported, not
    #    silent. `|| true`: one bad string must not abandon the other languages.
    .venv/bin/python3 "$(dirname "$0")/check_ts.py" --quarantine "$ts" || {
        echo "  ! $code has problems (see above) — compiled without them"
        FAILED+=("$code")
    }

    "$LRELEASE" "$ts" 2>&1 | grep -E "Generated" | head -1
    echo "  updated -> $ts + .qm"
done

# Refresh the English reference base in Localisation (the copy-me file).
if [ -f "$SRC/translations/amethyst_en.ts" ]; then
    cp "$SRC/translations/amethyst_en.ts" "$LOC_DIR/amethyst_en.ts"
    echo ""
    echo "refreshed English reference: $LOC_DIR/amethyst_en.ts"
fi
echo ""
if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "WARNING: unusable entries were quarantined in: ${FAILED[*]}"
    echo "  Those strings fall back to English until fixed in the .ts."
fi
echo "done. Review the diffs in $LOC_DIR, then commit to the Resources branch."
