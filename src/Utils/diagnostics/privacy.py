"""Username redaction at diagnostic output boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import re


def _home_patterns():
    homes = {os.path.expanduser("~")}
    try:
        import pwd
        homes.add(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        pass
    homes.update(os.path.realpath(home) for home in tuple(homes))
    patterns = []
    for home in sorted(homes, key=len, reverse=True):
        if not home or home in ("/", ".", "~"):
            continue
        for source in (home, home.replace("/", "\\"), home.replace("/", "\\\\")):
            parent = source[:len(source) - len(Path(home).name)]
            patterns.append((re.escape(source) + r"(?=$|[/\\\s\"'<>:;,)}\]])",
                             parent + "<user>"))
    for user in {Path(home).name for home in homes} - {"", ".", "~"}:
        for root in ("/media/", "/run/media/"):
            for prefix in (root, root.replace("/", "\\"), root.replace("/", "\\\\")):
                patterns.append((re.escape(prefix + user) + r"(?=$|[/\\\s\"'<>:;,)}\]])",
                                 prefix + "<user>"))
    return patterns


_PATTERNS = _home_patterns()
_HOME_RULES = [(re.compile(pattern), replacement) for pattern, replacement in _PATTERNS]
_USER_PATH = re.compile(
    r"([/\\]+(?:home|users)[/\\]+)"
    r"(?:[^/\\\r\n\"'<>:;,)}\]]+(?=[/\\])|[^/\\\s\"'<>:;,)}\]]+)",
    re.IGNORECASE,
)


def redact_paths(text: str) -> str:
    text = str(text)
    for pattern, replacement in _HOME_RULES:
        text = pattern.sub(lambda match, value=replacement: value, text)
    return _USER_PATH.sub(r"\1<user>", text)


_STREAM_WORKER = r'''
import json, re, sys
rules = [(re.compile(pattern), value) for pattern, value in json.loads(sys.argv[1])]
users = re.compile(sys.argv[2], re.IGNORECASE)
echo = sys.argv[3] == "1"
for raw in sys.stdin.buffer:
    text = raw.decode("utf-8", "replace")
    for pattern, value in rules:
        text = pattern.sub(lambda match, value=value: value, text)
    text = users.sub(r"\1<user>", text)
    sys.stdout.write(text)
    sys.stdout.flush()
    if echo:
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except OSError:
            pass
'''


def start_log_redactor(path, mode="a", *, echo=False):
    import json
    import subprocess
    import sys

    with open(path, mode, encoding="utf-8", errors="replace") as output:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _STREAM_WORKER,
             json.dumps(_PATTERNS), _USER_PATH.pattern, "1" if echo else "0"],
            stdin=subprocess.PIPE, stdout=output,
            stderr=None if echo else subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=True,
        )
    return proc


def open_redacted_log(path, mode="a", *, echo=False):
    """Return a pipe whose independent reader redacts even native crash output."""
    import threading

    proc = start_log_redactor(path, mode, echo=echo)
    threading.Thread(target=proc.wait, daemon=True, name="log-redaction").start()
    return proc.stdin


if __name__ == "__main__":
    import sys

    with open(sys.argv[1], "w", encoding="utf-8", errors="replace") as output:
        for line in sys.stdin:
            line = redact_paths(line)
            output.write(line)
            output.flush()
            try:
                sys.stderr.write(line)
                sys.stderr.flush()
            except OSError:
                pass
