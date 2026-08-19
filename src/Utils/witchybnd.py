"""Native WitchyBND backend for Elden Ring ``regulation.bin`` merging.

WitchyBND supplies the format knowledge Amethyst should not duplicate: it
decrypts/re-encrypts regulation binders and serializes PARAM files with the
matching Paramdex definitions.  Amethyst owns the actual merge semantics.

Every modded regulation is compared with the same vanilla regulation.  Those
three-way changes are then applied lowest-priority-first at cell granularity,
so two mods changing different fields in one row do not clobber each other.
Rows added or deleted by a mod are represented explicitly.  Loose DSMS-style
CSV files remain supported as deliberately whole-row edits.

The module is GUI-neutral. It installs and invokes the native Linux release,
serializes only raw PARAM members that differ, and only publishes an output
after a second Witchy unpack proves every repacked PARAM matches the expected
bytes.
"""

from __future__ import annotations

import copy
import csv
import codecs
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


LogFn = Callable[[str], None]

REGULATION_NAME = "regulation.bin"
EXE_NAME = "WitchyBND"
APP_DIR = "WitchyBND"

_LATEST_API = "https://api.github.com/repos/ividyon/WitchyBND/releases/latest"
_BINDER_MANIFEST = "_witchy-bnd4.xml"
_OVERRIDE_NAME = "appsettings.override.json"
_PARAM_XML_SUFFIX = ".param.xml"
_MAX_REPORTED_CONFLICTS = 200
# Attribute-style PARAM XML is commonly 5-10x larger than the binary member.
# This budget gives Witchy useful parallel work while keeping normal batches
# to roughly 0.5 GiB; one unusually large table may exceed it by itself.
_PARAM_BATCH_RAW_BYTES = 64 * 1024 * 1024
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_TERMINAL_STATUS_QUERIES = (b"\x1b[6n", b"\x1b[?6n")
_TERMINAL_STATUS_REPLY = b"\x1b[1;1R"

# A cross-version three-way merge needs the vanilla regulation matching the
# mod's version. Smithbox uses the same strategy in its Param Upgrader and
# publishes these versioned baselines in its source tree. Pin both the commit
# and digest so an automatic merge never trusts mutable remote content.
_SMITHBOX_BASELINE_REVISION = "6411b5229301fedd1cd18d25d01375f2dc2dbf01"
_VERSION_BASELINES = {
    "11240023": (
        "https://raw.githubusercontent.com/vawser/Smithbox/"
        f"{_SMITHBOX_BASELINE_REVISION}/src/Smithbox.Data/Assets/PARAM/ER/"
        "Regulations/1.12.4%20%2811240023%29/regulation.bin",
        "c773c44563f8233fa54eb31fae131dc12014dc4f5b1837ac42ec22712289d697",
    ),
    "11601000": (
        "https://raw.githubusercontent.com/vawser/Smithbox/"
        f"{_SMITHBOX_BASELINE_REVISION}/src/Smithbox.Data/Assets/PARAM/ER/"
        "Regulations/1.16.0%20%2811601000%29/regulation.bin",
        "c2af61b9d1ad1c895eea762725ff455218f53c142bcadea69cf53c825221a976",
    ),
}

_attempted = False
_attempt_lock = threading.Lock()


class RegulationMergeError(RuntimeError):
    """A user-actionable failure in the unpack/merge/repack pipeline."""


def _noop(_message: str) -> None:
    pass


def _log_fn(log_fn: "LogFn | None") -> LogFn:
    if log_fn is not None:
        return log_fn
    try:
        from Utils.app_log import app_log
        return lambda message: app_log(f"witchybnd: {message}")
    except Exception:
        return _noop


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def bundled_dir(game) -> Path:
    """Return ``Profiles/<game>/Applications/WitchyBND``."""
    from Utils.xedit_tools import applications_dir
    return applications_dir(game, APP_DIR)


def find_witchy(game) -> "Path | None":
    """Return the installed native executable, if present."""
    executable = bundled_dir(game) / EXE_NAME
    return executable if executable.is_file() else None


def _linux_asset(data: dict) -> "tuple[str, str] | None":
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        return None
    tag = str(data.get("tag_name") or "")
    for asset in data.get("assets") or ():
        name = str(asset.get("name") or "")
        low = name.lower()
        url = str(asset.get("browser_download_url") or "")
        if low.endswith(".zip") and "linux-x64" in low and tag and url:
            return tag, url
    return None


def _fetch_latest(log: LogFn) -> "tuple[str, str] | None":
    if platform.system() != "Linux":
        log("the automated WitchyBND backend currently requires Linux.")
        return None
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        log(f"WitchyBND has no supported build for {platform.machine()}.")
        return None
    from Utils.ca_bundle import get_ssl_context
    try:
        request = urllib.request.Request(
            _LATEST_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Amethyst-Mod-Manager",
            },
        )
        with urllib.request.urlopen(
                request, timeout=30, context=get_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log(f"could not reach the WitchyBND release feed: {exc}")
        return None
    found = _linux_asset(data)
    if found is None:
        log("the latest WitchyBND release has no linux-x64 ZIP asset.")
    return found


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (destination / member.filename).resolve()
            if target != destination and destination not in target.parents:
                raise RegulationMergeError(
                    f"refusing suspicious archive entry: {member.filename}")
        zf.extractall(destination)


def _payload_root(stage: Path) -> Path:
    """Collapse harmless single-directory wrappers in release archives."""
    root = stage
    while not (root / EXE_NAME).is_file():
        entries = [entry for entry in root.iterdir()
                   if entry.name != "__MACOSX"]
        directories = [entry for entry in entries if entry.is_dir()]
        files = [entry for entry in entries if entry.is_file()]
        if len(directories) != 1 or files:
            break
        root = directories[0]
    return root


def _write_override(install_dir: Path) -> None:
    """Pin machine-readable PARAM XML regardless of global Witchy settings."""
    settings = {
        "Bnd": True,
        # Witchy must record a defaultValue for every field so omitted common
        # cells can be materialized before comparison. At 1.0 it only elides a
        # value when every row uses it (and still records that value in fields).
        "ParamDefaultValueThreshold": 1.0,
        # Attribute avoids Witchy's per-cell XPath lookups when repacking
        # Element XML. CSV is smaller still, but WitchyBND 3.0.1 cannot
        # round-trip some wide Elden Ring PARAMs in that mode.
        "ParamCellStyle": "Attribute",
        "Recursive": False,
        "EndDelay": 0,
        "PauseOnError": False,
        "Parallel": True,
        "Expert": True,
        "Offline": True,
        "Flexible": False,
        "BackupMethod": "None",
        "GitBackup": False,
    }
    (install_dir / _OVERRIDE_NAME).write_text(
        json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _prepare_executable(executable: Path) -> None:
    """Make an existing install deterministic as well as newly installed ones."""
    try:
        executable.chmod(executable.stat().st_mode | 0o111)
        _write_override(executable.parent)
    except OSError as exc:
        raise RegulationMergeError(
            f"could not configure WitchyBND at {executable.parent}: {exc}") from exc


def install_witchy(game, log_fn: "LogFn | None" = None) -> bool:
    """Download and atomically install the latest native Linux release."""
    log = _log_fn(log_fn)
    latest = _fetch_latest(log)
    if latest is None:
        return False
    tag, url = latest
    destination = bundled_dir(game)
    destination.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading WitchyBND {tag} for linux-x64 ...")

    from Utils.ca_bundle import download_file
    archive = destination.parent / f"witchybnd-{tag}-linux-x64.zip"
    try:
        download_file(url, archive)
        with tempfile.TemporaryDirectory(
                prefix="witchybnd-install-", dir=destination.parent) as td:
            stage = Path(td)
            _safe_extract_zip(archive, stage)
            payload = _payload_root(stage)
            executable = payload / EXE_NAME
            if not executable.is_file():
                raise RegulationMergeError(
                    f"{EXE_NAME} is missing from the release archive.")

            old = destination.with_name(f".{destination.name}.previous")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            if destination.exists():
                destination.rename(old)
            try:
                shutil.copytree(payload, destination)
                installed = destination / EXE_NAME
                _prepare_executable(installed)
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                if old.exists():
                    old.rename(destination)
                raise
            shutil.rmtree(old, ignore_errors=True)
    except Exception as exc:
        log(f"could not install WitchyBND: {exc}")
        return False
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass

    if find_witchy(game) is None:
        log(f"{EXE_NAME} is missing after extraction.")
        return False
    log(f"WitchyBND {tag} installed to {destination}.")
    return True


def ensure_witchy(game, log_fn: "LogFn | None" = None) -> "Path | None":
    """Return WitchyBND, attempting one installation per process."""
    global _attempted
    found = find_witchy(game)
    if found is not None:
        return found
    with _attempt_lock:
        if _attempted:
            return None
        _attempted = True
    if not install_witchy(game, log_fn):
        return None
    return find_witchy(game)


# ---------------------------------------------------------------------------
# Source discovery and priority
# ---------------------------------------------------------------------------


@dataclass
class RegulationSource:
    """One enabled mod contributing a regulation and/or whole-row CSVs."""

    name: str
    regulation: "Path | None" = None
    csvs: list[Path] = field(default_factory=list)

    @property
    def contributes(self) -> bool:
        return self.regulation is not None or bool(self.csvs)


def find_regulation_sources(
        mod_dirs: list[tuple[str, Path]]) -> list[RegulationSource]:
    """Find each enabled mod's shallowest regulation and adjacent CSVs.

    CSV files nested in documentation and optional-variant directories are not
    attached to a top-level regulation. A CSV-only mod is still discovered when
    its CSVs are at the mod root; every file is validated against Witchy's
    current PARAM schema at merge time.
    """
    sources: list[RegulationSource] = []
    for name, mod_dir in mod_dirs:
        if not mod_dir.is_dir():
            continue
        regulation: "Path | None" = None
        try:
            candidates = [path for path in mod_dir.rglob("*")
                          if path.is_file()
                          and path.name.lower() == REGULATION_NAME]
            if candidates:
                regulation = min(
                    candidates,
                    key=lambda path: (
                        len(path.relative_to(mod_dir).parts), str(path).lower()),
                )
        except OSError:
            continue

        csv_root = regulation.parent if regulation is not None else mod_dir
        try:
            if regulation is not None:
                csvs = sorted(path for path in csv_root.iterdir()
                              if path.is_file()
                              and path.suffix.lower() == ".csv")
            else:
                csvs = sorted(path for path in csv_root.iterdir()
                              if path.is_file()
                              and path.suffix.lower() == ".csv")
        except OSError:
            csvs = []
        source = RegulationSource(name, regulation, csvs)
        if source.contributes:
            sources.append(source)
    return sources


@dataclass
class MergePlan:
    """Sources in application order (lowest priority first)."""

    sources: list[RegulationSource]

    @property
    def is_useful(self) -> bool:
        return len(self.sources) > 1


def plan_merge(sources: list[RegulationSource]) -> MergePlan:
    """Convert Amethyst's highest-first list to application order."""
    return MergePlan(list(reversed(sources)))


def describe_param_version(version: str) -> str:
    digits = "".join(character for character in version if character.isdigit())
    if len(digits) < 4:
        return version or "unknown"
    major, minor, patch = int(digits[0]), int(digits[1:3]), int(digits[3])
    return f"{major}.{minor}" if patch == 0 else f"{major}.{minor}.{patch}"


# ---------------------------------------------------------------------------
# Witchy process and unpacked regulation handling
# ---------------------------------------------------------------------------


def unpack_command(executable: Path, source: Path, location: Path) -> list[str]:
    """Unpack only the regulation binder; PARAMs are serialized selectively."""
    return [
        str(executable), "--passive", "--special", "--unpack",
        "--location", str(location), str(source),
    ]


def serialize_command(executable: Path, params: Iterable[Path]) -> list[str]:
    """Serialize selected raw PARAM members beside themselves as XML."""
    return [str(executable), "--passive", "--unpack",
            *(str(path) for path in params)]


def deserialize_command(executable: Path, params: Iterable[Path]) -> list[str]:
    """Deserialize selected PARAM XML files back to their raw members."""
    return [str(executable), "--passive", "--repack",
            *(str(path) for path in params)]


def repack_command(executable: Path, unpacked: Path) -> list[str]:
    """Repack a regulation binder whose raw PARAM members are already final."""
    return [
        str(executable), "--passive", "--special", "--repack",
        str(unpacked),
    ]


def _run_witchy(command: list[str], *, cwd: Path, label: str,
                 log: LogFn) -> None:
    """Run WitchyBND with a terminal attached and stream cleaned output.

    WitchyBND 3.x constructs PromptPlus before parsing ``--passive``. On Linux,
    PromptPlus exits immediately when stdout is a regular subprocess pipe, so a
    pseudo-terminal is required even though the operation itself is passive.
    """
    log(f"$ WitchyBND {label}")
    master_fd, slave_fd = os.openpty()
    try:
        # Some terminal libraries reject a newly-created PTY with a 0x0 window.
        import fcntl
        import struct
        import termios
        import tty
        fcntl.ioctl(
            slave_fd, termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, 120, 0, 0),
        )
        # There is no terminal emulator between the PTY and Witchy. Raw mode
        # lets PromptPlus receive our cursor-position reply immediately instead
        # of the line discipline echoing and buffering it until a newline.
        tty.setraw(slave_fd)
        environment = os.environ.copy()
        environment.setdefault("TERM", "xterm-256color")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=environment,
            close_fds=True,
        )
    except OSError as exc:
        os.close(master_fd)
        os.close(slave_fd)
        raise RegulationMergeError(f"could not start WitchyBND: {exc}") from exc
    os.close(slave_fd)

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    query_tail = b""

    def emit(text: str, *, final: bool = False) -> None:
        nonlocal pending
        pending += text.replace("\r\n", "\n").replace("\r", "\n")
        lines = pending.split("\n")
        pending = "" if final else lines.pop()
        if final and pending:
            lines.append(pending)
            pending = ""
        for line in lines:
            line = _ANSI_ESCAPE.sub("", line).strip()
            if line:
                log(f"  {line}")

    read_error: "OSError | None" = None
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as exc:
                # Linux PTYs signal EOF as EIO once the slave has closed.
                if exc.errno == errno.EIO:
                    break
                read_error = exc
                break
            if not chunk:
                break
            query_data = query_tail + chunk
            for query in _TERMINAL_STATUS_QUERIES:
                for _unused in range(query_data.count(query)):
                    try:
                        os.write(master_fd, _TERMINAL_STATUS_REPLY)
                    except OSError:
                        break
            # Preserve only a trailing partial query for the next read. Keeping
            # a complete query here would answer it twice on the following read.
            query_tail = b""
            for query in _TERMINAL_STATUS_QUERIES:
                for length in range(1, len(query)):
                    suffix = query_data[-length:]
                    if query.startswith(suffix) and length > len(query_tail):
                        query_tail = suffix
            emit(decoder.decode(chunk))
        emit(decoder.decode(b"", final=True), final=True)
    finally:
        os.close(master_fd)

    return_code = process.wait()
    if read_error is not None:
        raise RegulationMergeError(
            f"could not read WitchyBND {label} output: {read_error}")
    if return_code != 0:
        raise RegulationMergeError(
            f"WitchyBND {label} exited with code {return_code}.")


@dataclass(frozen=True)
class _UnpackedRegulation:
    input_copy: Path
    root: Path
    manifest: Path
    game: str
    version: str
    params: dict[str, Path]


def _param_files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".param":
            continue
        key = path.relative_to(root).as_posix().lower()
        if key in found:
            raise RegulationMergeError(f"duplicate PARAM path: {key}")
        found[key] = path
    return found


def _param_xml_path(param: Path) -> Path:
    return param.with_name(f"{param.name}.xml")


def _serialize_params(executable: Path, params: Iterable[Path],
                      label: str, log: LogFn) -> None:
    selected = list(params)
    if not selected:
        return
    _run_witchy(
        serialize_command(executable, selected),
        cwd=executable.parent,
        label=f"serialize {label} ({len(selected)} table(s))",
        log=log,
    )
    missing = [path.name for path in selected
               if not _param_xml_path(path).is_file()]
    if missing:
        shown = ", ".join(missing[:4])
        more = f" (+{len(missing) - 4} more)" if len(missing) > 4 else ""
        raise RegulationMergeError(
            f"WitchyBND did not serialize {shown}{more}.")


def _deserialize_params(executable: Path, xml_paths: Iterable[Path],
                        label: str, log: LogFn) -> None:
    """Write a bounded batch of merged XML tables back to raw members."""
    selected = list(xml_paths)
    if not selected:
        return
    _run_witchy(
        deserialize_command(executable, selected),
        cwd=executable.parent,
        label=f"write merged {label} ({len(selected)} table(s))",
        log=log,
    )


def _read_manifest(path: Path) -> tuple[str, str]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise RegulationMergeError(
            f"could not read {path.name}: {exc}") from exc
    game = (root.findtext("game") or "").strip()
    version = (root.findtext("version") or "").strip()
    if not game or not version:
        raise RegulationMergeError(
            f"{path.name} does not identify its game and regulation version.")
    return game, version


def _unpack_regulation(executable: Path, source: Path, stage: Path,
                       label: str, log: LogFn) -> _UnpackedRegulation:
    stage.mkdir(parents=True, exist_ok=True)
    input_copy = stage / REGULATION_NAME
    shutil.copy2(source, input_copy)
    location = stage / "unpacked"
    location.mkdir(parents=True, exist_ok=True)
    _run_witchy(
        unpack_command(executable, input_copy, location),
        cwd=executable.parent,
        label=f"unpack {label}",
        log=log,
    )
    manifests = list(location.rglob(_BINDER_MANIFEST))
    if len(manifests) != 1:
        raise RegulationMergeError(
            f"unpacking {label} produced {len(manifests)} regulation manifests; "
            "expected one.")
    manifest = manifests[0]
    game, version = _read_manifest(manifest)
    params = _param_files(manifest.parent)
    if not params:
        raise RegulationMergeError(
            f"unpacking {label} produced no PARAM files.")
    return _UnpackedRegulation(
        input_copy, manifest.parent, manifest, game, version, params)


def _non_param_payload(root: Path) -> dict[str, Path]:
    """Binder payload files outside PARAM; manifests/serialized XML are ignored."""
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        low = path.name.lower()
        if low.startswith("_witchy-") and low.endswith(".xml"):
            continue
        if low.endswith(_PARAM_XML_SUFFIX) or low.endswith(".param"):
            continue
        found[path.relative_to(root).as_posix().lower()] = path
    return found


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def _version_baseline(executable: Path, version: str, log: LogFn) -> Path:
    """Return a verified old vanilla regulation needed for a safe rebase."""
    asset = _VERSION_BASELINES.get(version)
    if asset is None:
        raise RegulationMergeError(
            "cannot safely upgrade an Elden Ring "
            f"{describe_param_version(version)} ({version}) regulation: no "
            "trusted vanilla baseline is available for that version.")
    url, expected_digest = asset
    cache_dir = executable.parent / "Regulation Baselines"
    destination = cache_dir / f"{version}.bin"
    if destination.is_file() \
            and _file_digest(destination).hex() == expected_digest:
        return destination

    log("downloading the verified Elden Ring "
        f"{describe_param_version(version)} vanilla baseline from Smithbox ...")
    temporary = destination.with_name(f".{destination.name}.download")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        from Utils.ca_bundle import download_file
        download_file(url, temporary)
        actual_digest = _file_digest(temporary).hex()
        if actual_digest != expected_digest:
            raise RegulationMergeError(
                "the downloaded versioned vanilla regulation failed its "
                "integrity check.")
        os.replace(temporary, destination)
    except RegulationMergeError:
        raise
    except Exception as exc:
        raise RegulationMergeError(
            f"could not download the {describe_param_version(version)} "
            f"vanilla baseline: {exc}") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def _validate_regulation_shape(base: _UnpackedRegulation,
                               other: _UnpackedRegulation, label: str) -> None:
    if other.game != base.game:
        raise RegulationMergeError(
            f"'{label}' is for {other.game}, not {base.game}.")
    if other.version != base.version:
        raise RegulationMergeError(
            f"'{label}' targets Elden Ring "
            f"{describe_param_version(other.version)} ({other.version}), but the "
            f"installed regulation is {describe_param_version(base.version)} "
            f"({base.version}). Update the mod before merging.")
    base_keys, other_keys = set(base.params), set(other.params)
    if other_keys != base_keys:
        missing = sorted(base_keys - other_keys)
        extra = sorted(other_keys - base_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing {len(missing)} PARAM table(s)")
        if extra:
            details.append(f"contains {len(extra)} unexpected PARAM table(s)")
        raise RegulationMergeError(
            f"'{label}' has an incompatible regulation layout ({'; '.join(details)}).")

    base_other = _non_param_payload(base.root)
    mod_other = _non_param_payload(other.root)
    if set(base_other) != set(mod_other):
        raise RegulationMergeError(
            f"'{label}' changes the regulation's non-PARAM payload, which this "
            "merger cannot combine safely.")
    for key, base_path in base_other.items():
        if _file_digest(base_path) != _file_digest(mod_other[key]):
            raise RegulationMergeError(
                f"'{label}' changes unsupported binder entry '{key}'.")


# ---------------------------------------------------------------------------
# PARAM XML model and cell-level merge
# ---------------------------------------------------------------------------


RowKey = tuple[int, int]


@dataclass
class _ParamRow:
    key: RowKey
    row_id: int
    name: str
    paramdex_name: str
    values: dict[str, str]

    def clone(self) -> "_ParamRow":
        return _ParamRow(
            self.key, self.row_id, self.name, self.paramdex_name,
            dict(self.values),
        )


@dataclass
class _ParamTable:
    path: Path
    tree: ET.ElementTree
    fields: list[str]
    field_schema: tuple[tuple[tuple[str, str], ...], ...]
    metadata: tuple[object, ...]
    rows: list[_ParamRow]

    def by_key(self) -> dict[RowKey, _ParamRow]:
        return {row.key: row for row in self.rows}

    def clone(self) -> "_ParamTable":
        tree = copy.deepcopy(self.tree)
        return _ParamTable(
            self.path,
            tree,
            list(self.fields),
            self.field_schema,
            self.metadata,
            [row.clone() for row in self.rows],
        )


def _cell_style(root: ET.Element) -> str:
    value = (root.findtext("cellStyle") or "").strip().lower()
    styles = {
        "0": "attribute", "attribute": "attribute",
        "1": "element", "element": "element",
        "2": "csv", "csv": "csv",
    }
    if value not in styles:
        raise RegulationMergeError(f"unsupported Witchy PARAM cell style '{value}'.")
    return styles[value]


def _element_signature(element: "ET.Element | None") -> object:
    """Return XML structure without insignificant formatting whitespace."""
    if element is None:
        return None
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        (element.text or "").strip(),
        tuple(_element_signature(child) for child in element),
    )


def _read_param(path: Path) -> _ParamTable:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise RegulationMergeError(f"could not read {path.name}: {exc}") from exc
    root = tree.getroot()
    fields_node = root.find("fields")
    rows_node = root.find("rows")
    if fields_node is None or rows_node is None:
        raise RegulationMergeError(f"{path.name} is missing fields or rows.")
    field_nodes = list(fields_node.findall("field"))
    fields = [node.get("name") or "" for node in field_nodes]
    if not fields or any(not name for name in fields):
        raise RegulationMergeError(f"{path.name} has an invalid field list.")
    if len(set(fields)) != len(fields):
        raise RegulationMergeError(f"{path.name} contains duplicate field names.")
    defaults = {node.get("name") or "": node.get("defaultValue")
                for node in field_nodes}
    schema = tuple(
        tuple(sorted((key, value) for key, value in node.attrib.items()
                     if key not in {"defaultValue", "defaultThreshold"}))
        for node in field_nodes
    )
    metadata_values = [(root.findtext(name) or "") for name in (
        "type", "format2D", "format2E", "dataVersion", "formatVersion", "unk06",
    )]
    paramdef = root.find("paramdef")
    metadata_values.append(_element_signature(paramdef))
    metadata = tuple(metadata_values)
    style = _cell_style(root)
    occurrences: dict[int, int] = {}
    rows: list[_ParamRow] = []
    for node in rows_node.findall("row"):
        raw_id = node.get("id")
        try:
            row_id = int(raw_id or "")
        except ValueError as exc:
            raise RegulationMergeError(
                f"{path.name} contains a row with invalid ID '{raw_id}'.") from exc
        occurrence = occurrences.get(row_id, 0)
        occurrences[row_id] = occurrence + 1
        values: dict[str, str] = {}
        if style == "element":
            explicit = {child.get("name") or "": child.text or ""
                        for child in node.findall("field")}
            for name in fields:
                value = explicit.get(name, defaults.get(name))
                if value is None:
                    raise RegulationMergeError(
                        f"{path.name} row {row_id} is missing field '{name}'.")
                values[name] = value
        elif style == "attribute":
            for name in fields:
                value = node.get(name, defaults.get(name))
                if value is None:
                    raise RegulationMergeError(
                        f"{path.name} row {row_id} is missing field '{name}'.")
                values[name] = value
        else:
            # WitchyBND 3.0.1's CSV output does not identify version-filtered
            # columns and fails to round-trip some wide ER tables itself. The
            # managed override always requests Attribute style instead.
            raise RegulationMergeError(
                f"{path.name} uses unsupported Witchy PARAM CSV cells.")
        rows.append(_ParamRow(
            (row_id, occurrence), row_id, node.get("name") or "",
            node.get("paramdexName") or "", values,
        ))
    return _ParamTable(path, tree, fields, schema, metadata, rows)


def _check_table_schema(base: _ParamTable, source: _ParamTable,
                        table_name: str, source_name: str) -> None:
    if (base.fields != source.fields or base.field_schema != source.field_schema
            or base.metadata != source.metadata):
        raise RegulationMergeError(
            f"'{source_name}' has an incompatible schema for {table_name}.")


def _insert_like(target: _ParamTable, reference: _ParamTable,
                 key: RowKey, row: _ParamRow) -> None:
    """Insert *row* close to its neighbours in *reference*."""
    target_keys = {item.key for item in target.rows}
    reference_keys = [item.key for item in reference.rows]
    try:
        reference_index = reference_keys.index(key)
    except ValueError:
        target.rows.append(row)
        return
    for previous in reversed(reference_keys[:reference_index]):
        if previous in target_keys:
            at = next(index for index, item in enumerate(target.rows)
                      if item.key == previous)
            target.rows.insert(at + 1, row)
            return
    for following in reference_keys[reference_index + 1:]:
        if following in target_keys:
            at = next(index for index, item in enumerate(target.rows)
                      if item.key == following)
            target.rows.insert(at, row)
            return
    target.rows.append(row)


@dataclass(frozen=True)
class MergeConflict:
    table: str
    row_id: int
    field: str
    previous_source: str
    winning_source: str


@dataclass
class MergeReport:
    version: str
    param_tables: int
    sources: list[str]
    source_changes: dict[str, int] = field(default_factory=dict)
    changed_tables: set[str] = field(default_factory=set)
    cell_changes: int = 0
    rows_added: int = 0
    rows_deleted: int = 0
    csv_files: int = 0
    conflict_count: int = 0
    conflicts: list[MergeConflict] = field(default_factory=list)


class _WriteTracker:
    def __init__(self, report: MergeReport):
        self.report = report
        self._writes: dict[tuple[str, RowKey, str], tuple[str, str]] = {}

    def _conflict(self, table: str, row: RowKey, field_name: str,
                  previous_source: str, winning_source: str) -> None:
        self.report.conflict_count += 1
        if len(self.report.conflicts) < _MAX_REPORTED_CONFLICTS:
            self.report.conflicts.append(MergeConflict(
                _table_name(Path(table.rsplit("/", 1)[-1])),
                row[0], field_name, previous_source, winning_source,
            ))

    def record(self, table: str, row: RowKey, field_name: str,
               value: str, source: str) -> None:
        key = (table, row, field_name)
        previous = self._writes.get(key)
        if previous is not None and previous[0] != source \
                and previous[1] != value:
            self._conflict(table, row, field_name, previous[0], source)
        self._writes[key] = (source, value)

    def row_was_deleted(self, table: str, row: RowKey, source: str) -> None:
        previous_sources = {
            previous_source
            for (written_table, written_row, _field),
            (previous_source, _value) in self._writes.items()
            if (written_table == table and written_row == row
                and _field != "<row>"
                and previous_source != source)
        }
        for previous_source in sorted(previous_sources):
            self._conflict(
                table, row, "<row>", previous_source, source)
        self.record(table, row, "<row>", "deleted", source)

    def row_is_required(self, table: str, row: RowKey, source: str) -> None:
        self.record(table, row, "<row>", "present", source)


def _row_signature(row: _ParamRow, fields: Iterable[str]) -> str:
    return json.dumps(
        [row.name, [(name, row.values[name]) for name in fields]],
        separators=(",", ":"),
    )


def _record_version_update(old: _ParamTable, current: _ParamTable,
                           table_key: str, source_name: str,
                           tracker: _WriteTracker) -> None:
    """Seed conflict tracking with official changes since an old baseline."""
    _check_table_schema(current, old, table_key, source_name)
    old_rows = old.by_key()
    current_rows = current.by_key()
    for key in old_rows.keys() - current_rows.keys():
        tracker.record(table_key, key, "<row>", "deleted", source_name)
    for key in current_rows.keys() - old_rows.keys():
        tracker.record(
            table_key, key, "<row>",
            _row_signature(current_rows[key], current.fields),
            source_name)
    for key in old_rows.keys() & current_rows.keys():
        old_row = old_rows[key]
        current_row = current_rows[key]
        if old_row.name != current_row.name:
            tracker.record(
                table_key, key, "Name", current_row.name, source_name)
        for field_name in current.fields:
            if old_row.values[field_name] != current_row.values[field_name]:
                tracker.record(
                    table_key, key, field_name,
                    current_row.values[field_name], source_name)


def _apply_regulation_table(vanilla: _ParamTable, target: _ParamTable,
                            source: _ParamTable, table_key: str,
                            source_name: str, tracker: _WriteTracker,
    report: MergeReport) -> int:
    _check_table_schema(vanilla, source, table_key, source_name)
    vanilla_rows = vanilla.by_key()
    source_rows = source.by_key()
    target_rows = target.by_key()
    changes = 0

    for key in vanilla_rows.keys() - source_rows.keys():
        existing = target_rows.get(key)
        if existing is not None:
            tracker.row_was_deleted(table_key, key, source_name)
            target.rows.remove(existing)
            target_rows.pop(key, None)
            report.rows_deleted += 1
            changes += 1

    for source_row in source.rows:
        key = source_row.key
        vanilla_row = vanilla_rows.get(key)
        if vanilla_row is None:
            tracker.record(
                table_key, key, "<row>",
                _row_signature(source_row, target.fields), source_name)
            existing = target_rows.get(key)
            if existing is None:
                added = source_row.clone()
                _insert_like(target, source, key, added)
                target_rows[key] = added
                report.rows_added += 1
            else:
                existing.name = source_row.name
                existing.paramdex_name = source_row.paramdex_name
                existing.values = dict(source_row.values)
            changes += 1
            continue

        changed_fields = [name for name in vanilla.fields
                          if source_row.values[name] != vanilla_row.values[name]]
        name_changed = source_row.name != vanilla_row.name
        if not changed_fields and not name_changed:
            continue
        target_row = target_rows.get(key)
        if target_row is None:
            tracker.row_is_required(table_key, key, source_name)
            target_row = vanilla_row.clone()
            _insert_like(target, vanilla, key, target_row)
            target_rows[key] = target_row
            report.rows_added += 1
        if name_changed:
            tracker.record(table_key, key, "Name", source_row.name, source_name)
            target_row.name = source_row.name
            changes += 1
        for field_name in changed_fields:
            value = source_row.values[field_name]
            tracker.record(table_key, key, field_name, value, source_name)
            target_row.values[field_name] = value
            report.cell_changes += 1
            changes += 1
    return changes


def _table_name(path: Path) -> str:
    name = path.name
    return name[:-len(_PARAM_XML_SUFFIX)] \
        if name.lower().endswith(_PARAM_XML_SUFFIX) else path.stem


def _resolve_csv_tables(csvs: Iterable[Path], params: dict[str, Path]
                        ) -> dict[str, list[Path]]:
    names: dict[str, list[str]] = {}
    for key, path in params.items():
        names.setdefault(_table_name(path).lower(), []).append(key)
    resolved: dict[str, list[Path]] = {}
    for csv_path in csvs:
        stem = csv_path.stem.lower()
        candidates = [name for name in names if name in stem]
        if not candidates:
            raise RegulationMergeError(
                f"cannot match CSV '{csv_path.name}' to a PARAM table.")
        best = max(candidates, key=len)
        keys = names[best]
        if len(keys) != 1:
            raise RegulationMergeError(
                f"CSV '{csv_path.name}' matches more than one PARAM table.")
        resolved.setdefault(keys[0], []).append(csv_path)
    return resolved


def _trim_trailing_empty(values: list[str], expected: int) -> list[str]:
    while len(values) > expected and values[-1] == "":
        values.pop()
    return values


def _normalise_csv_value(table: _ParamTable, field_index: int, value: str,
                         csv_path: Path, line_number: int) -> str:
    """Convert DSMS CSV scalar dummy8 cells to Witchy's bracket syntax."""
    attributes = dict(table.field_schema[field_index])
    if attributes.get("type", "").lower() != "dummy8":
        return value
    try:
        array_length = int(attributes.get("arraylength", "1"))
    except ValueError as exc:
        raise RegulationMergeError(
            f"{table.path.name} has an invalid dummy8 array length.") from exc
    if value.startswith("[") and value.endswith("]"):
        parts = value[1:-1].split("|") if value[1:-1] else []
    elif array_length == 1:
        parts = [value]
    else:
        parts = []
    try:
        numbers = [int(part) for part in parts]
    except ValueError as exc:
        raise RegulationMergeError(
            f"CSV '{csv_path.name}' line {line_number} has an invalid dummy8 "
            f"value for '{table.fields[field_index]}'.") from exc
    if len(numbers) != array_length or any(number < 0 or number > 255
                                            for number in numbers):
        raise RegulationMergeError(
            f"CSV '{csv_path.name}' line {line_number} has an invalid dummy8 "
            f"value for '{table.fields[field_index]}'.")
    return "[" + "|".join(str(number) for number in numbers) + "]"


def _apply_csv(table: _ParamTable, csv_path: Path, table_key: str,
               source_name: str, tracker: _WriteTracker,
               report: MergeReport) -> int:
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            records = list(csv.reader(stream))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RegulationMergeError(
            f"could not read CSV '{csv_path.name}': {exc}") from exc
    if not records:
        raise RegulationMergeError(f"CSV '{csv_path.name}' is empty.")
    csv_fields = table.fields
    expected = len(csv_fields) + 2
    header = _trim_trailing_empty(records[0], expected)
    if (len(header) != expected or header[0].strip().lower() != "id"
            or header[1].strip().lower() != "name"
            or header[2:] != csv_fields):
        raise RegulationMergeError(
            f"CSV '{csv_path.name}' does not match the current "
            f"{_table_name(table.path)} schema.")

    changes = 0
    for line_number, raw_record in enumerate(records[1:], start=2):
        if not raw_record or not any(value.strip() for value in raw_record):
            continue
        record = _trim_trailing_empty(raw_record, expected)
        if len(record) != expected:
            raise RegulationMergeError(
                f"CSV '{csv_path.name}' line {line_number} has "
                f"{len(record)} values; expected {expected}.")
        try:
            row_id = int(record[0].strip())
        except ValueError as exc:
            raise RegulationMergeError(
                f"CSV '{csv_path.name}' line {line_number} has invalid row ID "
                f"'{record[0]}'.") from exc
        field_values = [
            _normalise_csv_value(
                table, table.fields.index(field_name), value,
                csv_path, line_number)
            for field_name, value in zip(csv_fields, record[2:])
        ]
        target_row = next((row for row in table.rows if row.row_id == row_id), None)
        if target_row is None:
            occurrence = sum(1 for row in table.rows if row.row_id == row_id)
            target_row = _ParamRow(
                (row_id, occurrence), row_id, record[1], "",
                dict(zip(csv_fields, field_values)),
            )
            insert_at = next((index for index, row in enumerate(table.rows)
                              if row.row_id > row_id), len(table.rows))
            table.rows.insert(insert_at, target_row)
            tracker.record(
                table_key, target_row.key, "<row>",
                _row_signature(target_row, csv_fields), source_name)
            report.rows_added += 1
            changes += 1
            continue

        tracker.record(table_key, target_row.key, "Name", record[1], source_name)
        if target_row.name != record[1]:
            target_row.name = record[1]
            changes += 1
        for field_name, value in zip(csv_fields, field_values):
            # A DSMS CSV is an explicit whole-row edit.  Recording even values
            # equal to vanilla lets a higher-priority CSV intentionally restore
            # a field changed by a lower-priority regulation.
            tracker.record(table_key, target_row.key, field_name,
                           value, source_name)
            if target_row.values[field_name] != value:
                target_row.values[field_name] = value
                report.cell_changes += 1
                changes += 1
    report.csv_files += 1
    return changes


def _write_param(table: _ParamTable) -> None:
    root = table.tree.getroot()
    style = root.find("cellStyle")
    if style is None:
        raise RegulationMergeError(f"{table.path.name} has no cellStyle.")
    # Attribute is dramatically faster for Witchy to repack than resolving an
    # XPath expression for every field in Element-style XML.
    style.text = "0"
    rows_node = root.find("rows")
    if rows_node is None:
        raise RegulationMergeError(f"{table.path.name} has no rows element.")
    rows_node.clear()
    for row in table.rows:
        attributes = {"id": str(row.row_id)}
        if row.name:
            attributes["name"] = row.name
        elif row.paramdex_name:
            attributes["paramdexName"] = row.paramdex_name
        attributes.update(
            (field_name, row.values[field_name])
            for field_name in table.fields)
        ET.SubElement(rows_node, "row", attributes)
    try:
        ET.indent(table.tree, space="  ")
        table.tree.write(table.path, encoding="utf-8", xml_declaration=True)
    except OSError as exc:
        raise RegulationMergeError(
            f"could not write {table.path.name}: {exc}") from exc


def _same_param_content(left: _ParamTable, right: _ParamTable) -> bool:
    """Compare the semantics Witchy will write, independent of XML layout."""
    if (left.fields != right.fields or left.field_schema != right.field_schema
            or left.metadata != right.metadata or len(left.rows) != len(right.rows)):
        return False
    for left_row, right_row in zip(left.rows, right.rows):
        if (left_row.key != right_row.key or left_row.name != right_row.name
                or left_row.values != right_row.values):
            return False
    return True


def _param_content_digest(table: _ParamTable) -> bytes:
    """Compactly fingerprint the semantic content used by the merger."""
    digest = hashlib.sha256()

    def add(value: object) -> None:
        encoded = str(value).encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "little"))
        digest.update(encoded)

    add(table.fields)
    add(table.field_schema)
    add(table.metadata)
    add(len(table.rows))
    for row in table.rows:
        add(row.key)
        add(row.name)
        for field_name in table.fields:
            add(row.values[field_name])
    return digest.digest()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.amethyst-tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def merge_regulations(executable: Path, vanilla: Path,
                      sources: list[RegulationSource], work_dir: Path,
                      output: Path, log_fn: "LogFn | None" = None) -> MergeReport:
    """Merge *sources* into vanilla, validate, and atomically publish *output*.

    ``sources`` must be in Amethyst's displayed order (highest priority first).
    No staged mod or game file is ever modified.
    """
    log = _log_fn(log_fn)
    executable = Path(executable)
    vanilla = Path(vanilla)
    work_dir = Path(work_dir)
    output = Path(output)
    if not executable.is_file():
        raise RegulationMergeError("WitchyBND is not installed.")
    _prepare_executable(executable)
    if not vanilla.is_file():
        raise RegulationMergeError("the game's regulation.bin was not found.")
    plan = plan_merge(sources)
    if not plan.is_useful:
        raise RegulationMergeError("nothing needs merging.")
    work_dir.mkdir(parents=True, exist_ok=True)

    log("unpacking the installed regulation ...")
    base = _unpack_regulation(
        executable, vanilla, work_dir / "base", "vanilla", log)
    if base.game != "ER":
        raise RegulationMergeError(
            f"the installed regulation was detected as {base.game}, not Elden Ring.")
    report = MergeReport(
        version=base.version,
        param_tables=len(base.params),
        sources=[source.name for source in plan.sources],
        source_changes={source.name: 0 for source in plan.sources},
    )

    unpacked_sources: dict[int, _UnpackedRegulation] = {}
    source_baselines: dict[int, _UnpackedRegulation] = {}
    version_baselines: dict[str, _UnpackedRegulation] = {base.version: base}
    changed_param_keys: dict[int, set[str]] = {}
    csv_tables: dict[int, dict[str, list[Path]]] = {}
    for index, source in enumerate(plan.sources):
        if source.regulation is not None:
            log(f"unpacking '{source.name}' ...")
            unpacked = _unpack_regulation(
                executable, source.regulation,
                work_dir / f"source-{index}", source.name, log)
            if unpacked.game != base.game:
                raise RegulationMergeError(
                    f"'{source.name}' is for {unpacked.game}, not {base.game}.")
            reference = base
            if unpacked.version != base.version:
                try:
                    source_version = int(unpacked.version)
                    current_version = int(base.version)
                except ValueError as exc:
                    raise RegulationMergeError(
                        f"'{source.name}' has an invalid regulation version "
                        f"'{unpacked.version}'.") from exc
                if source_version >= current_version:
                    raise RegulationMergeError(
                        f"'{source.name}' targets newer Elden Ring "
                        f"{describe_param_version(unpacked.version)} "
                        f"({unpacked.version}); update the game before merging.")
                reference = version_baselines.get(unpacked.version)
                if reference is None:
                    baseline_path = _version_baseline(
                        executable, unpacked.version, log)
                    reference = _unpack_regulation(
                        executable, baseline_path,
                        work_dir / f"baseline-{unpacked.version}",
                        f"Elden Ring {describe_param_version(unpacked.version)} "
                        "vanilla baseline", log)
                    if (reference.game != base.game
                            or reference.version != unpacked.version):
                        raise RegulationMergeError(
                            "the downloaded vanilla baseline reopened with "
                            "unexpected game/version metadata.")
                    missing_from_current = (
                        set(reference.params) - set(base.params))
                    if missing_from_current:
                        raise RegulationMergeError(
                            f"Elden Ring {describe_param_version(base.version)} "
                            "removed PARAM tables required by the older mod; "
                            "automatic upgrading is not safe.")
                    version_baselines[unpacked.version] = reference
                log(f"  rebasing '{source.name}' from Elden Ring "
                    f"{describe_param_version(unpacked.version)} onto "
                    f"{describe_param_version(base.version)}.")
            _validate_regulation_shape(reference, unpacked, source.name)
            unpacked_sources[index] = unpacked
            source_baselines[index] = reference
            changed_param_keys[index] = {
                key for key, reference_param in reference.params.items()
                if (_file_digest(reference_param)
                    != _file_digest(unpacked.params[key]))
            }
            log(f"  '{source.name}': {len(changed_param_keys[index])} raw "
                f"PARAM table(s) differ from its "
                f"{describe_param_version(reference.version)} vanilla baseline.")
        csv_tables[index] = _resolve_csv_tables(source.csvs, base.params)

    needed_keys: set[str] = set()
    for keys in changed_param_keys.values():
        needed_keys.update(keys)
    for tables in csv_tables.values():
        needed_keys.update(tables)

    table_inputs: dict[str, list[Path]] = {}
    for table_key in sorted(needed_keys):
        inputs = [base.params[table_key]]
        for index, unpacked in unpacked_sources.items():
            if table_key not in changed_param_keys[index]:
                continue
            reference = source_baselines[index]
            if reference is not base:
                inputs.append(reference.params[table_key])
            inputs.append(unpacked.params[table_key])
        table_inputs[table_key] = list(dict.fromkeys(inputs))

    batches: list[list[str]] = []
    batch: list[str] = []
    batch_bytes = 0
    for table_key, inputs in table_inputs.items():
        input_bytes = sum(path.stat().st_size for path in inputs)
        if batch and batch_bytes + input_bytes > _PARAM_BATCH_RAW_BYTES:
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(table_key)
        batch_bytes += input_bytes
    if batch:
        batches.append(batch)

    log("applying field-level changes, lowest priority first: "
        + ", ".join(report.sources))
    tracker = _WriteTracker(report)
    seeded_updates: set[tuple[str, str]] = set()
    expected_table_digests: dict[str, bytes] = {}
    for batch_number, table_keys in enumerate(batches, start=1):
        serialized_params = [
            path for table_key in table_keys for path in table_inputs[table_key]
        ]
        _serialize_params(
            executable, serialized_params,
            f"batch {batch_number} of {len(batches)}", log)
        merged_xmls: list[Path] = []
        try:
            for table_key in table_keys:
                base_path = base.params[table_key]
                base_xml = _param_xml_path(base_path)
                vanilla_table = _read_param(base_xml)
                target_table = vanilla_table.clone()
                table_changed = False
                for index, source_info in enumerate(plan.sources):
                    source_changes = 0
                    unpacked = unpacked_sources.get(index)
                    if (unpacked is not None
                            and table_key in changed_param_keys[index]):
                        reference = source_baselines[index]
                        reference_table = vanilla_table
                        if reference is not base:
                            reference_table = _read_param(
                                _param_xml_path(reference.params[table_key]))
                            try:
                                _check_table_schema(
                                    vanilla_table, reference_table, table_key,
                                    f"Elden Ring "
                                    f"{describe_param_version(reference.version)} "
                                    "vanilla baseline")
                            except RegulationMergeError as exc:
                                raise RegulationMergeError(
                                    f"cannot safely upgrade "
                                    f"'{source_info.name}' from Elden Ring "
                                    f"{describe_param_version(reference.version)}: "
                                    f"the schema for {_table_name(base_path)} "
                                    "changed.") from exc
                            update_key = (reference.version, table_key)
                            if update_key not in seeded_updates:
                                _record_version_update(
                                    reference_table, vanilla_table, table_key,
                                    f"Elden Ring "
                                    f"{describe_param_version(base.version)} "
                                    "update", tracker)
                                seeded_updates.add(update_key)
                        source_table = _read_param(
                            _param_xml_path(unpacked.params[table_key]))
                        source_changes += _apply_regulation_table(
                            reference_table, target_table, source_table,
                            table_key, source_info.name, tracker, report)
                    for csv_path in csv_tables[index].get(table_key, ()):
                        source_changes += _apply_csv(
                            target_table, csv_path, table_key,
                            source_info.name, tracker, report)
                    if source_changes:
                        report.source_changes[source_info.name] += source_changes
                        table_changed = True
                if table_changed:
                    _write_param(target_table)
                    merged_xmls.append(base_xml)
                    expected_table_digests[table_key] = _param_content_digest(
                        target_table)
                    report.changed_tables.add(_table_name(base_path))
            _deserialize_params(
                executable, merged_xmls,
                f"batch {batch_number} of {len(batches)}", log)
        finally:
            for param_path in serialized_params:
                try:
                    _param_xml_path(param_path).unlink(missing_ok=True)
                except OSError:
                    pass

    for source_name, count in report.source_changes.items():
        log(f"  '{source_name}': {count} effective change(s).")
    if not report.changed_tables:
        log("the selected inputs contain no changes from the installed regulation.")
    else:
        log(f"changed {len(report.changed_tables)} of {report.param_tables} PARAM tables.")

    # Each bounded XML batch was already converted back to raw PARAMs, so binder
    # repacking is deliberately non-recursive. Remove the copied input first so
    # a missing output cannot be mistaken for the original after a superficially
    # successful process exit.
    base.input_copy.unlink(missing_ok=True)
    _run_witchy(
        repack_command(executable, base.root),
        cwd=executable.parent,
        label="repack merged regulation",
        log=log,
    )
    if not base.input_copy.is_file() or base.input_copy.stat().st_size == 0:
        raise RegulationMergeError("WitchyBND produced no merged regulation.bin.")

    log("validating the merged regulation ...")
    validated = _unpack_regulation(
        executable, base.input_copy, work_dir / "validation", "merged output", log)
    if validated.game != base.game or validated.version != base.version:
        raise RegulationMergeError(
            "the merged regulation reopened with different game/version metadata.")
    if set(validated.params) != set(base.params):
        raise RegulationMergeError(
            "the merged regulation reopened with a different PARAM table set.")
    for table_key, expected_path in base.params.items():
        if _file_digest(expected_path) != _file_digest(validated.params[table_key]):
            raise RegulationMergeError(
                "the merged regulation did not preserve the expected data in "
                f"{_table_name(expected_path)}.")
    # Re-open every changed PARAM semantically from the final binder. This
    # catches a successful Witchy exit that skipped or truncated a field while
    # avoiding a duplicate verification pass after every intermediate batch.
    semantic_params = [
        validated.params[table_key]
        for table_key in sorted(expected_table_digests)
    ]
    try:
        _serialize_params(
            executable, semantic_params, "merged PARAM validation", log)
        for table_key, expected_digest in expected_table_digests.items():
            actual = _read_param(_param_xml_path(validated.params[table_key]))
            if _param_content_digest(actual) != expected_digest:
                raise RegulationMergeError(
                    "WitchyBND did not preserve the merged data in "
                    f"{_table_name(validated.params[table_key])}.")
    finally:
        for param_path in semantic_params:
            try:
                _param_xml_path(param_path).unlink(missing_ok=True)
            except OSError:
                pass
    _atomic_copy(base.input_copy, output)
    log(f"validated merged regulation written to {output.parent}")
    return report
