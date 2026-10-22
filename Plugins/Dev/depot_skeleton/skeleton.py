import html
import re
from pathlib import Path


_ROW = re.compile(
    r"<tr>\s*"
    r"<td>(?P<path>[^<]*)</td>\s*"
    r"<td(?P<typeattr>[^>]*)>(?P<type>[^<]*)</td>\s*"
    r'<td data-sort="(?P<sort>-?\d+)"',
    re.IGNORECASE,
)
_TABLE = re.compile(
    r"depot-files.*?<tbody[^>]*>(?P<body>.*?)</tbody>",
    re.IGNORECASE | re.DOTALL,
)


def parse_entries(text):
    table = _TABLE.search(text)
    body = table.group("body") if table else text
    for match in _ROW.finditer(body):
        relative = html.unescape(match.group("path")).strip()
        if relative:
            is_dir = "folder" in match.group("type").strip().lower() or match.group("sort") == "-1"
            yield relative, is_dir


def build(text, output, check_cancelled):
    entries = []
    for entry in parse_entries(text):
        check_cancelled()
        entries.append(entry)
    if not entries:
        return {"matched": 0, "files": 0, "dirs": 0, "skipped": 0}
    check_cancelled()
    output = Path(output)
    if output.is_symlink():
        raise ValueError("The output folder must not be a symbolic link.")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    files = dirs = skipped = 0
    for want_dir in (True, False):
        for relative, is_dir in entries:
            check_cancelled()
            if is_dir != want_dir:
                continue
            relative = relative.replace("\\", "/").lstrip("/")
            target = (output / relative).resolve()
            if target == output or output not in target.parents:
                skipped += 1
                continue
            if is_dir:
                if target.exists():
                    skipped += 1
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    dirs += 1
            elif target.exists():
                skipped += 1
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb"):
                    pass
                files += 1
    return {"matched": len(entries), "files": files, "dirs": dirs, "skipped": skipped}
