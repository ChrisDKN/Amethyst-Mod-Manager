import re
from pathlib import Path

from Utils.atomic_write import write_atomic_text


def _read_ini_key(ini_path: Path, section: str, key: str,
                  *, case_insensitive: bool = False) -> "str | None":
    """Return the current value for [section] key, or None if not present."""
    try:
        text = ini_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        text = ini_path.read_text(encoding="utf-8", errors="replace")

    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=(?P<value>.*)$",
                        re.IGNORECASE if case_insensitive else 0)
    want_section = section.casefold() if case_insensitive else section

    in_section = False
    for line in text.splitlines():
        m = section_re.match(line)
        if m:
            found = m.group("name").strip()
            in_section = (found.casefold() if case_insensitive else found) == want_section
            continue
        if in_section:
            km = key_re.match(line)
            if km:
                return km.group("value").rstrip("\r")
    return None


def _set_ini_key(ini_path: Path, section: str, key: str, value: "str | None",
                 *, case_insensitive: bool = False) -> None:
    # Preserve CRLF when reading and rewriting INIs.
    try:
        with ini_path.open(encoding="utf-8", newline="") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    except UnicodeDecodeError:
        with ini_path.open(encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline) if text else []

    section_header = f"[{section}]"
    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=",
                        re.IGNORECASE if case_insensitive else 0)
    want_section = section.casefold() if case_insensitive else section

    section_start = -1
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = section_re.match(line)
        if not m:
            continue
        found = m.group("name").strip()
        if case_insensitive:
            found = found.casefold()
        if section_start == -1 and found == want_section:
            section_start = i
        elif section_start != -1:
            section_end = i
            break

    if section_start == -1:
        if value is None:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(section_header)
        lines.append(f"{key}={value}")
        lines.append("")
    else:
        key_line = -1
        for i in range(section_start + 1, section_end):
            if key_re.match(lines[i]):
                key_line = i
                break

        if value is None:
            if key_line != -1:
                del lines[key_line]
                section_end -= 1
            has_content = any(
                ln.strip() and not ln.strip().startswith((";", "#"))
                for ln in lines[section_start + 1:section_end]
            )
            if not has_content:
                trailing = section_end
                while trailing < len(lines) and lines[trailing] == "":
                    trailing += 1
                del lines[section_start:trailing]
        else:
            new_line = f"{key}={value}"
            if key_line != -1:
                lines[key_line] = new_line
            else:
                insert_at = section_end
                while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
                    insert_at -= 1
                lines.insert(insert_at, new_line)

    out = newline.join(lines)
    if text.endswith(newline) and not out.endswith(newline):
        out += newline
    # Write through profile symlinks so the link and profile content survive.
    if ini_path.is_symlink():
        real = ini_path.resolve()
        write_atomic_text(real, out)
    else:
        write_atomic_text(ini_path, out)
