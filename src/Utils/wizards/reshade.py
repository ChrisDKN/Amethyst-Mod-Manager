"""GUI-neutral ReShade installation logic.

Moved out of wizards/reshade.py (which imports customtkinter) so both the Tk
wizard and the Qt wizard view can share it. Everything here is pure
urllib/zipfile/shutil/re - no toolkit imports.

Covers:
  * fetching + extracting the ReShade DLL from reshade.me
  * downloading + merging the base shader repo and optional packs
  * parsing a ReShade preset and pruning the shader set down to it
  * the file-copy install (DLL + ReShade.ini + preset + reshade-shaders/) into
    the game folder / Root_Folder staging / a managed root-flagged mod, plus
    the Wine DLL override

The bundled default ReShade.ini lives next to the Qt wizard
(wizards_qt/ReShade.ini); this module references it by path.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

RESHADE_BASE_URL = "https://reshade.me/downloads/"
RESHADE_HOME_URL = "https://reshade.me/"
MAX_SETUP_BYTES = 128 * 1024 * 1024
MAX_DLL_BYTES = 64 * 1024 * 1024
MAX_SHADER_PACK_BYTES = 256 * 1024 * 1024
MAX_SHADER_FILE_BYTES = 64 * 1024 * 1024
MAX_SHADER_TREE_BYTES = 2 * 1024 * 1024 * 1024

# Rendering API → DLL name the game loads ReShade as. (label, dll_name)
API_CHOICES: list[tuple[str, str]] = [
    ("DirectX 10 / 11 / 12  (dxgi.dll)", "dxgi.dll"),
    ("DirectX 9  (d3d9.dll)", "d3d9.dll"),
    ("OpenGL  (opengl32.dll)", "opengl32.dll"),
]

# Path to the bundled default ReShade.ini (lives next to the Qt wizard).
BUNDLED_INI_PATH = Path(__file__).resolve().parents[2] / "wizards_qt" / "ReShade.ini"


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# ReShade DLL download
# ---------------------------------------------------------------------------

def fetch_latest_reshade_url() -> tuple[str, str]:
    """Return the download URL and version for the latest full add-on build.

    Raises RuntimeError if the version cannot be determined.
    """
    req = urllib.request.Request(
        RESHADE_HOME_URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        if urllib.parse.urlsplit(resp.url).scheme != "https":
            raise RuntimeError("ReShade's download page redirected away from HTTPS.")
        page = resp.read(1024 * 1024 + 1)
        if len(page) > 1024 * 1024:
            raise RuntimeError("ReShade's download page is unexpectedly large.")
        html = page.decode("utf-8", errors="replace")

    match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)_Addon\.exe', html)
    if not match:
        raise RuntimeError(
            "Could not find the full add-on ReShade download link on reshade.me."
        )

    version = match.group(1)
    url = f"{RESHADE_BASE_URL}ReShade_Setup_{version}_Addon.exe"
    return url, version


def download_and_extract_reshade_dll(
    dest_dir: Path, arch: int = 64, *, source: tuple[str, str] | None = None,
    release: tuple[str, str] | None = None,
    checkpoint: Callable[[], None] = lambda: None,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
) -> Path:
    """Download the latest ReShade installer and extract the DLL to *dest_dir*.

    *arch* selects ``ReShade64.dll`` (default) or ``ReShade32.dll`` for
    32-bit games. The installer .exe is a self-extracting zip; Python's zipfile
    reads it by seeking past the PE stub. Returns the extracted DLL path.
    Raises RuntimeError on any failure.
    """
    if isinstance(arch, bool) or not isinstance(arch, int) or arch not in (32, 64):
        raise ValueError("ReShade architecture must be 32 or 64.")
    url, _version = source or release or fetch_latest_reshade_url()
    match = re.fullmatch(
        r"https://reshade\.me/downloads/ReShade_Setup_(\d+\.\d+\.\d+)_Addon\.exe", url)
    if match is None or match.group(1) != _version:
        raise RuntimeError("The ReShade installer URL is not the official full add-on build.")

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    from Utils.ca_bundle import get_ssl_context
    checkpoint()
    with urllib.request.urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        if urllib.parse.urlsplit(resp.url).scheme != "https":
            raise RuntimeError("ReShade installer redirected away from HTTPS.")
        reported_size = resp.headers.get("Content-Length")
        expected_size = int(reported_size) if reported_size is not None else 0
        if expected_size < 0 or expected_size > MAX_SETUP_BYTES:
            raise RuntimeError("ReShade installer is unexpectedly large.")
        data = bytearray()
        progress(0, expected_size)
        while True:
            checkpoint()
            chunk = resp.read(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_SETUP_BYTES:
                raise RuntimeError("ReShade installer is unexpectedly large.")
            progress(len(data), expected_size)
        if reported_size is not None and len(data) != expected_size:
            raise RuntimeError("ReShade installer download is incomplete.")
    checkpoint()

    want = f"reshade{arch}.dll"
    buf = io.BytesIO(data)
    try:
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            matches = [name for name in names if name.lower() == want]
            if len(matches) != 1:
                raise RuntimeError(
                    f"ReShade installer did not contain exactly one {want}. "
                    f"Found: {names}"
                )
            dll_name = matches[0]
            info = zf.getinfo(dll_name)
            if info.file_size > MAX_DLL_BYTES:
                raise RuntimeError(f"ReShade DLL {dll_name} is unexpectedly large.")
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / f"ReShade{arch}.dll"
            fd, raw = tempfile.mkstemp(prefix=".reshade-dll-", dir=dest_dir)
            temporary = Path(raw)
            try:
                with zf.open(dll_name) as src, os.fdopen(fd, "wb") as dst:
                    size = 0
                    while True:
                        checkpoint()
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_DLL_BYTES:
                            raise RuntimeError(f"ReShade DLL {dll_name} is unexpectedly large.")
                        dst.write(chunk)
                checkpoint()
                temporary.replace(out_path)
            finally:
                temporary.unlink(missing_ok=True)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ReShade installer is not a valid zip archive: {exc}") from exc

    return out_path


# ---------------------------------------------------------------------------
# Shader packs
# ---------------------------------------------------------------------------

# Always downloaded - the official ReShade shader set.
SHADER_BASE_URL = "https://github.com/crosire/reshade-shaders/archive/refs/heads/slim.zip"
SHADER_BASE_SUBFOLDER = None  # has Shaders/ and Textures/ at root

# Optional shader packs shown as checkboxes in the wizard.
# Each entry: (label, url, subfolder)
#   subfolder=None  → extract Shaders/ and Textures/ from repo root
#   subfolder="xyz" → strip repo root then extract only the "xyz/" subtree
OPTIONAL_SHADER_PACKS: list[tuple[str, str, "str | None"]] = [
    ("SweetFX",          "https://github.com/CeeJayDK/SweetFX/archive/refs/heads/master.zip",                  None),
    ("qUINT",            "https://github.com/martymcmodding/qUINT/archive/refs/heads/master.zip",              None),
    ("iMMERSE",          "https://github.com/martymcmodding/iMMERSE/archive/refs/heads/main.zip",              None),
    ("METEOR",           "https://github.com/martymcmodding/METEOR/archive/refs/heads/main.zip",               None),
    ("AstrayFX",         "https://github.com/BlueSkyDefender/AstrayFX/archive/refs/heads/master.zip",          None),
    ("Depth3D",          "https://github.com/BlueSkyDefender/Depth3D/archive/refs/heads/master.zip",           None),
    ("FXShaders",        "https://github.com/luluco250/FXShaders/archive/refs/heads/master.zip",               None),
    ("Pirate Shaders",   "https://github.com/Heathen/Pirate-Shaders/archive/refs/heads/master.zip",            "reshade-shaders"),
    ("OtisFX",           "https://github.com/FransBouma/OtisFX/archive/refs/heads/master.zip",                 None),
    ("AcerolaFX",        "https://github.com/GarrettGunnell/AcerolaFX/archive/refs/heads/main.zip",            None),
    ("prod80",           "https://github.com/prod80/prod80-ReShade-Repository/archive/refs/heads/master.zip",  None),
    ("AlucardDH (DH)",   "https://github.com/AlucardDH/dh-reshade-shaders/archive/refs/heads/master.zip",      None),
    ("CobraFX",          "https://github.com/LordKobra/CobraFX/archive/refs/heads/master.zip",                 None),
    ("CorgiFX",          "https://github.com/originalnicodr/CorgiFX/archive/refs/heads/master.zip",            None),
    ("Daodan317081",     "https://github.com/Daodan317081/reshade-shaders/archive/refs/heads/master.zip",      None),
    ("Brussell Shaders", "https://github.com/brussell1/Shaders/archive/refs/heads/master.zip",                 None),
    ("Matsilagi (Ports)","https://github.com/Matsilagi/reshade-shaders/archive/refs/heads/master.zip",         None),
    ("fubax-shaders",    "https://github.com/Fubaxiusz/fubax-shaders/archive/refs/heads/master.zip",           None),
    ("Insane-Shaders",   "https://github.com/LordOfLunacy/Insane-Shaders/archive/refs/heads/master.zip",       None),
    ("Warp-FX",          "https://github.com/Radegast-FFXIV/Warp-FX/archive/refs/heads/master.zip",            None),
    ("GShade Shaders",   "https://github.com/Mortalitas/GShade-Shaders/archive/refs/heads/master.zip",         None),
    ("Legacy (crosire)", "https://github.com/crosire/reshade-shaders/archive/refs/heads/legacy.zip",           None),
    # --- additions from crosire's official EffectPackages.ini index ---
    ("CRT-Royale",       "https://github.com/akgunter/crt-royale-reshade/archive/refs/heads/master.zip",       "reshade-shaders"),
    ("RSRetroArch",      "https://github.com/Matsilagi/RSRetroArch/archive/refs/heads/main.zip",                None),
    ("VRToolkit",        "https://github.com/retroluxfilm/reshade-vrtoolkit/archive/refs/heads/main.zip",       None),
    ("FGFX",             "https://github.com/AlexTuduran/FGFX/archive/refs/heads/main.zip",                      None),
    ("CShade",           "https://github.com/papadanku/CShade/archive/refs/heads/main.zip",                      None),
    ("Lilium HDR",       "https://github.com/EndlesslyFlowering/ReShade_HDR_shaders/archive/refs/heads/master.zip", None),
    ("vort_Shaders",     "https://github.com/vortigern11/vort_Shaders/archive/refs/heads/main.zip",              None),
    ("BX-Shade",         "https://github.com/liuxd17thu/BX-Shade/archive/refs/heads/main.zip",                   None),
    ("SHADERDECK",       "https://github.com/IAmTreyM/SHADERDECK/archive/refs/heads/main.zip",                   None),
    ("Ann-ReShade",      "https://github.com/AnastasiaGals/Ann-ReShade/archive/refs/heads/main.zip",             None),
    ("PumboAutoHDR",     "https://github.com/Filoppi/PumboAutoHDR/archive/refs/heads/master.zip",                None),
    ("ZenteonFX",        "https://github.com/Zenteon/ZenteonFX/archive/refs/heads/main.zip",                     None),
    ("Ptho-FX",          "https://github.com/PthoEastCoast/Ptho-FX/archive/refs/heads/main.zip",                 None),
    ("potatoFX",         "https://github.com/GimleLarpes/potatoFX/archive/refs/heads/main.zip",                  None),
    ("Anagrama",         "https://github.com/nullfrctl/reshade-shaders/archive/refs/heads/main.zip",             None),
    ("MaxG3D HDR",       "https://github.com/MaxG2D/ReshadeSimpleHDRShaders/archive/refs/heads/main.zip",        None),
    ("Barbatos",         "https://github.com/BarbatosBachiko/Reshade-Shaders/archive/refs/heads/main.zip",       None),
    ("smolbbsoop",       "https://github.com/smolbbsoop/smolbbsoopshaders/archive/refs/heads/main.zip",          None),
    ("BFBFX",            "https://github.com/yplebedev/BFBFX/archive/refs/heads/main.zip",                       "reshade-shaders"),
    ("Rendepth",         "https://github.com/outmode/rendepth-reshade/archive/refs/heads/main.zip",              None),
    ("Crop and Resize",  "https://github.com/P0NYSLAYSTATION/Scaling-Shaders/archive/refs/heads/main.zip",       None),
    ("LumeniteFX",       "https://github.com/umar-afzaal/LumeniteFX/archive/refs/heads/mainline.zip",            None),
    ("Reshade-Shades",   "https://github.com/JakobPCoder/Reshade-Shades/archive/refs/heads/main.zip",            None),
]

# Effect filenames that presets still reference but which no current shader
# pack ships - they were renamed or removed upstream. Used only to give the
# user a clearer "this isn't a bug" message (key = bare filename, lowercase).
OBSOLETE_PRESET_EFFECTS: dict[str, str] = {
    "depth3d.fx":            "renamed upstream to SuperDepth3D.fx (Depth3D pack)",
    "superdepth3d_vr+.fx":   "renamed upstream to SuperDepth3D.fx (Depth3D pack)",
    "emotionblur.fx":        "removed from AstrayFX upstream (had a shader bug)",
}

# ReShade's canonical core headers. These ship with the base ``slim`` repo and
# must always come from it: several packs vendor a *stale* copy. We extract the
# base repo first and refuse to let any later pack overwrite these names.
CORE_HEADERS = frozenset({"reshade.fxh", "reshadeui.fxh"})


def _extract_zip_into(
    data: bytes | Path, dest: Path, subfolder: "str | None", *,
    is_base: bool = False, checkpoint: Callable[[], None] = lambda: None,
    extracted_bytes: list[int] | None = None,
) -> None:
    """Extract the ``Shaders/`` and ``Textures/`` trees from a GitHub repo zip
    into *dest* (which is the ``reshade-shaders/`` output folder).

    *subfolder* - if None, those folders sit at the repo root. If a string,
    they live inside that subfolder. Either way the result is merged as
    ``dest/Shaders/...`` and ``dest/Textures/...``.

    *is_base* - True only for the base ``slim`` repo (extracted first). Packs
    are not allowed to overwrite :data:`CORE_HEADERS`.
    """
    _KEEP = ("Shaders", "Textures")
    extracted_bytes = extracted_bytes if extracted_bytes is not None else [0]

    try:
        with zipfile.ZipFile(io.BytesIO(data) if isinstance(data, bytes) else data) as zf:
            top = next(
                (n.split("/")[0] for n in zf.namelist() if "/" in n),
                None,
            )
            if top in (None, "", ".", ".."):
                raise RuntimeError("Unexpected zip layout (no top-level folder).")

            # Path prefix to strip: the repo root, plus the wrapper subfolder
            # when one is named.
            prefix = top + "/"
            if subfolder:
                prefix += subfolder.rstrip("/") + "/"

            for info in zf.infolist():
                checkpoint()
                if not info.filename.startswith(prefix):
                    continue
                rel = info.filename[len(prefix):]
                if not rel:
                    continue
                head = rel.split("/", 1)[0]
                canon = next((name for name in _KEEP
                              if head.lower() == name.lower()), None)
                if canon is None:
                    continue
                if "\\" in rel or "\x00" in rel:
                    raise RuntimeError("Shader archive contains an unsafe path.")
                parts = rel.rstrip("/").split("/")
                if any(part in ("", ".", "..") or part.endswith((" ", "."))
                       or ":" in part for part in parts):
                    raise RuntimeError("Shader archive contains an unsafe path.")

                # Keep only the Shaders/ and Textures/ subtrees, matching the
                # top folder case-insensitively and canonicalising to the
                # capitalised name so every pack merges into the same folders.
                target = dest.joinpath(canon, *parts[1:])
                parent = target.parent
                while parent != dest:
                    if parent.is_symlink():
                        raise RuntimeError("Shader output contains a symbolic link.")
                    parent = parent.parent
                kind = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir():
                    if kind not in (0, stat.S_IFDIR):
                        raise RuntimeError("Shader archive contains a special directory.")
                    if target.is_symlink():
                        raise RuntimeError("Shader output contains a symbolic link.")
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    if kind not in (0, stat.S_IFREG):
                        raise RuntimeError("Shader archive contains a link or special file.")
                    if info.file_size > MAX_SHADER_FILE_BYTES or (
                            extracted_bytes[0] + info.file_size > MAX_SHADER_TREE_BYTES):
                        raise RuntimeError("Shader archive exceeds the extraction limit.")
                    # Don't let a pack overwrite a canonical core header that
                    # the base repo already provided.
                    if (
                        not is_base
                        and target.name.lower() in CORE_HEADERS
                        and target.exists()
                    ):
                        continue
                    if target.is_symlink() or (target.exists() and not target.is_file()):
                        raise RuntimeError("Shader output collides with a link or directory.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fd, raw = tempfile.mkstemp(prefix=".reshade-shader-", dir=target.parent)
                    temporary = Path(raw)
                    try:
                        size = 0
                        with zf.open(info) as src, os.fdopen(fd, "wb") as dst:
                            while True:
                                checkpoint()
                                chunk = src.read(65536)
                                if not chunk:
                                    break
                                size += len(chunk)
                                if (size > MAX_SHADER_FILE_BYTES or
                                        extracted_bytes[0] + size > MAX_SHADER_TREE_BYTES):
                                    raise RuntimeError("Shader archive exceeds the extraction limit.")
                                dst.write(chunk)
                        checkpoint()
                        temporary.replace(target)
                        extracted_bytes[0] += size
                    finally:
                        temporary.unlink(missing_ok=True)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Shader zip is not a valid archive: {exc}") from exc


def _pack_cache_filename(url: str) -> str:
    """Readable, collision-free filename for a pack's cached zip."""
    try:
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        owner, repo = parts[0], parts[1]
        branch = parts[-1].rsplit(".", 1)[0]  # drop .zip
        stem = f"{owner}__{repo}__{branch}"
    except Exception:
        stem = url
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    return stem + ".zip"


_PACK_CACHE_LOCKS = tuple(threading.Lock() for _ in range(64))


def _fetch_pack_zip(
    url: str, cache_dir: Path, timeout: float = 60.0, *,
    checkpoint: Callable[[], None] = lambda: None,
) -> Path:
    zip_path = cache_dir / _pack_cache_filename(url)
    etag_path = zip_path.with_suffix(zip_path.suffix + ".etag")
    if zip_path.is_symlink() or (zip_path.exists() and not zip_path.is_file()):
        raise RuntimeError("The shader pack cache contains an unsafe entry.")
    cached = (zip_path.is_file() and zip_path.stat().st_size <= MAX_SHADER_PACK_BYTES
              and zipfile.is_zipfile(zip_path))
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }
    if cached and etag_path.is_file() and not etag_path.is_symlink():
        try:
            tag = etag_path.read_text(encoding="utf-8").strip()
            if tag:
                headers["If-None-Match"] = tag
        except OSError:
            pass

    if urllib.parse.urlsplit(url).scheme != "https":
        raise RuntimeError("Shader packs must be downloaded over HTTPS.")
    req = urllib.request.Request(url, headers=headers)
    try:
        from Utils.ca_bundle import get_ssl_context
        checkpoint()
        with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as resp:
            if urllib.parse.urlsplit(resp.url).scheme != "https":
                raise RuntimeError("Shader pack redirected away from HTTPS.")
            raw_size = resp.headers.get("Content-Length")
            expected = int(raw_size) if raw_size is not None else None
            if expected is not None and (expected < 0 or expected > MAX_SHADER_PACK_BYTES):
                raise RuntimeError("Shader pack is unexpectedly large.")
            fd, raw = tempfile.mkstemp(prefix=".reshade-pack-", dir=cache_dir)
            temporary = Path(raw)
            try:
                size = 0
                with os.fdopen(fd, "wb") as output:
                    while True:
                        checkpoint()
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_SHADER_PACK_BYTES:
                            raise RuntimeError("Shader pack is unexpectedly large.")
                        output.write(chunk)
                if (expected is not None and size != expected) or not zipfile.is_zipfile(temporary):
                    raise RuntimeError("Shader pack download is incomplete or invalid.")
                checkpoint()
                temporary.replace(zip_path)
                new_tag = resp.headers.get("ETag")
                etag_path.unlink(missing_ok=True)
                if new_tag:
                    tag_fd, tag_raw = tempfile.mkstemp(prefix=".reshade-etag-", dir=cache_dir)
                    tag_temp = Path(tag_raw)
                    try:
                        with os.fdopen(tag_fd, "w", encoding="utf-8") as output:
                            output.write(new_tag)
                        tag_temp.replace(etag_path)
                    finally:
                        tag_temp.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
            return zip_path
    except urllib.error.HTTPError as exc:
        if cached:
            return zip_path
        raise RuntimeError(f"{url}: {exc}") from exc
    except (urllib.error.URLError, OSError) as exc:
        if cached:
            return zip_path
        raise RuntimeError(f"{url}: {exc}") from exc


def download_and_extract_shaders(
    dest_dir: Path,
    optional_packs: list[tuple[str, str, str | None]],
    *, cache_dir: Path | None = None,
    checkpoint: Callable[[], None] = lambda: None,
    progress: Callable[[int, int], None] = lambda _done, _total: None,
    allow_partial: bool = False,
    warnings: list[str] | None = None,
) -> Path:
    out = dest_dir / "reshade-shaders"
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise RuntimeError("The shader output path is unsafe.")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or dest_dir / "packs"
    completed = dest_dir / "completed-packs"
    for folder in (cache_dir, completed):
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise RuntimeError("The shader pack cache path is unsafe.")
        folder.mkdir(parents=True, exist_ok=True)
    packs = [("Official shaders", SHADER_BASE_URL, SHADER_BASE_SUBFOLDER)] + optional_packs
    results: dict[int, Path] = {}
    errors: dict[int, str] = {}

    def fetch(url):
        path = completed / _pack_cache_filename(url)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("The completed shader pack path is unsafe.")
        if not path.is_file():
            key = (cache_dir / _pack_cache_filename(url)).resolve()
            lock = _PACK_CACHE_LOCKS[hash(key) % len(_PACK_CACHE_LOCKS)]
            while not lock.acquire(timeout=0.2):
                checkpoint()
            try:
                checkpoint()
                cached = _fetch_pack_zip(url, cache_dir, checkpoint=checkpoint)
                shutil.copyfile(cached, path)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            finally:
                lock.release()
        return path

    checkpoint()
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(fetch, url): i for i, (_, url, _) in enumerate(packs)}
        for count, future in enumerate(as_completed(pending), 1):
            i = pending[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                checkpoint()
                errors[i] = f"{packs[i][0]}: {exc}"
            progress(count, len(packs))

    total_bytes = [0]
    for i, path in sorted(results.items()):
        checkpoint()
        try:
            with tempfile.TemporaryDirectory(prefix="pack-", dir=dest_dir) as unpacked:
                unpacked = Path(unpacked)
                _extract_zip_into(path, unpacked, packs[i][2], is_base=(i == 0),
                                  checkpoint=checkpoint, extracted_bytes=total_bytes)
                files = [p for p in unpacked.rglob("*") if p.is_file()]
                if not files:
                    raise RuntimeError("Archive contains no shaders or textures in the expected folders.")
                for source in files:
                    checkpoint()
                    target = out / source.relative_to(unpacked)
                    if i and target.name.lower() in CORE_HEADERS and target.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        except OSError:
            raise
        except Exception as exc:
            checkpoint()
            path.unlink(missing_ok=True)
            cached = cache_dir / _pack_cache_filename(packs[i][1])
            with _PACK_CACHE_LOCKS[hash(cached.resolve()) % len(_PACK_CACHE_LOCKS)]:
                cached.unlink(missing_ok=True)
                cached.with_suffix(cached.suffix + ".etag").unlink(missing_ok=True)
            errors[i] = f"{packs[i][0]}: {exc}"
    if errors:
        messages = [errors[i] for i in sorted(errors)]
        if 0 in errors or not allow_partial:
            raise RuntimeError("Shader download failed:\n" + "\n".join(messages))
        if warnings is not None:
            warnings.extend(messages)
    if not any((out / "Shaders").rglob("*.fx")):
        raise RuntimeError("The downloaded shader packs contain no effect files.")
    return out


# ---------------------------------------------------------------------------
# Preset-driven effect selection
# ---------------------------------------------------------------------------

def _set_preset_path_in_ini(ini_path: Path, preset_filename: str) -> None:
    lines = ini_path.read_text(encoding="utf-8-sig").splitlines()
    value = "PresetPath=.\\" + preset_filename
    general = next((i for i, line in enumerate(lines) if line.strip().casefold() == "[general]"), None)
    if general is None:
        lines.extend(["", "[GENERAL]", value])
    else:
        end = next((i for i in range(general + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        key = next((i for i in range(general + 1, end)
                    if lines[i].partition("=")[0].strip().casefold() == "presetpath"), None)
        if key is None:
            lines.insert(end, value)
        else:
            lines[key] = value
    ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_preset_effect_files(preset_path: Path) -> set[str]:
    """Return the set of ``.fx`` filenames referenced by a ReShade preset.

    ReShade presets list active effects in a top-level (section-less)
    ``Techniques=`` key as comma-separated ``TechniqueName@File.fx`` entries.
    Only ``Techniques=`` (the *enabled* effects) drives what gets installed.
    Returns an empty set if no ``Techniques`` key is found (caller should treat
    that as "install everything").
    """
    wanted: set[str] = set()
    try:
        text = preset_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return wanted

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            break  # entered the per-effect sections - stop
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key.strip() != "Techniques":
            continue
        for entry in value.split(","):
            entry = entry.strip()
            at = entry.find("@")
            if at >= 0:
                fname = entry[at + 1:].strip()
                if fname:
                    wanted.add(fname.replace("\\", "/").lower())
    return wanted


_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+(.+)', re.MULTILINE)


def prune_shaders_to_preset(shaders_dir: Path, wanted: set[str], *, checkpoint=lambda: None) -> set[str]:
    """Prune unused effects after resolving includes; retain support assets."""
    if not wanted:
        return set()
    root = shaders_dir.resolve()
    files = [p for p in root.rglob("*") if p.is_file()]
    by_path = {p.as_posix().casefold(): p for p in files}
    effects = {p for p in files if p.suffix.lower() == ".fx"}

    def matches(ref: str) -> set[Path]:
        ref = ref.replace("\\", "/").casefold()
        return {p for p in files if p.relative_to(root).as_posix().casefold().endswith("/" + ref)
                or p.relative_to(root).as_posix().casefold() == ref}

    keep: set[Path] = set()
    missing = set()
    for name in wanted:
        found = matches(name) & effects
        if found:
            keep.update(found)
        else:
            missing.add(name)
    queue = list(keep)
    conservative = False
    while queue:
        checkpoint()
        current = queue.pop()
        try:
            text = current.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            conservative = True
            continue
        for directive in _INCLUDE_RE.findall(text):
            literal = re.match(r'[<"]([^>"]+)[>"]', directive.strip())
            if literal is None:
                conservative = True
                continue
            ref = literal.group(1).replace("\\", "/")
            local = by_path.get((current.parent / ref).resolve().as_posix().casefold())
            found = {local} if local else matches(ref)
            if not found:
                conservative = True
            for dependency in found - keep:
                keep.add(dependency)
                queue.append(dependency)
    # Macro includes and texture filenames may depend on preset definitions.
    if not conservative:
        for effect in effects - keep:
            checkpoint()
            effect.unlink()
    return missing


# ---------------------------------------------------------------------------
# Install (file copy + Wine override)
# ---------------------------------------------------------------------------

def resolve_install_paths(game: "BaseGame", dest: str, mod_name: str) -> tuple[Path, Path, Path]:
    from Utils.mods.names import sanitize_mod_folder_name

    game_root = game.get_game_path()
    if game_root is None:
        raise RuntimeError("Game path is not configured.")
    game_root = Path(game_root)
    subdir = Path(game.reshade_install_subdir(game_root) or ".")
    game_root = game_root.resolve()
    if subdir.is_absolute() or ".." in subdir.parts:
        raise RuntimeError("Invalid ReShade executable directory.")
    target = game_root / subdir
    if dest == "game":
        return game_root, target, target
    root_getter = getattr(game, "get_root_deploy_path", None)
    deploy_root = root_getter() if callable(root_getter) else game_root
    if deploy_root is None:
        raise RuntimeError("Root deployment path is not configured.")
    relative = target.relative_to(Path(deploy_root).resolve())
    if dest == "mod":
        if not mod_name or mod_name != sanitize_mod_folder_name(mod_name):
            raise ValueError("Enter a valid mod folder name without slashes or reserved characters.")
        staging = game.get_effective_mod_staging_path()
        if staging is None:
            raise RuntimeError("Mod staging path is not configured.")
        base = Path(staging) / mod_name
        if base.is_symlink():
            raise RuntimeError("Cannot install into a linked mod directory.")
    elif dest == "root_folder":
        base = game.get_effective_root_folder_path()
        if base is None:
            raise RuntimeError("Root_Folder is not configured.")
        base = Path(base)
    else:
        raise ValueError("Unknown ReShade installation destination.")
    return base, base / relative, target


def install_reshade_files(
    game: "BaseGame",
    *,
    reshade_dll: str,
    override_key: str,
    dest: str,
    mod_name: str,
    extracted_dll: Path | None,
    extracted_shaders: Path | None,
    preset_path: Path | None,
    replace_existing: bool = False,
    log_fn: Callable[[str], None] = _noop,
) -> str:
    """Prepare the payload, back up replacements, and roll back failed copies."""
    from Utils.atomic_write import atomic_writer
    from Utils.deployment import apply_wine_dll_overrides
    from Utils.mods.names import sanitize_mod_folder_name

    if reshade_dll not in {dll for _, dll in API_CHOICES} or override_key != Path(reshade_dll).stem:
        raise ValueError("Unsupported ReShade DLL selection.")
    if extracted_dll is None or not Path(extracted_dll).is_file():
        raise RuntimeError("ReShade DLL not found - download it again.")
    if extracted_shaders is None or not Path(extracted_shaders).is_dir():
        raise RuntimeError("Shader files not found - download them again.")
    if preset_path is not None and not Path(preset_path).is_file():
        raise RuntimeError("The selected preset is no longer available.")
    if not BUNDLED_INI_PATH.is_file():
        raise RuntimeError("The bundled ReShade configuration is missing.")
    base, destination, target = resolve_install_paths(game, dest, mod_name)
    backup: Path | None = None
    changed: list[tuple[Path, Path | None]] = []
    created_dirs: set[Path] = set()
    with tempfile.TemporaryDirectory(prefix="install-", dir=Path(extracted_shaders).parent) as tmp:
        payload = Path(tmp) / "payload"
        payload.mkdir()
        shutil.copy2(extracted_dll, payload / reshade_dll)
        shutil.copytree(extracted_shaders, payload / "reshade-shaders")
        config = destination / "ReShade.ini"
        shutil.copy2(config if config.is_file() else BUNDLED_INI_PATH, payload / "ReShade.ini")
        if preset_path is not None:
            preset_name = sanitize_mod_folder_name(Path(preset_path).stem) + ".ini"
            if preset_name.casefold() in {"reshade.ini", "meta.ini"}:
                preset_name = "ReShadePreset.ini"
            shutil.copy2(preset_path, payload / preset_name)
            _set_preset_path_in_ini(payload / "ReShade.ini", preset_name)
        elif not config.is_file():
            if not (destination / "ReShadePreset.ini").exists():
                (payload / "ReShadePreset.ini").touch()
        if dest != "game" and not (destination / "ReShade.log").exists():
            (payload / "ReShade.log").touch()

        copies = [(p, destination / p.relative_to(payload))
                  for p in payload.rglob("*") if p.is_file()]
        if dest == "mod":
            from Nexus.nexus_meta import NexusModMeta, read_meta, write_meta
            meta_path = base / "meta.ini"
            if base.exists() and any(base.iterdir()) and not meta_path.is_file():
                raise RuntimeError("The mod directory already contains unregistered files. Choose another name.")
            meta = read_meta(meta_path) if meta_path.is_file() else NexusModMeta(mod_name=mod_name)
            if meta_path.is_file() and not meta.root_folder:
                raise RuntimeError("That name belongs to a non-root mod. Choose another mod name.")
            if not meta_path.is_file():
                meta.root_folder = True
                prepared_meta = Path(tmp) / "meta.ini"
                write_meta(prepared_meta, meta)
                copies.append((prepared_meta, meta_path))

        replacements = []
        planned = []
        for source, output in copies:
            for parent in output.parents:
                if parent == base.parent:
                    break
                if parent.is_symlink():
                    raise RuntimeError(f"Cannot install through a linked directory: {parent}")
            if output.exists() or output.is_symlink():
                if not output.is_file():
                    raise RuntimeError(f"Installation target is not a regular file: {output}")
                if source.read_bytes() == output.read_bytes():
                    continue
                replacements.append(output)
            planned.append((source, output))
        if replacements and not replace_existing:
            names = ", ".join(str(p.relative_to(base)) for p in replacements[:8])
            raise RuntimeError(f"Existing files would be replaced: {names}. "
                               "Choose another mod name or enable backup and replacement.")
        if replacements:
            backup_root = game.get_profile_root() / "reshade_backups"
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix="install-", dir=backup_root))
            for output in replacements:
                saved = backup / output.relative_to(base)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(output, saved)
            log_fn(f"ReShade backups: {backup}")
        try:
            for source, output in planned:
                for parent in output.parents:
                    if parent.exists():
                        break
                    created_dirs.add(parent)
                old = Path(tmp) / "rollback" / output.relative_to(base)
                existed = output.exists() or output.is_symlink()
                if existed:
                    old.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(output, old, follow_symlinks=False)
                with atomic_writer(output, "wb", encoding=None) as stream, source.open("rb") as incoming:
                    shutil.copyfileobj(incoming, stream)
                changed.append((output, old if existed else None))
            if dest == "mod":
                from Utils.mods.modlist import ensure_mod_preserving_position
                profile = getattr(game, "_active_profile_dir", None)
                profile = Path(profile) if profile else game.get_profile_root() / "profiles" / "default"
                ensure_mod_preserving_position(profile / "modlist.txt", mod_name, enabled=True)
        except Exception:
            for output, old in reversed(changed):
                if old is None:
                    output.unlink(missing_ok=True)
                elif old.is_symlink():
                    output.unlink(missing_ok=True)
                    shutil.copy2(old, output, follow_symlinks=False)
                else:
                    with atomic_writer(output, "wb", encoding=None) as stream, old.open("rb") as incoming:
                        shutil.copyfileobj(incoming, stream)
            for directory in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise

    if dest == "mod":
        from Utils.mods.install_as_mod import index_installed_mod
        index_installed_mod(game, mod_name, log_fn=log_fn)
    prefix = getattr(game, "_prefix_path", None)
    applied = bool(prefix) and apply_wine_dll_overrides(
        Path(prefix), {override_key: "native,builtin"}, log_fn=log_fn)
    override_note = (f"✓ Wine override {override_key}=native,builtin applied." if applied else
                     "⚠ Could not apply Wine override automatically.\n"
                     f'Add to Steam launch options:\nWINEDLLOVERRIDES="{override_key}=native,builtin" %command%')
    log_fn("ReShade wizard: installation complete.")
    message = f"✓ ReShade installed to {destination}.\n{override_note}\n"
    if backup:
        message += f"\nBackups: {backup}\n"
    if dest != "game":
        message += f"\nDeploy your mods to install ReShade into {target}.\n"
    return message + "\nClick Done to close."
