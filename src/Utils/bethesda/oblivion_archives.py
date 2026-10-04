import json
import os
from pathlib import Path

from Utils.atomic_write import write_atomic_text


_BASE_MTIME_NS = 1136073600 * 1_000_000_000  # 2006-01-01 UTC


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or any(
        not isinstance(name, str)
        or not Path(name).is_absolute()
        or Path(name).suffix.lower() != ".bsa"
        or not isinstance(record, list)
        or len(record) != 5
        or any(type(value) is not int for value in record[:4])
        or not isinstance(record[4], list)
        or not record[4]
        or any(type(value) is not int for value in record[4])
        for name, record in state.items()
    ):
        raise ValueError(f"Invalid Oblivion BSA timestamp backup: {path}")
    return state


def backdate_archives(data_dir: Path, state_path: Path, log_fn) -> None:
    state = _read_state(state_path)
    archives = []
    target = _BASE_MTIME_NS
    stack = [data_dir]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file():
                    if entry.name.lower().endswith(".bsa"):
                        if directory == data_dir:
                            archives.append(Path(entry.path))
                    elif Path(entry.name).suffix.lower() not in {".esp", ".esm"}:
                        target = min(target, entry.stat().st_mtime_ns - 2_000_000_000)

    # Whole, even seconds also survive filesystems with coarse timestamps.
    target = target // 2_000_000_000 * 2_000_000_000
    pending = []
    for archive in archives:
        if archive.name.lower() == "oblivion - invalidation.bsa":
            continue
        path = archive.resolve(strict=True)
        st = path.stat()
        if st.st_mtime_ns <= target:
            continue
        key = str(path)
        previous = state.get(key)
        original = st.st_mtime_ns
        applied = [target]
        if previous and previous[:3] == [st.st_dev, st.st_ino, st.st_size]:
            if st.st_mtime_ns in previous[4]:
                original = previous[3]
                applied = list(dict.fromkeys([*previous[4], target]))
        state[key] = [st.st_dev, st.st_ino, st.st_size, original, applied]
        pending.append((path, st))

    if not pending:
        return
    write_atomic_text(state_path, json.dumps(state, indent=2))
    for path, st in pending:
        os.utime(path, ns=(st.st_atime_ns, target))
    log_fn(f"  Backdated {len(pending)} Oblivion BSA(s) so loose files take priority.")


def restore_archive_timestamps(state_path: Path, log_fn) -> None:
    state = _read_state(state_path)
    restored = 0
    for name, record in state.items():
        path = Path(name)
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        if ([st.st_dev, st.st_ino, st.st_size] != record[:3]
                or st.st_mtime_ns not in record[4]):
            continue
        os.utime(path, ns=(st.st_atime_ns, record[3]))
        restored += 1
    state_path.unlink(missing_ok=True)
    if restored:
        log_fn(f"  Restored timestamps on {restored} Oblivion BSA(s).")
