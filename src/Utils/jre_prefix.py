"""Install a Windows Java runtime into a Proton prefix (for running .jar tools).

JavaFX 8 apps (a common shape for old modding tools) need JavaFX, which was
removed from the JRE after Java 8. Liberica **Full** 8 for Windows bundles it,
so we download that and unpack it onto the prefix's C: drive at ``C:\\java8``.
The resulting ``C:\\java8\\bin\\java.exe`` is what the exe's Launch Options
point at (``C:\\java8\\bin\\java.exe -jar %command%``).

Toolkit-neutral (no tkinter / Qt imports) so both GUIs can call it from a
worker thread; progress and errors are reported through log_fn only.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

# Liberica Full 8 (Windows x64) - bundles JavaFX. Pinned to a known-good build;
# BellSoft keeps old builds available indefinitely.
LIBERICA_WIN_JRE_URL = (
    "https://download.bell-sw.com/java/8u442+7/"
    "bellsoft-jre8u442+7-windows-amd64-full.zip"
)

# Where inside the prefix the JRE lands, and the resulting Windows java.exe.
_JAVA_DIR_REL = Path("drive_c") / "java8"
JAVA_EXE_WIN = r"C:\java8\bin\java.exe"


def java_exe_in_prefix(compat_data: Path) -> Path:
    """The native path to java.exe inside a prefix (compat_data/pfx/...)."""
    return compat_data / "pfx" / _JAVA_DIR_REL / "bin" / "java.exe"


def _flatten_single_top_dir(dest: Path) -> None:
    """Liberica zips unpack into one top folder (jre8u442-full/…); move its
    contents up so bin/java.exe sits directly under *dest*."""
    if (dest / "bin" / "java.exe").is_file():
        return
    subdirs = [d for d in dest.iterdir() if d.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "bin" / "java.exe").is_file():
        top = subdirs[0]
        for entry in list(top.iterdir()):
            entry.rename(dest / entry.name)
        try:
            top.rmdir()
        except OSError:
            pass


def install_windows_jre(compat_data: Path, log_fn=lambda _m: None,
                        url: str = LIBERICA_WIN_JRE_URL) -> Path | None:
    """Download + unpack a Windows JRE (with JavaFX) into *compat_data*'s prefix.

    Returns the native path to the installed java.exe, or None on failure.
    Idempotent: if java.exe is already present it just returns it. Synchronous
    (network + unzip) - call from a worker thread.
    """
    existing = java_exe_in_prefix(compat_data)
    if existing.is_file():
        log_fn(f"Java: already installed at {JAVA_EXE_WIN}.")
        return existing

    dest = compat_data / "pfx" / _JAVA_DIR_REL
    dest.mkdir(parents=True, exist_ok=True)

    import tempfile
    archive = Path(tempfile.gettempdir()) / "liberica8-win-full.zip"
    log_fn("Java: downloading Liberica Full 8 (Windows, with JavaFX) …")
    try:
        from Utils.ca_bundle import download_file
        download_file(url, archive)
    except Exception as e:
        log_fn(f"Java: download failed - {e}")
        return None

    log_fn("Java: unpacking into the prefix …")
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    except Exception as e:
        log_fn(f"Java: unpack failed - {e}")
        return None
    finally:
        try:
            archive.unlink()
        except OSError:
            pass

    _flatten_single_top_dir(dest)
    result = java_exe_in_prefix(compat_data)
    if not result.is_file():
        log_fn("Java: install finished but java.exe wasn't found - the archive "
               "layout may have changed.")
        return None
    log_fn(f"Java: installed. Set Launch Options to '{JAVA_EXE_WIN} -jar %command%'.")
    return result
