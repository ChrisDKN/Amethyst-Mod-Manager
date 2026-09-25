from pathlib import Path, PureWindowsPath


def extraction_path(root: Path, name: str, *, listings: dict | None = None) -> Path:
    parts = name.replace("\\", "/").split("/")
    if (not name or "\x00" in name or PureWindowsPath(name).drive
            or any(part in {"", ".", ".."} or ":" in part for part in parts)):
        raise ValueError(f"Unsafe archive path: {name!r}")
    root = root.resolve()
    listings = {} if listings is None else listings
    target = root
    for part in parts:
        if target not in listings:
            entries = {}
            if target.is_dir():
                for entry in target.iterdir():
                    entries.setdefault(entry.name.lower(), []).append(entry.name)
            listings[target] = entries
        matches = listings[target].get(part.lower(), [])
        if len(matches) > 1:
            raise ValueError(f"Ambiguous archive destination: {name!r}")
        target = target / (matches[0] if matches else part)
        if target.is_symlink():
            raise ValueError(f"Archive path crosses a symlink: {name!r}")
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"Archive path escapes destination: {name!r}")
    return target


def extraction_paths(root: Path, names: list[str]) -> list[Path]:
    seen = set()
    targets = []
    listings = {}
    for name in names:
        key = name.replace("\\", "/").lower()
        if key in seen:
            raise ValueError(f"Duplicate archive path: {name!r}")
        seen.add(key)
        targets.append(extraction_path(root, name, listings=listings))
    return targets
