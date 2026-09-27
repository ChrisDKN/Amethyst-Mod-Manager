from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Iterable

from Utils.bethesda.laa import PEFormatError, write_large_address_aware
from .diagnostics import emit
from .games import token
from .hashes import canonical_hash, hash_bytes, verify_file
from .models import Archive, GameFilePreparation, InstallRequest
from .paths import WabbajackError, safe_name, within


_CURIOS_PATCHES = {
    ("DG3YZQj7xwk=", "FQbA20bA5Dw="): "steam-to-cc.bsa.bsdiff",
    ("FQbA20bA5Dw=", "DG3YZQj7xwk="): "cc-to-steam.bsa.bsdiff",
    ("STK4THfMHzw=", "it6+eSu4OCw="): "steam-to-cc.esl.bsdiff",
    ("it6+eSu4OCw=", "STK4THfMHzw="): "cc-to-steam.esl.bsdiff",
}
_CURIOS_SIZES = {".bsa": 111740475, ".esl": 37476}


def _curios_patch(source_hash: str, output_hash: str) -> Path | None:
    name = _CURIOS_PATCHES.get((source_hash, output_hash))
    if name is None:
        return None
    patch = Path(__file__).parent / "data" / "rare_curios" / name
    return patch if patch.is_file() else None


def plan_game_file(archive: Archive, candidates: Iterable[Path], stop=None,
                   log=None, *, game="") -> GameFilePreparation | None:
    started = time.monotonic()
    candidates = list(dict.fromkeys(Path(path) for path in candidates))
    source_name = str(archive.state.get("GameFile", archive.name)).replace("\\", "/")
    emit(log, "game_file.preparation.probe", archive=archive.name,
         source_name=source_name, candidates=candidates,
         expected_size=archive.size, output_hash=archive.key)
    if (token(game) in {"skyrimspecialedition", "skyrimse"}
            and Path(source_name.casefold()).name in
            {"ccbgssse037-curios.bsa", "ccbgssse037-curios.esl"}
            and archive.size == _CURIOS_SIZES.get(Path(source_name).suffix.casefold())):
        try:
            import bsdiff4
        except ImportError:
            bsdiff4 = None
        if bsdiff4 is not None:
            suffix = Path(source_name).suffix.casefold() + ".bsdiff"
            for source in candidates:
                for (source_hash, output_hash), patch_name in _CURIOS_PATCHES.items():
                    if (output_hash == archive.key and patch_name.endswith(suffix)
                            and _curios_patch(source_hash, output_hash)
                            and verify_file(source, source_hash, archive.size, stop)):
                        emit(log, "game_file.preparation.available", archive=archive.name,
                             source=source, source_hash=source_hash, kind="rare-curios-bsdiff",
                             elapsed_seconds=round(time.monotonic() - started, 3))
                        return GameFilePreparation(source, source_hash, "rare-curios-bsdiff")
    if not source_name.casefold().endswith(".exe"):
        emit(log, "game_file.preparation.skipped", archive=archive.name,
             reason="source is not a PE executable")
        return None
    try:
        source_hash = canonical_hash(archive.state["Hash"])
    except (KeyError, WabbajackError) as exc:
        emit(log, "game_file.preparation.skipped", archive=archive.name,
             reason="source hash is unavailable", exception=str(exc))
        return None
    if source_hash == archive.key:
        emit(log, "game_file.preparation.skipped", archive=archive.name,
             reason="source and output hashes are identical")
        return None
    for source in candidates:
        try:
            if not verify_file(source, source_hash, archive.size, stop):
                emit(log, "game_file.preparation.source_rejected", archive=archive.name,
                     source=source, expected_hash=source_hash)
                continue
            with tempfile.TemporaryDirectory(prefix="amethyst-wj-") as temporary:
                target = Path(temporary) / "prepared.exe"
                write_large_address_aware(source, target, stop)
                if verify_file(target, archive.key, archive.size, stop):
                    emit(log, "game_file.preparation.available", archive=archive.name,
                         source=source, source_hash=source_hash, kind="pe-laa",
                         elapsed_seconds=round(time.monotonic() - started, 3))
                    return GameFilePreparation(source, source_hash, "pe-laa")
        except InterruptedError:
            raise
        except (OSError, PEFormatError) as exc:
            emit(log, "game_file.preparation.source_failed", archive=archive.name,
                 source=source, exception_type=type(exc).__name__, exception=str(exc))
            continue
    emit(log, "game_file.preparation.unavailable", archive=archive.name,
         candidates=len(candidates), elapsed_seconds=round(time.monotonic() - started, 3))
    return None


def materialize_game_file(request: InstallRequest, archive: Archive,
                          preparation: GameFilePreparation, stop=None,
                          log=None) -> Path:
    started = time.monotonic()
    emit(log, "game_file.preparation.started", archive=archive.name,
         source=preparation.source, source_hash=preparation.source_hash,
         output_hash=archive.key, kind=preparation.kind)
    if preparation.kind not in {"pe-laa", "rare-curios-bsdiff"}:
        raise WabbajackError(f"Unsupported game-file preparation: {preparation.kind}")
    if not verify_file(preparation.source, preparation.source_hash, archive.size, stop):
        raise WabbajackError(f"Original game file changed after preflight: {archive.name}")
    name = hash_bytes(archive.key).hex() + "-" + safe_name(archive.name)
    target = within(request.directory, "work/game-files/" + name)
    if verify_file(target, archive.key, archive.size, stop):
        target.chmod(preparation.source.stat().st_mode & 0o777)
        emit(log, "game_file.preparation.reused", archive=archive.name,
             target=target)
        return target
    try:
        if preparation.kind == "pe-laa":
            write_large_address_aware(preparation.source, target, stop)
        else:
            patch = _curios_patch(preparation.source_hash, archive.key)
            if patch is None:
                raise WabbajackError(f"Rare Curios patch is unavailable: {archive.name}")
            import bsdiff4
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".curios-", dir=target.parent,
                                             delete=False) as temporary:
                temporary_path = Path(temporary.name)
            try:
                bsdiff4.file_patch(str(preparation.source), str(temporary_path), str(patch))
                if stop is not None and stop.is_set():
                    raise InterruptedError("Installation stopped")
                if not verify_file(temporary_path, archive.key, archive.size, stop):
                    raise WabbajackError(f"Prepared game file failed verification: {archive.name}")
                temporary_path.chmod(preparation.source.stat().st_mode & 0o777)
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
    except InterruptedError:
        raise
    except WabbajackError:
        raise
    except Exception as exc:
        raise WabbajackError(f"Could not prepare {archive.name}: {exc}") from exc
    if not verify_file(target, archive.key, archive.size, stop):
        target.unlink(missing_ok=True)
        raise WabbajackError(f"Prepared game file failed verification: {archive.name}")
    emit(log, "game_file.preparation.completed", archive=archive.name,
         target=target, bytes=archive.size, hash=archive.key,
         elapsed_seconds=round(time.monotonic() - started, 3))
    return target
