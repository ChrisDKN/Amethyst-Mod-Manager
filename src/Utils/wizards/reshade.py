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
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

RESHADE_BASE_URL = "https://reshade.me/downloads/"
RESHADE_HOME_URL = "https://reshade.me/"

# Rendering API → DLL name the game loads ReShade as. (label, dll_name)
API_CHOICES: list[tuple[str, str]] = [
    ("DirectX 10 / 11 / 12  (dxgi.dll)", "dxgi.dll"),
    ("DirectX 9  (d3d9.dll)", "d3d9.dll"),
    ("OpenGL  (opengl32.dll)", "opengl32.dll"),
    ("Vulkan  (dxgi.dll)", "dxgi.dll"),
]

# Path to the bundled default ReShade.ini (lives next to the Qt wizard).
BUNDLED_INI_PATH = Path(__file__).resolve().parents[2] / "wizards_qt" / "ReShade.ini"


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# ReShade DLL download
# ---------------------------------------------------------------------------

def fetch_latest_reshade_url() -> tuple[str, str]:
    """Return (download_url, version_string) for the latest ReShade release.

    Raises RuntimeError if the version cannot be determined.
    """
    req = urllib.request.Request(
        RESHADE_HOME_URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Match e.g. "ReShade_Setup_6.7.3.exe" (not the _Addon variant)
    match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)\.exe(?!"[^"]*Addon)', html)
    if not match:
        # Fallback: accept any non-Addon exe
        match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)\.exe', html)
    if not match:
        raise RuntimeError("Could not find ReShade download link on reshade.me.")

    version = match.group(1)
    url = f"{RESHADE_BASE_URL}ReShade_Setup_{version}.exe"
    return url, version


def download_and_extract_reshade_dll(dest_dir: Path, arch: int = 64) -> Path:
    """Download the latest ReShade installer and extract the DLL to *dest_dir*.

    *arch* selects ``ReShade64.dll`` (default) or ``ReShade32.dll`` for
    32-bit games. The installer .exe is a self-extracting zip; Python's zipfile
    reads it by seeking past the PE stub. Returns the extracted DLL path.
    Raises RuntimeError on any failure.
    """
    url, version = fetch_latest_reshade_url()

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        data = resp.read()

    want = f"reshade{arch}.dll"
    fallback = "reshade32.dll" if arch == 64 else "reshade64.dll"

    buf = io.BytesIO(data)
    try:
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            dll_name = next((n for n in names if n.lower() == want), None)
            if dll_name is None:
                dll_name = next((n for n in names if n.lower() == fallback), None)
            if dll_name is None:
                raise RuntimeError(
                    f"ReShade installer did not contain {want} or {fallback}. "
                    f"Found: {names}"
                )
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / dll_name
            with zf.open(dll_name) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
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
    data: bytes, dest: Path, subfolder: "str | None", *, is_base: bool = False
) -> None:
    """Extract the ``Shaders/`` and ``Textures/`` trees from a GitHub repo zip
    into *dest* (which is the ``reshade-shaders/`` output folder).

    *subfolder* - if None, those folders sit at the repo root. If a string,
    they live inside that subfolder. Either way the result is merged as
    ``dest/Shaders/...`` and ``dest/Textures/...``.

    *is_base* - True only for the base ``slim`` repo (extracted first). Packs
    are not allowed to overwrite :data:`CORE_HEADERS`.
    """
    _KEEP = ("Shaders/", "Textures/")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            top = next(
                (n.split("/")[0] for n in zf.namelist() if "/" in n),
                None,
            )
            if top is None:
                raise RuntimeError("Unexpected zip layout (no top-level folder).")

            # Path prefix to strip: the repo root, plus the wrapper subfolder
            # when one is named.
            prefix = top + "/"
            if subfolder:
                prefix += subfolder.rstrip("/") + "/"

            for member in zf.namelist():
                if not member.startswith(prefix):
                    continue
                rel = member[len(prefix):]
                if not rel:
                    continue

                # Keep only the Shaders/ and Textures/ subtrees, matching the
                # top folder case-insensitively and canonicalising to the
                # capitalised name so every pack merges into the same folders.
                head, _, tail = rel.partition("/")
                canon = next((k.rstrip("/") for k in _KEEP
                              if head.lower() == k.rstrip("/").lower()), None)
                if canon is None:
                    continue
                rel = canon + ("/" + tail if tail else "")

                target = dest / rel
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    # Don't let a pack overwrite a canonical core header that
                    # the base repo already provided.
                    if (
                        not is_base
                        and target.name.lower() in CORE_HEADERS
                        and target.exists()
                    ):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
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


def _fetch_pack_zip(url: str, cache_dir: Path, timeout: float = 60.0) -> bytes:
    """Return a pack's zip bytes, reusing a cached copy when GitHub reports the
    content is unchanged (HTTP 304 via ``If-None-Match``).

    Cached under *cache_dir* as ``<owner>__<repo>__<branch>.zip`` with a sidecar
    ``.etag``. A 304 (or any network error when a cached copy exists) reuses the
    cached zip. Raises RuntimeError only with neither a fresh download nor cache.
    """
    zip_path = cache_dir / _pack_cache_filename(url)
    etag_path = zip_path.with_suffix(zip_path.suffix + ".etag")

    cached = zip_path.read_bytes() if zip_path.is_file() else None
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }
    if cached is not None and etag_path.is_file():
        try:
            tag = etag_path.read_text(encoding="utf-8").strip()
            if tag:
                headers["If-None-Match"] = tag
        except OSError:
            pass

    req = urllib.request.Request(url, headers=headers)
    try:
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as resp:
            body = resp.read()
            try:
                zip_path.write_bytes(body)
                new_tag = resp.headers.get("ETag")
                if new_tag:
                    etag_path.write_text(new_tag, encoding="utf-8")
            except OSError:
                pass  # cache write is best-effort; the bytes are still usable
            return body
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached is not None:
            return cached  # unchanged upstream - reuse the cached zip
        if cached is not None:
            return cached
        raise RuntimeError(f"{url}: {exc}") from exc
    except Exception as exc:
        if cached is not None:
            return cached  # offline / transient error - fall back to cache
        raise RuntimeError(f"{url}: {exc}") from exc


def download_and_extract_shaders(
    dest_dir: Path,
    optional_packs: "list[tuple[str, str, str | None]]",
) -> Path:
    """Download the base shader repo plus any selected optional packs and merge
    them all into *dest_dir*/reshade-shaders/.

    Each pack's zip is cached under ``dest_dir/packs/`` (ETag-validated).
    *optional_packs* is a subset of :data:`OPTIONAL_SHADER_PACKS`. Downloads run
    in parallel. Returns the ``reshade-shaders/`` folder. Raises on any failure.
    """
    out = dest_dir / "reshade-shaders"
    # Start from a clean tree so a previous preset-pruned run can't leave the
    # cache missing effects for a later non-preset (or different-preset) run.
    if out.exists():
        shutil.rmtree(str(out), ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    cache_dir = dest_dir / "packs"
    cache_dir.mkdir(parents=True, exist_ok=True)

    to_fetch = [(SHADER_BASE_URL, SHADER_BASE_SUBFOLDER)] + [
        (url, sub) for (_, url, sub) in optional_packs
    ]

    errors: list[str] = []
    results: list[tuple[bytes, "str | None"] | None] = [None] * len(to_fetch)

    def _fetch(idx: int, url: str, subfolder: "str | None") -> None:
        try:
            results[idx] = (_fetch_pack_zip(url, cache_dir), subfolder)
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    threads = [
        threading.Thread(target=_fetch, args=(i, url, sub), daemon=True)
        for i, (url, sub) in enumerate(to_fetch)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise RuntimeError("Shader download failed:\n" + "\n".join(errors))

    # Index 0 is always the base repo (see to_fetch above). Extract it first
    # with is_base=True so its canonical core headers are laid down before any
    # pack, and later packs can't overwrite them.
    for idx, entry in enumerate(results):
        if entry is not None:
            data, subfolder = entry
            _extract_zip_into(data, out, subfolder, is_base=(idx == 0))

    return out


# ---------------------------------------------------------------------------
# Preset-driven effect selection
# ---------------------------------------------------------------------------

def _set_preset_path_in_ini(ini_path: Path, preset_filename: str) -> None:
    """Rewrite the ``PresetPath=`` line in a ReShade.ini so ReShade loads the
    preset under *preset_filename* (a bare name, placed next to the ini).
    Best-effort."""
    try:
        lines = ini_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    new_value = ".\\" + preset_filename
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip().lower() == "presetpath":
            lines[i] = "PresetPath=" + new_value
            break
    else:
        return  # no PresetPath key - leave the ini untouched
    try:
        ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


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
        text = preset_path.read_text(encoding="utf-8", errors="replace")
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
                    wanted.add(fname.lower())
    return wanted


# Matches ``#include "Foo.fxh"`` (or .fx) - captures the bare filename.
_INCLUDE_RE = re.compile(
    r'#\s*include\s+[<"]\s*([^>"]+?)\s*[>"]', re.IGNORECASE
)
# Matches a quoted texture filename referenced inside a shader.
_TEXTURE_REF_RE = re.compile(
    r'["\']([^"\']+\.(?:png|jpg|jpeg|bmp|dds|tga))["\']', re.IGNORECASE
)


def _scan_includes_and_textures(path: Path) -> "tuple[set[str], set[str]]":
    """Return (included basenames lower, texture basenames lower) referenced
    directly by the shader at *path*. Best-effort text scan - never raises."""
    incs: set[str] = set()
    texs: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return incs, texs
    for m in _INCLUDE_RE.finditer(text):
        incs.add(Path(m.group(1).replace("\\", "/")).name.lower())
    for m in _TEXTURE_REF_RE.finditer(text):
        texs.add(Path(m.group(1).replace("\\", "/")).name.lower())
    return incs, texs


def prune_shaders_to_preset(shaders_dir: Path, wanted: "set[str]") -> set[str]:
    """Delete every ``.fx`` under *shaders_dir* not in *wanted*, then prune the
    include headers and textures down to only those the surviving effects
    transitively reference.

    Returns the subset of *wanted* not found on disk, so the caller can warn
    about effects missing from the downloaded packs.
    """
    if not wanted:
        return set()

    # --- pass 1: keep only the wanted .fx (first copy of each) ----------------
    found: set[str] = set()
    kept_fx: list[Path] = []
    for fx in sorted(shaders_dir.rglob("*.fx"), key=lambda p: (len(p.parts), str(p))):
        name = fx.name.lower()
        if name in wanted and name not in found:
            found.add(name)        # keep the first copy
            kept_fx.append(fx)
        else:
            try:
                fx.unlink()
            except OSError:
                pass

    # --- pass 2: resolve transitive #include closure + texture refs -----------
    header_by_name: dict[str, Path] = {}
    for ext in ("*.fxh", "*.cfg", "*.h"):
        for h in sorted(shaders_dir.rglob(ext), key=lambda p: (len(p.parts), str(p))):
            header_by_name.setdefault(h.name.lower(), h)

    needed_headers: set[str] = set()
    needed_tex: set[str] = set()
    queue: list[Path] = list(kept_fx)
    seen: set[str] = set()
    while queue:
        cur = queue.pop()
        incs, texs = _scan_includes_and_textures(cur)
        needed_tex |= texs
        for inc in incs:
            if inc in seen:
                continue
            seen.add(inc)
            if inc.endswith(".fx"):
                continue
            if inc in header_by_name:
                needed_headers.add(inc)
                queue.append(header_by_name[inc])

    # --- pass 3: delete unreferenced headers and textures ---------------------
    keep_header_paths = {header_by_name[n] for n in needed_headers if n in header_by_name}
    header_files = (
        list(shaders_dir.rglob("*.fxh"))
        + list(shaders_dir.rglob("*.cfg"))
        + list(shaders_dir.rglob("*.h"))
    )
    for h in header_files:
        if h not in keep_header_paths:
            try:
                h.unlink()
            except OSError:
                pass

    tex_root = shaders_dir / "Textures"
    if tex_root.is_dir():
        kept_tex_names: set[str] = set()
        for t in sorted(tex_root.rglob("*"), key=lambda p: (len(p.parts), str(p))):
            if not t.is_file():
                continue
            name = t.name.lower()
            if name in needed_tex and name not in kept_tex_names:
                kept_tex_names.add(name)        # keep the first copy
            else:
                try:
                    t.unlink()
                except OSError:
                    pass

    # Clean up now-empty directories left behind by the deletions.
    for d in sorted(shaders_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if d.is_dir():
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
            except OSError:
                pass

    return {w for w in wanted if w not in found}


# ---------------------------------------------------------------------------
# Install (file copy + Wine override)
# ---------------------------------------------------------------------------

def install_reshade_files(
    game: "BaseGame",
    *,
    reshade_dll: str,
    override_key: str,
    dest: str,
    mod_name: str,
    extracted_dll: "Path | None",
    extracted_shaders: "Path | None",
    preset_path: "Path | None",
    log_fn: Callable[[str], None] = _noop,
) -> str:
    """Copy ReShade into the chosen destination and apply the Wine DLL override.

    *dest* - "game" | "root_folder" | "mod". For "mod", a managed root-flagged
    mod is created (staging folder + modlist entry) and the payload is indexed
    so it deploys without a manual Refresh. Returns a human-readable status
    string; raises on failure. Blocking - call from a worker thread.

    Does NO UI refresh - the caller must reload the modlist on the GUI thread
    after this returns (a managed-mod install changes modlist.txt). Touching
    widgets from this worker-thread call would spawn a stray floating window.
    """
    from Utils.deployment import apply_wine_dll_overrides
    from Utils.mods.names import sanitize_mod_folder_name

    # ReShade must live next to the rendering exe. Ask the game handler for the
    # install subdir relative to the game root (e.g. "bin/x64", "<Project>/
    # Binaries/Win64"). Mirrored inside Root_Folder / mod staging too, since
    # those deploy verbatim to the game root.
    game_root = game.get_game_path()
    try:
        exe_subdir = game.reshade_install_subdir(game_root)
    except Exception as exc:
        log_fn(f"ReShade wizard: reshade_install_subdir failed ({exc}); using game root.")
        exe_subdir = None
    if exe_subdir is not None:
        log_fn(f"ReShade wizard: install subdir resolved to '{exe_subdir}'.")

    indexed_mod: str | None = None
    if dest == "mod":
        mod_name = (mod_name or "").strip() or "ReShade"
        from Utils.mods.install_as_mod import register_as_mod_neutral
        base_dir = register_as_mod_neutral(
            game, mod_name, None, log_fn=log_fn, root_folder=True)
        indexed_mod = mod_name
        dest_label = f"managed mod “{mod_name}” (root-flagged)"
    elif dest == "root_folder":
        base_dir = game.get_effective_root_folder_path()
        base_dir.mkdir(parents=True, exist_ok=True)
        dest_label = "Root_Folder (staging)"
    else:
        base_dir = game.get_game_path()
        if base_dir is None:
            raise RuntimeError("Game path is not configured.")
        dest_label = "game folder"

    dest_dir = base_dir / exe_subdir if exe_subdir else base_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    if exe_subdir:
        dest_label += f" (under {exe_subdir})"

    if extracted_dll is None or not Path(extracted_dll).is_file():
        raise RuntimeError("ReShade DLL not found - please restart the wizard.")

    # 1. Copy the ReShade DLL renamed to the game's override name.
    shutil.copy2(str(extracted_dll), str(dest_dir / reshade_dll))
    log_fn(f"ReShade wizard: copied {Path(extracted_dll).name} → {reshade_dll}")

    # 1b. For managed installs, seed an empty ReShade.log next to the DLL so
    #     it's a tracked, deployed file (cleaned up on restore) instead of
    #     being left behind in the game folder.
    if dest in ("mod", "root_folder"):
        (dest_dir / "ReShade.log").touch()
        log_fn("ReShade wizard: created empty ReShade.log")

    # 2. Copy bundled ReShade.ini and the preset (or a blank one).
    if preset_path is not None and Path(preset_path).is_file():
        preset_name = sanitize_mod_folder_name(Path(preset_path).stem) + ".ini"
    else:
        preset_name = "ReShadePreset.ini"

    if BUNDLED_INI_PATH.is_file():
        shutil.copy2(str(BUNDLED_INI_PATH), str(dest_dir / "ReShade.ini"))
        if preset_name != "ReShadePreset.ini":
            _set_preset_path_in_ini(dest_dir / "ReShade.ini", preset_name)
        log_fn("ReShade wizard: copied ReShade.ini")
    if preset_path is not None and Path(preset_path).is_file():
        shutil.copy2(str(preset_path), str(dest_dir / preset_name))
        log_fn(f"ReShade wizard: installed preset {Path(preset_path).name} as {preset_name}")
    else:
        (dest_dir / preset_name).touch()
        log_fn(f"ReShade wizard: created {preset_name}")

    # 3. Copy reshade-shaders/ directly into dest_dir.
    if extracted_shaders is None or not Path(extracted_shaders).is_dir():
        raise RuntimeError("Shader files not found - please restart the wizard.")
    shaders_dest = dest_dir / "reshade-shaders"
    if shaders_dest.exists():
        shutil.rmtree(str(shaders_dest))
    shutil.copytree(str(extracted_shaders), str(shaders_dest))
    log_fn("ReShade wizard: copied reshade-shaders/")

    # 3b. Index the managed mod so build_filemap sees its files and it deploys
    #     immediately (the Tk wizard skipped this - mod needed a Refresh first).
    #     The caller reloads the modlist UI on the GUI thread after we return.
    if indexed_mod is not None:
        from Utils.mods.install_as_mod import index_installed_mod
        index_installed_mod(game, indexed_mod, log_fn=log_fn)

    # 4. Apply Wine DLL override to the Proton prefix.
    prefix = getattr(game, "_prefix_path", None)
    if prefix and Path(prefix).is_dir():
        apply_wine_dll_overrides(
            Path(prefix),
            {override_key: "native,builtin"},
            log_fn=log_fn,
        )
        log_fn(f"ReShade wizard: applied Wine override {override_key}=native,builtin")
        override_note = f"✓ Wine override {override_key}=native,builtin applied."
    else:
        override_note = (
            f"⚠ Could not apply Wine override automatically.\n"
            f"Add to Steam launch options:\n"
            f'WINEDLLOVERRIDES="{override_key}=native,builtin" %command%'
        )

    deploy_note = (
        "\nDeploy your mods to copy ReShade into the game folder.\n"
        if dest in ("mod", "root_folder") else ""
    )
    log_fn("ReShade wizard: installation complete.")
    return (
        f"✓ ReShade installed to {dest_label}.\n"
        f"{override_note}\n"
        f"{deploy_note}\n"
        "Click Done to close."
    )
