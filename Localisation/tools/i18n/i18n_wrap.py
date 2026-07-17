#!/usr/bin/env python3
"""Source-preserving helper to wrap user-facing strings in self.tr(...).

This does NOT do a full AST round-trip (that would reflow the whole file and
destroy comments/formatting). Instead it locates specific, provably-safe string
argument nodes and rewrites ONLY those byte spans, leaving everything else
untouched.

Scope (conservative on purpose):
  * The first positional arg of a configured set of calls/constructors, when
    that arg is:
      - a plain string literal            "Foo"           -> self.tr("Foo")
      - an implicit-concat of literals     "a" "b"         -> self.tr("a" "b")
      - a simple f-string with only {name}/{obj.attr} field exprs
                                           f"Total: {x}"   -> self.tr("Total: {0}").format(x)
  * Anything else (method calls in the f-string, ternaries, format specs,
    already-wrapped tr() calls, non-self classes) is SKIPPED and reported so a
    human can handle it.

Usage:
    python3 tools/i18n_wrap.py <file.py> [--apply]     # dry-run unless --apply
    python3 tools/i18n_wrap.py <file.py> --list        # just report sites

It prints, per file, how many sites it wrapped and how many it skipped (with
line numbers + reason) so the residue can be finished by hand.

Suppression: put `# i18n: skip` (optionally `# i18n: skip — reason`) on a line
whose flagged literal is intentionally NOT translated — a protocol/format token,
or a data-match keyword that must stay in the source language. Suppressed lines
are dropped from both the report AND --apply.
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass

# Call names whose FIRST positional arg is user-facing text. Constructors are
# matched by the callee's attribute/name; methods by attribute name.
WRAP_CALLS = {
    # widgets built with visible text
    "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QToolButton",
    "QGroupBox", "QAction",
    # NB: NOT QLineEdit — its ctor arg is initial *content*, not a label.
    # setters
    "setText", "setToolTip", "setPlaceholderText", "setWindowTitle",
    "setStatusTip", "setTitle", "addAction", "setWhatsThis",
    # app notifications: self._notify("msg", state)
    "_notify",
    # project-specific helpers whose FIRST positional arg is display text
    # (verified signatures): wizard page headers, buttons, section labels,
    # status/hint lines. NB: only helpers where text is the FIRST arg — e.g.
    # _make_note(self, lay, text) has text SECOND, so it's handled by hand.
    "_step_page", "_small_btn", "_text_button", "_color_button",
    "_section_header", "_section_title", "_section_label", "_hint",
    "_accent_btn", "_field_label", "_primary", "_page", "_help_label",
    "_green_btn", "_set_prefix_status", "_panel", "_status", "_set_tip",
    "_make_section_header", "_mono_edit", "_append_box",
}

# Helpers whose display text is the SECOND positional arg (arg index 1), e.g.
# self._make_note(layout, "text"). Same wrapping, different arg position.
WRAP_CALLS_ARG2 = {
    "_make_note",
    # _set_status(self, status_label, "text", color) in the wizard views. NB a
    # few files define _set_status(self, "text", kind) with text FIRST — those
    # are handled by hand (the tool skips arg-1 there since it's not a string).
    "_set_status",
    # open_tab(widget, "Tab Title", key) / open_scoped_tab(widget, "Title", ...)
    # — the tab-bar label is the 2nd arg.
    "open_tab", "open_scoped_tab",
    # safe_emit(self._status_sig, "text", color) — the status text a worker
    # thread pushes into a visible QLabel is the 2nd arg.
    "safe_emit",
}

# Keyword arguments whose value is user-facing display text, regardless of
# position — e.g. super().__init__(..., title="Foo"), show_over(...,
# confirm_label="Delete"). The positional-only scan above can't see these
# because they never appear in call.args; they live in call.keywords.
WRAP_KWARGS = {
    "title", "text", "heading", "label", "confirm_label", "cancel_label",
    "ok_label", "button_text", "next_text", "missing_text", "placeholder",
    "tooltip", "status", "message",
}

# Callees where a display-looking kwarg (esp. `label=`) is actually a log/tool
# identifier, NOT UI text — skip the keyword scan on these so we don't wrap e.g.
# run_tool_logged(..., label="ESLifier"). Matched on the callee's name/attr.
KWARG_CALLEE_DENYLIST = {
    "run_tool_logged", "run_tool", "app_log", "log", "_log", "_wlog",
    "logging", "getLogger", "debug", "info", "warning", "error", "exception",
    "run_in_worker", "Signal", "emit",
}

# Receiver expression to use for tr(). Everything here is inside a QObject
# subclass method, so "self" is right; a plain-class fallback would use
# QCoreApplication.translate but we SKIP those (reported) instead of guessing.
TR_RECEIVER = "self"


@dataclass
class Site:
    lineno: int
    col: int
    end_lineno: int
    end_col: int
    kind: str          # "plain" | "fstring"
    replacement: str
    reason: str = ""


# Inline suppression marker. Put `# i18n: skip` (optionally `# i18n: skip —
# reason`) on a line to tell the audit that a flagged literal there is
# intentionally NOT translated (a protocol token, a data-match keyword, etc.).
# Line-anchored, so it survives line-number churn better than a path/line list.
_SKIP_MARKER = re.compile(r"#\s*i18n:\s*skip\b")


def _line_suppressed(src_lines, lineno: int) -> bool:
    return (1 <= lineno <= len(src_lines)
            and bool(_SKIP_MARKER.search(src_lines[lineno - 1])))


def _drop_suppressed(sites, src_lines):
    """Remove Sites whose line carries the `# i18n: skip` marker."""
    return [s for s in sites if not _line_suppressed(src_lines, s.lineno)]


def _callee_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _is_plain_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _worth_translating(s: str) -> bool:
    """True only if the string has actual letters to translate.

    Skips empty strings and decorative glyph/symbol/punctuation-only labels
    (e.g. "●", "▶", "⊟", "…", "1/2") — wrapping those in tr() just pollutes the
    catalogue with untranslatable noise.
    """
    return bool(s) and any(ch.isalpha() for ch in s)


def _fstring_to_template(node: ast.JoinedStr) -> str | None:
    """Return `self.tr("template").format(args)` or None if not safely convertible.

    Only handles field exprs that are a bare Name or a dotted attribute chain of
    Names (e.g. x, obj.attr, a.b.c) and literal text parts. Rejects format specs,
    conversions (!r), calls, subscripts, ternaries, etc.
    """
    template_parts: list[str] = []
    args: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            # Escape braces so .format() treats them as literal.
            template_parts.append(part.value.replace("{", "{{").replace("}", "}}"))
        elif isinstance(part, ast.FormattedValue):
            if part.conversion != -1 or part.format_spec is not None:
                return None
            expr = _dotted_name(part.value)
            if expr is None:
                return None
            template_parts.append("{%d}" % len(args))
            args.append(expr)
        else:
            return None
    if not args:
        # No interpolation — it's effectively a plain string; caller handles.
        return None
    template = "".join(template_parts)
    # Build a double-quoted Python literal safely via repr, but prefer keeping
    # it readable: use ast to render the string constant.
    literal = _py_str_literal(template)
    return f'{TR_RECEIVER}.tr({literal}).format({", ".join(args)})'


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _py_str_literal(s: str) -> str:
    # Render as a double-quoted literal; repr may pick single quotes, so
    # normalise when safe.
    r = repr(s)
    if r.startswith("'") and '"' not in s and "\\" not in r:
        r = '"' + r[1:-1] + '"'
    return r


def _self_method_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """Line ranges of every function whose first arg is `self`.

    Only inside such a method is `self.tr(...)` valid. A wrap site outside all
    of these (module-level function, staticmethod, plain-class method) would
    raise NameError at runtime, so we report those instead of wrapping.
    """
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            first = (a.posonlyargs + a.args)[:1]
            if first and first[0].arg == "self":
                ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


# Parameter names that, on a locally-defined helper, carry user-facing display
# text — so a call like add_check(key, "Label", gate) passes translatable text
# positionally into a helper the hardcoded WRAP_CALLS list can't know about.
_DISPLAY_PARAM_NAMES = {
    "text", "label", "title", "heading", "prompt", "caption", "tooltip",
    "message", "placeholder",
}


def _local_text_helpers(tree: ast.AST) -> dict[str, int]:
    """Map {helper_name: arg_index} for functions defined in this file whose
    parameter list contains a display-text-named param (text/label/title/…).

    This lets find_sites() catch string literals passed positionally into
    file-local builders (e.g. a nested `def add_check(key, text, gate)`), which
    otherwise slip through because the callee isn't a known widget/setter. Only
    the FIRST display-named param is used as the wrap target; a param that is
    itself `self`/`cls` is skipped when counting the index.
    """
    import re
    # Helpers whose name marks them as NON-UI sinks (loggers, appenders,
    # setters that write files) — a `text`/`message` param there is a log line,
    # not display text. Skip them to avoid flooding the audit with log strings.
    non_ui_re = re.compile(r"log|print|debug|write|emit|append_log|_log", re.I)
    # The project's OWN translation helpers (they take a `label`/`text` param but
    # ARE the tr() wrapper — treating them as display-text helpers would flag the
    # already-translated string inside them). _is_tr_call knows these names too.
    tr_helpers = {"_mt", "_mtf", "_t", "_tr", "_trf", "tr", "translate"}
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if non_ui_re.search(node.name) or node.name in tr_helpers:
            continue
        params = node.args.posonlyargs + node.args.args
        names = [p.arg for p in params]
        # Positional index of the arg AS SEEN BY THE CALLER: for a method, the
        # caller doesn't pass self, so drop a leading self/cls from the count.
        offset = 1 if names[:1] in (["self"], ["cls"]) else 0
        for i, pname in enumerate(names):
            if i < offset:
                continue
            if pname in _DISPLAY_PARAM_NAMES:
                out[node.name] = i - offset
                break
    return out


def _is_tr_call(node) -> bool:
    """True if node is a tr()/translate()/QT_TRANSLATE_NOOP(...) call."""
    return (isinstance(node, ast.Call)
            and _callee_name(node) in (
                "tr", "translate", "QT_TRANSLATE_NOOP",
                # project-local translation helpers (QCoreApplication.translate
                # wrappers): _mt/_mtf in the menu modules, _t in others.
                "_mt", "_mtf", "_t", "_tr", "_trf"))


def _fstring_has_text(node: ast.JoinedStr) -> bool:
    """True if an f-string has any alphabetic LITERAL text worth translating.
    f"{a}/{b}" or f"{opt.label}\n\n{desc}" (pure interpolation) has none."""
    lit = "".join(p.value for p in node.values
                  if isinstance(p, ast.Constant) and isinstance(p.value, str))
    return any(ch.isalpha() for ch in lit)


def _ternary_has_unwrapped_text(node: ast.IfExp) -> bool:
    """A ternary is a real miss only if some branch is an UNWRAPPED translatable
    string. `self.tr("A") if x else self.tr("B")` (both wrapped) is fine; a
    branch that's a data-only f-string is not; nested ternaries recurse."""
    for branch in (node.body, node.orelse):
        if _is_tr_call(branch):
            continue
        if _is_plain_str(branch) and _worth_translating(branch.value):
            return True
        if isinstance(branch, ast.JoinedStr) and _fstring_has_text(branch):
            return True
        if isinstance(branch, ast.IfExp) and _ternary_has_unwrapped_text(branch):
            return True
    return False


def _classify_arg(arg, src, has_self, wrapped, skipped):
    """Sort one display-text argument node into wrapped/skipped.

    Shared by the positional and keyword scans in find_sites(). Mutates the
    passed-in lists. Returns nothing.
    """
    # Already wrapped? self.tr(...) / translate(...) as the arg.
    if _is_tr_call(arg):
        return

    # A ternary whose branches are all already wrapped is not a miss.
    if isinstance(arg, ast.IfExp) and not _ternary_has_unwrapped_text(arg):
        return

    # tr() needs a QObject `self` in scope; if this call isn't inside a
    # self-method, report it (unless it's a non-translatable literal).
    translatable = (
        (_is_plain_str(arg) and _worth_translating(arg.value))
        or isinstance(arg, (ast.JoinedStr, ast.IfExp)))
    if translatable and not has_self(arg.lineno):
        skipped.append(Site(arg.lineno, arg.col_offset, arg.end_lineno,
                            arg.end_col_offset, "noself", "",
                            "no self in scope (manual)"))
        return

    if _is_plain_str(arg):
        if not _worth_translating(arg.value):
            return  # empty / glyph-only / non-letter — not translatable
        lit = _slice(src, arg)
        wrapped.append(Site(arg.lineno, arg.col_offset,
                            arg.end_lineno, arg.end_col_offset,
                            "plain", f"{TR_RECEIVER}.tr({lit})"))
    elif isinstance(arg, ast.JoinedStr):
        # An f-string with no alphabetic literal text (e.g. f"{a}/{b}",
        # f"({i+1}/{t})") has nothing to translate — don't flag it.
        if not _fstring_has_text(arg):
            return
        repl = _fstring_to_template(arg)
        if repl is None:
            skipped.append(Site(arg.lineno, arg.col_offset, arg.end_lineno,
                                arg.end_col_offset, "fstring", "",
                                "complex f-string (manual)"))
        else:
            wrapped.append(Site(arg.lineno, arg.col_offset, arg.end_lineno,
                                arg.end_col_offset, "fstring", repl))
    elif isinstance(arg, ast.IfExp):
        skipped.append(Site(arg.lineno, arg.col_offset, arg.end_lineno,
                            arg.end_col_offset, "ternary", "",
                            "ternary text (manual)"))
    # else: not a string arg (variable, etc.) — ignore silently.


def find_sites(tree: ast.AST, src: str) -> tuple[list[Site], list[Site]]:
    wrapped: list[Site] = []
    skipped: list[Site] = []
    self_ranges = _self_method_ranges(tree)
    # File-local builders whose Nth positional arg is display text (discovered
    # from param names) — e.g. a nested `def add_check(key, text, gate)`.
    local_helpers = _local_text_helpers(tree)

    def _has_self(lineno: int) -> bool:
        return any(lo <= lineno <= hi for lo, hi in self_ranges)

    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = _callee_name(call)
        # Which positional arg carries the display text (0 for most, 1 for the
        # _make_note-style helpers, or the discovered index for a local helper).
        if name in WRAP_CALLS:
            arg_idx = 0
        elif name in WRAP_CALLS_ARG2:
            arg_idx = 1
        elif name in local_helpers:
            arg_idx = local_helpers[name]
        else:
            arg_idx = None
        if arg_idx is not None and len(call.args) > arg_idx:
            _classify_arg(call.args[arg_idx], src, _has_self, wrapped, skipped)

        # Keyword-argument display text — e.g. super().__init__(..., title="…"),
        # show_over(..., confirm_label="…"). These never appear in call.args, so
        # the positional scan above can't see them. We scan keywords on EVERY
        # call (not just known UI ctors) but only for the curated WRAP_KWARGS
        # names, and skip callees where such a kwarg is a log/tool identifier.
        if name not in KWARG_CALLEE_DENYLIST:
            for kw in call.keywords:
                if kw.arg in WRAP_KWARGS:
                    _classify_arg(kw.value, src, _has_self, wrapped, skipped)

    src_lines = src.splitlines()
    return (_drop_suppressed(wrapped, src_lines),
            _drop_suppressed(skipped, src_lines))


# Assignment-target names that hold user-facing display strings as bare literals
# in a list/tuple/dict — column headers, tooltip tables, button-label maps. These
# are NOT calls, so find_sites() (and --apply) never see them; they need a
# QT_TRANSLATE_NOOP registration + self.tr() at display, which the auto-wrapper
# can't do. find_literal_sites() reports them for MANUAL handling only.
def find_literal_sites(tree: ast.AST, src: str) -> list[Site]:
    """Report-only: bare display-string literals in UI-named list/dict/tuple
    assignments (e.g. `_COLS = ["Name", ...]`, `_FLAG_TIPS = {k: "Note"}`).

    Matches on the assignment TARGET name (COLUMNS/_COLS/*_TIPS/*_LABELS/
    *_HEADERS/*_COLS) so ordinary constant lists aren't flagged. Every hit needs
    a manual QT_TRANSLATE_NOOP fix, so these are never auto-wrapped.
    """
    import re
    # NB: LABELS (plural) only — a singular *_LABEL like TOOL_LABEL is usually a
    # single tool/identifier string, not a UI display table, so we don't flag it.
    # TITLES (plural) likewise; a lone _TITLE is caught by the WRAP_KWARGS scan.
    name_re = re.compile(
        r"(?:^|_)(COL|COLS|COLUMNS|HEADERS?|TIPS|LABELS|TITLES)$")

    def _name_is_ui(target) -> bool:
        n = None
        if isinstance(target, ast.Name):
            n = target.id
        elif isinstance(target, ast.AnnAssign) and isinstance(target.target, ast.Name):
            n = target.target.id
        if not n:
            return False
        up = n.upper()
        return bool(name_re.search(up))

    def _string_leaves(node):
        """Yield plain-string Constant nodes inside a list/tuple/dict literal
        (values only, not dict keys)."""
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for e in node.elts:
                yield from _string_leaves(e)
        elif isinstance(node, ast.Dict):
            for v in node.values:
                yield from _string_leaves(v)
        elif _is_plain_str(node):
            yield node
        # Anything else (a QT_TRANSLATE_NOOP(...) call, a Name, an f-string) is
        # already handled or not a bare literal — skip.

    # Strings ALREADY registered via QT_TRANSLATE_NOOP anywhere in the file are
    # translatable — a bare COLUMNS=[…] that doubles as persistence keys with a
    # sibling _COL_TR=(QT_TRANSLATE_NOOP(…),…) is the canonical pattern here, and
    # must NOT be flagged. Collect those source strings and suppress matches.
    registered = set()
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and _callee_name(c) == "QT_TRANSLATE_NOOP":
            # QT_TRANSLATE_NOOP(context, "text") — text is the 2nd positional.
            if len(c.args) >= 2 and _is_plain_str(c.args[1]):
                registered.add(c.args[1].value)

    out: list[Site] = []
    seen_lines = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(_name_is_ui(t) for t in targets):
            continue
        # Report at most ONE site per assignment line (the list may hold many
        # strings; one flag per definition is enough to point the eye there).
        for leaf in _string_leaves(value):
            if not _worth_translating(leaf.value):
                continue
            if leaf.value in registered:
                continue  # already covered by a QT_TRANSLATE_NOOP sibling
            if node.lineno in seen_lines:
                continue
            seen_lines.add(node.lineno)
            out.append(Site(leaf.lineno, leaf.col_offset, leaf.end_lineno,
                            leaf.end_col_offset, "literal", "",
                            "UI literal in list/dict — needs QT_TRANSLATE_NOOP "
                            "(manual)"))
    return _drop_suppressed(out, src.splitlines())


def _slice(src: str, node: ast.AST) -> str:
    """Exact source text of `node`, sliced by BYTE offsets (ast cols are bytes)."""
    data = src.encode("utf-8")
    line_start = [0]
    for b in data.splitlines(keepends=True):
        line_start.append(line_start[-1] + len(b))
    start = line_start[node.lineno - 1] + node.col_offset
    end = line_start[node.end_lineno - 1] + node.end_col_offset
    return data[start:end].decode("utf-8")


def apply_sites(src: str, sites: list[Site]) -> str:
    # ast col offsets are UTF-8 BYTE offsets, so do all slicing in bytes (a
    # non-ASCII char like "…" is 3 bytes but 1 str codepoint — mixing the two
    # corrupts spans). Decode back to str only at the end.
    data = src.encode("utf-8")
    # Byte offset of the start of each line (1-based line -> byte index).
    line_start = [0]
    for b in data.splitlines(keepends=True):
        line_start.append(line_start[-1] + len(b))

    def off(lineno, col):
        return line_start[lineno - 1] + col

    spans = sorted(
        ((off(s.lineno, s.col), off(s.end_lineno, s.end_col),
          s.replacement.encode("utf-8"))
         for s in sites),
        reverse=True)
    out = data
    for start, end, repl in spans:
        out = out[:start] + repl + out[end:]
    return out.decode("utf-8")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        return 2
    path = args[0]
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    src_lines = src.splitlines()
    wrapped, skipped = find_sites(tree, src)
    literals = find_literal_sites(tree, src)

    print(f"{path}: {len(wrapped)} wrappable, "
          f"{len(skipped) + len(literals)} need manual review")
    if "--list" in flags or "--apply" not in flags:
        for s in skipped + literals:
            print(f"  SKIP L{s.lineno}: {s.reason}: "
                  f"{src_lines[s.lineno-1].strip()[:80]}")
    if "--apply" in flags and wrapped:
        new = apply_sites(src, wrapped)
        # sanity: must still parse
        ast.parse(new)
        open(path, "w", encoding="utf-8").write(new)
        print(f"  applied {len(wrapped)} wraps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
