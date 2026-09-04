#!/bin/bash
# Extract translatable strings from the Qt UI and (re)compile translations.
#
#   ./tools/i18n/i18n_update.sh          # update .ts files + compile all .qm
#   ./tools/i18n/i18n_update.sh de fr    # only (re)generate these language .ts files
#
# 1. pyside6-lupdate scans gui_qt/ + wizards_qt/ + Games/ for tr()/translate()
#    calls and
#    merges any new/changed strings into translations/amethyst_<code>.ts,
#    preserving existing human translations (new strings arrive marked
#    "unfinished"). Edit the .ts in Qt Linguist or any text editor.
# 2. pyside6-lrelease compiles each .ts -> .qm (what the app actually loads).
#
# It also always (re)generates translations/amethyst_en.ts, a FILLED English
# base file: every <translation> is pre-populated with the English source text
# and marked finished. Contributors copy it to amethyst_<lang>.ts and translate
# in place instead of starting from empty tags.
#
# Requires PySide6 in the project-root .venv (same one run_qt.sh uses).
set -e
cd "$(dirname "$0")/../.."

VENV=".venv"
LUPDATE="$VENV/bin/pyside6-lupdate"
LRELEASE="$VENV/bin/pyside6-lrelease"
SRC="src"
TR_DIR="$SRC/translations"

if [ ! -x "$LUPDATE" ]; then
    echo "error: $LUPDATE not found — run src/run_qt.sh once to build the venv." >&2
    exit 1
fi

mkdir -p "$TR_DIR"

# Languages to generate. Default set kept in sync with i18n.AVAILABLE_LANGUAGES
# Non-English translations now live in Localisation/ (committed to the Resources
# repo), NOT here — src/translations/ ships ENGLISH ONLY. So with NO args this
# only refreshes the English base below; it does NOT touch/create any language
# .ts in src/translations/. Pass explicit codes to (re)extract a language .ts
# INTO src/translations/ on purpose (e.g. a one-off local test).
if [ "$#" -gt 0 ]; then
    LANGS=("$@")
else
    LANGS=()
fi

# All Python sources with translatable strings. Games/ is included because the
# per-game Qt wizards live there (Games/Morrowind/*_qt.py) — without it a tr()
# added to one would extract to nothing and silently never translate. Keep this
# list in sync with refresh_translations.sh and i18n_gui.py's audit.
mapfile -t PY_FILES < <(find "$SRC/gui_qt" "$SRC/wizards_qt" "$SRC/Games" \
    -name '*.py' -not -path '*/__pycache__/*' | sort)

# -no-obsolete drops strings that are no longer in the source (and any orphaned
# empty-context entries left over from a context change) so re-runs don't
# accumulate stale <message> blocks / empty <name/> contexts.
# -locations none omits the <location filename=… line=…/> refs: they only help a
# translator jump to source in Qt Linguist (no runtime effect — the .qm has none,
# matching is by context+source), but they churn on EVERY code edit that shifts a
# line, bloating diffs. Dropping them means the .ts only changes when a STRING does.
# lupdate writes quotes/apostrophes as XML entities (&quot;/&apos;), but
# contributor/Linguist files use literal " and '. Normalising to the literal
# form (normalize_ts.py) keeps both in sync so edits don't churn in git.
NORMALIZE="$(dirname "$0")/normalize_ts.py"
for code in "${LANGS[@]}"; do
    ts="$TR_DIR/amethyst_${code}.ts"
    echo ">> lupdate -> $ts"
    "$LUPDATE" -no-obsolete -locations none "${PY_FILES[@]}" -ts "$ts" \
        -source-language en -target-language "$code"
    "$VENV/bin/python3" "$NORMALIZE" "$ts" >/dev/null
done

# --- English base (amethyst_en.ts): extract, then fill every <translation>
# with its <source> so translators have a complete, copy-ready reference. -----
EN_TS="$TR_DIR/amethyst_en.ts"
echo ">> lupdate -> $EN_TS (English base)"
"$LUPDATE" -no-obsolete -locations none "${PY_FILES[@]}" -ts "$EN_TS" \
    -source-language en -target-language en
"$VENV/bin/python3" - "$EN_TS" <<'PY'
# Copy each <source>...</source> into its sibling <translation>, dropping the
# type="unfinished" marker so the English base reads as fully translated. The
# .ts is a small, regular XML doc (see amethyst_de.ts) — ElementTree is enough.
import sys, xml.etree.ElementTree as ET

path = sys.argv[1]
tree = ET.parse(path)
root = tree.getroot()
n = 0
for msg in root.iter("message"):
    src = msg.find("source")
    tr = msg.find("translation")
    if src is None or tr is None:
        continue
    tr.text = src.text or ""
    # A finished translation carries no "type" attribute.
    tr.attrib.pop("type", None)
    n += 1

# ElementTree drops the <!DOCTYPE TS> line lupdate writes; restore it so the
# file matches the other .ts files and Qt Linguist stays happy.
body = ET.tostring(root, encoding="unicode")
with open(path, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n')
    f.write(body)
    f.write("\n")
print(f"   filled {n} English translations")
PY
"$VENV/bin/python3" "$NORMALIZE" "$EN_TS" >/dev/null

# Guard the English base: strings with no context are dead (lupdate could not
# tell which class the self.tr() call was in), and a placeholder that .format()
# can't handle is a runtime crash. check_ts.py explains both causes; failing
# here is deliberate - a broken base propagates into every language.
"$VENV/bin/python3" "$(dirname "$0")/check_ts.py" "$EN_TS"

echo ">> lrelease all .ts"
for ts in "$TR_DIR"/amethyst_*.ts; do
    [ -e "$ts" ] || continue
    "$LRELEASE" "$ts"
done

echo "done. Edit the .ts files (Qt Linguist), then re-run to compile .qm."
