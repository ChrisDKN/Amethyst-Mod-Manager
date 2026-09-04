# i18n tooling

Everything for translating the app's UI lives here. Paths below are from the
repo root.

## Quick start (GUI)

```sh
./tools/i18n/translation_manager.sh
```

A window to: pick the translations folder, see each language's status vs the
English base, pick a backend (DeepL / LibreTranslate / Auto), enter the DeepL
API key, start/stop the local LibreTranslate server, and run a refresh with a
live log.

The **DeepL key** field lives in the "Translation backend" box: paste the key,
hit **Save**, and the quota line under it re-probes `/usage` right away. It's
stored in `~/.config/amethyst/i18n_gui.json` (mode 0600, outside the repo) and
exported as `DEEPL_API_KEY` to every task the GUI runs, so no shell export is
needed. An empty field falls back to `DEEPL_API_KEY` from the environment at
startup; clearing it after that runs the tasks with no key at all.

## The workflow

Translation `.ts`/`.qm` normally live on the **Resources branch** and are synced
to users at runtime. To update after changing UI strings:

1. **Pull** the current language `.ts` from the Resources branch (GitHub) — the
   GUI's "⭳ Pull from Resources" button, or
   `./tools/i18n/pull_from_resources.py src/translations`.
2. **Refresh** — GUI, or `./tools/i18n/refresh_translations.sh src/translations`.
3. **Push** the updated `.ts` + `.qm` to the Resources branch's `Localisation/`.

No branch-switching needed to update — pull, refresh, push. `src/translations/`
ships **English only**; other languages reach users via the runtime sync.

## Files

| File | What it does |
|------|--------------|
| `translation_manager.sh` | Launch the GUI (`i18n_gui.py`) with the app's venv. |
| `i18n_gui.py` | PySide6 GUI — a thin front-end over the scripts below. |
| `refresh_translations.sh` | **Main command.** Merge new strings into each language + machine-translate only the new ones + recompile. Auto-picks the backend. |
| `libretranslate_server.sh` | `start`/`stop`/`status`/`setup` a local LibreTranslate server (free DeepL fallback). Handles venv + model downloads. |
| `i18n_deepl.py` | DeepL backend (needs `DEEPL_API_KEY` — the GUI sets it from its key field). |
| `i18n_libre.py` | LibreTranslate backend (needs the local server). |
| `i18n_update.sh` | Extract strings → refresh the English base (`amethyst_en.ts`) + compile. |
| `i18n_wrap.py` | Dev tool: auto-wrap unwrapped `tr()` strings in a source file. |
| `i18n_batch.sh` | Run `i18n_wrap.py` over many files. |
| `pull_from_resources.py` | Download the language `.ts` from the Resources branch (GitHub) so you can refresh without switching branches. |
| `normalize_ts.py` | Normalise a `.ts` to literal quotes/apostrophes (not `&quot;`/`&apos;`) so lupdate and Qt Linguist stop producing spurious diffs. Run automatically by the refresh; safe to run by hand. |
| `check_ts.py` | Validate a `.ts` for problems only visible at runtime: strings with no context (dead — never match a lookup) and placeholders `str.format()` would choke on. Run automatically by the refresh; `--quarantine` degrades a bad entry to English instead of failing. |
| `ts_merge.py` | Context-aware merge helpers shared by the two backends, so a translator's per-context work survives a refresh. |

## Translation backends

`refresh_translations.sh` auto-selects: **DeepL** (if `DEEPL_API_KEY` set and has
quota — it probes `/usage`) → **LibreTranslate** (if the local server is up) →
**none** (merge + report only). Force one with `AMM_MT_BACKEND=deepl|libre|none`.

DeepL free tier is 500k chars/month. When it's exhausted, use LibreTranslate:

```sh
./tools/i18n/libretranslate_server.sh start   # sets up + runs (models cache after 1st run)
./tools/i18n/refresh_translations.sh src/translations   # auto-detects the server
./tools/i18n/libretranslate_server.sh stop
```

## Notes

- Machine translations (both backends) are **placeholder quality** — a native
  review is worth it before calling a language "official". LibreTranslate is
  rougher than DeepL, especially for CJK.
- Placeholders (`{0}`, `{1}`) are protected during machine translation **and
  verified afterwards** by `check_ts.py`. This matters: our translated strings go
  through `str.format()`, so a translation that renames `{0}` to `{O}`, invents a
  `{2}`, or leaves an unbalanced brace raises inside a Qt callback at runtime —
  where no translator would ever see it. The refresh quarantines those entries
  (marks them unfinished, so Qt falls back to English) and reports them; the
  English base fails the build outright. Reordering (`{1} … {0}`) is fine, and
  dropping a placeholder is a warning, not an error — `format()` ignores extra
  args.
- Translations are keyed by **(context, source)**, never source alone. "Save"
  appears in 15 contexts and "Cancel" in 43; a source-only key lets one
  context's translation overwrite all the others. See `ts_merge.py`.
- `-locations none` is used so a code edit that shifts line numbers never churns
  the `.ts` — they only change when a string does.
- **Quotes/apostrophes are stored literally** (`"`, `'`), not as `&quot;`/`&apos;`
  entities. `pyside6-lupdate` emits entities, so the refresh runs `normalize_ts.py`
  to convert them back — this matches Qt Linguist / hand-edited contributor files,
  so translating a few strings and saving produces a clean, small diff instead of
  hundreds of entity-churn lines. (`&amp;`/`&lt;`/`&gt;` stay escaped — required
  for valid XML. Both forms compile to the identical `.qm`.)
- **"Check for unwrapped strings"** (GUI) / `i18n_wrap.py --list` audits
  `gui_qt/` + `wizards_qt/` for user-facing text not wrapped in `tr()`. It covers
  widget/setter calls, keyword args (`title=`, `confirm_label=`, …), `safe_emit`
  status text, file-local text helpers, and UI-named literal lists/dicts
  (`_COLS`, `*_TIPS`). To silence a deliberately-untranslated literal (a protocol
  token, or a keyword matched against source-language data), put `# i18n: skip`
  on its line — that drops it from both the report and `--apply`.
